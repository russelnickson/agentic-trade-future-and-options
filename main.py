"""
Process supervisor for the F&O trading data engine.

Launches four isolated processes:
  1. WebSocket Streamer  – broker ticks → ZeroMQ PUB
  2. ZeroMQ Worker       – SUB + latency health checks
  3. Redis Cache Manager – SUB → hot option-chain / latest-tick cache
  4. DB Writer           – SUB → batched TimescaleDB inserts
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import os
import signal
import sys
import time
from typing import Sequence

from ingestion.zmq_pub import DEFAULT_ENDPOINT

logger = logging.getLogger("main")


def _configure_logging(name: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s %(levelname)s [{name}] %(message)s",
        force=True,
    )


def _tick_token(tick: dict) -> int | None:
    for key in ("instrument_token", "token", "security_id"):
        if key in tick and tick[key] is not None:
            try:
                return int(tick[key])
            except (TypeError, ValueError):
                return None
    return None


# --------------------------------------------------------------------------- #
# Worker processes
# --------------------------------------------------------------------------- #


def run_websocket_streamer(
    tokens: Sequence[int],
    broker: str,
    mode: str,
    stop_event: mp.synchronize.Event,
) -> None:
    _configure_logging("websocket-streamer")
    log = logging.getLogger("websocket-streamer")

    from ingestion.tick_listener import TickListener

    listener = TickListener(tokens, broker=broker, mode=mode)  # type: ignore[arg-type]
    listener.start()
    log.info("WebSocket streamer running (%s, %d tokens)", broker, len(tokens))
    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    finally:
        listener.stop()
        log.info("WebSocket streamer stopped")


def run_zmq_worker(
    endpoint: str,
    stop_event: mp.synchronize.Event,
    latency_threshold_ms: float,
) -> None:
    _configure_logging("zmq-worker")
    log = logging.getLogger("zmq-worker")

    from ingestion.zmq_sub import iter_ticks
    from services.health_check import HealthCheck

    health = HealthCheck(threshold_ms=latency_threshold_ms)
    log.info("ZeroMQ worker subscribed to %s", endpoint)
    try:
        for tick in iter_ticks(endpoint, stop_event=stop_event):
            health.observe(tick)
    finally:
        health.log_summary()
        log.info("ZeroMQ worker stopped")


def run_redis_cache_manager(
    endpoint: str,
    stop_event: mp.synchronize.Event,
) -> None:
    _configure_logging("redis-cache")
    log = logging.getLogger("redis-cache")

    from database.chain_cache import ChainCache
    from database.redis_client import RedisClient
    from ingestion.zmq_sub import iter_ticks

    redis_client = RedisClient.from_settings()
    cache = ChainCache(redis_client)

    # Premarket flush clears option_chain:* — rebuild skeleton from TRADE_TOKENS
    # so live ticks assemble into option_chain:{NIFTY|BANKNIFTY}.
    try:
        from services.chain_bootstrap import bootstrap_option_chains

        summary = bootstrap_option_chains(redis_client=redis_client)
        log.info("Option-chain bootstrap OK: %s", summary.get("underlyings"))
        # Reload local token index after bootstrap wrote Redis.
        cache = ChainCache(redis_client)
    except Exception:
        log.exception(
            "Option-chain bootstrap failed — ticks will only update tick:* keys "
            "(Trade Console will show SKIP until bootstrap succeeds)"
        )

    log.info("Redis cache manager subscribed to %s", endpoint)
    try:
        for tick in iter_ticks(endpoint, stop_event=stop_event):
            token = _tick_token(tick)
            if token is None:
                continue
            cache.on_tick(token, tick)
    finally:
        redis_client.close()
        log.info("Redis cache manager stopped")


def run_db_writer(
    endpoint: str,
    stop_event: mp.synchronize.Event,
) -> None:
    _configure_logging("db-writer")
    log = logging.getLogger("db-writer")

    from database.db_writer import DbWriter
    from ingestion.zmq_sub import iter_ticks

    writer = DbWriter()
    writer.start()
    log.info("DB writer subscribed to %s", endpoint)
    try:
        for tick in iter_ticks(endpoint, stop_event=stop_event):
            try:
                writer.write_tick(tick)
            except Exception:
                log.exception("Failed to enqueue tick for TimescaleDB")
    finally:
        writer.stop()
        log.info("DB writer stopped (rows_written=%d)", writer.rows_written)


# --------------------------------------------------------------------------- #
# Supervisor
# --------------------------------------------------------------------------- #


def run_circuit_breaker(
    broker: str,
    stop_event: mp.synchronize.Event,
) -> None:
    _configure_logging("circuit-breaker")
    log = logging.getLogger("circuit-breaker")

    from services.circuit_breaker import CircuitBreaker

    breaker = CircuitBreaker(broker=broker)  # type: ignore[arg-type]
    breaker.start()
    log.info("Circuit breaker monitoring daily P&L")
    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    finally:
        breaker.stop()
        log.info("Circuit breaker stopped")


def parse_tokens(raw: str) -> list[int]:
    tokens = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        tokens.append(int(part))
    if not tokens:
        raise argparse.ArgumentTypeError("at least one token is required")
    return sorted(set(tokens))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch F&O data-engine processes (WS / ZMQ / Redis / DB)",
    )
    parser.add_argument(
        "--tokens",
        type=parse_tokens,
        default=os.getenv("TRADE_TOKENS"),
        help="Comma-separated F&O tokens (or env TRADE_TOKENS)",
    )
    parser.add_argument(
        "--broker",
        choices=("zerodha", "dhan"),
        default=os.getenv("TRADE_BROKER", "zerodha"),
    )
    parser.add_argument(
        "--mode",
        choices=("ltp", "quote", "full"),
        default=os.getenv("TRADE_FEED_MODE", "full"),
    )
    parser.add_argument(
        "--zmq-endpoint",
        default=os.getenv("TRADE_ZMQ_ENDPOINT", DEFAULT_ENDPOINT),
    )
    parser.add_argument(
        "--latency-threshold-ms",
        type=float,
        default=float(os.getenv("TRADE_LATENCY_THRESHOLD_MS", "500")),
    )
    parser.add_argument(
        "--max-clock-drift-ms",
        type=float,
        default=float(os.getenv("TRADE_MAX_CLOCK_DRIFT_MS", "50")),
        help="Abort launch if NTP drift exceeds this many ms (default 50)",
    )
    parser.add_argument(
        "--skip-clock-sync",
        action="store_true",
        default=os.getenv("TRADE_SKIP_CLOCK_SYNC", "").lower() in {"1", "true", "yes"},
        help="Skip the pre-launch NTP / IST clock drift gate",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_logging("supervisor")
    args = build_parser().parse_args(argv)

    if not args.tokens:
        logger.error("Provide --tokens or set TRADE_TOKENS")
        return 2

    if not args.skip_clock_sync:
        from services.clock_sync import SystemLaunchWarning, ensure_clock_synced

        try:
            sync = ensure_clock_synced(max_drift_ms=args.max_clock_drift_ms)
            logger.info(
                "Pre-launch clock sync OK (drift=%.3fms vs %s)",
                sync.drift_ms,
                sync.ntp_host,
            )
        except SystemLaunchWarning:
            logger.error("Aborting launch due to system clock drift")
            return 3

    ctx = mp.get_context("spawn")
    stop_event = ctx.Event()

    # Consumers first (connect), streamer last (bind) to reduce slow-joiner loss.
    process_specs = [
        (
            "zmq-worker",
            run_zmq_worker,
            (args.zmq_endpoint, stop_event, args.latency_threshold_ms),
        ),
        (
            "redis-cache",
            run_redis_cache_manager,
            (args.zmq_endpoint, stop_event),
        ),
        (
            "db-writer",
            run_db_writer,
            (args.zmq_endpoint, stop_event),
        ),
        (
            "circuit-breaker",
            run_circuit_breaker,
            (args.broker, stop_event),
        ),
        (
            "websocket-streamer",
            run_websocket_streamer,
            (args.tokens, args.broker, args.mode, stop_event),
        ),
    ]

    processes: list[mp.Process] = []
    for name, target, target_args in process_specs:
        proc = ctx.Process(target=target, args=target_args, name=name, daemon=False)
        processes.append(proc)

    def _shutdown(signum: int, _frame: object) -> None:
        logger.info("Received signal %s; shutting down children", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    for proc in processes:
        proc.start()
        logger.info("Started process %s pid=%s", proc.name, proc.pid)
        # Brief stagger so SUB sockets connect before the PUB bind races them.
        time.sleep(0.2)

    try:
        while not stop_event.is_set():
            alive = [p for p in processes if p.is_alive()]
            if len(alive) < len(processes):
                dead = [p.name for p in processes if not p.is_alive()]
                logger.error("Process exited unexpectedly: %s — stopping all", dead)
                stop_event.set()
                break
            time.sleep(0.5)
    finally:
        stop_event.set()
        for proc in processes:
            proc.join(timeout=8.0)
            if proc.is_alive():
                logger.warning("Force-terminating %s pid=%s", proc.name, proc.pid)
                proc.terminate()
                proc.join(timeout=3.0)
        logger.info("All processes stopped")

    return 0


if __name__ == "__main__":
    # Required for macOS / Windows spawn start method.
    mp.set_start_method("spawn", force=True)
    sys.exit(main())
