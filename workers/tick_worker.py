"""PM2 tick_worker — WebSocket streamer + ZMQ health + Redis cache + DB writer.

Circuit breaker / auto square-off runs separately as PM2 app ``auto_squareoff``.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import signal
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ingestion.zmq_pub import DEFAULT_ENDPOINT
from main import (
    run_db_writer,
    run_redis_cache_manager,
    run_websocket_streamer,
    run_zmq_worker,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [tick_worker] %(message)s",
    force=True,
)
logger = logging.getLogger("tick_worker")


def main() -> int:
    # Load gitignored desk secrets so PM2 does not depend on shell exports.
    try:
        from dashboard.secrets_store import apply_secrets_to_environ

        apply_secrets_to_environ()
    except Exception:
        logger.debug("secrets bootstrap skipped", exc_info=True)

    raw = os.getenv("TRADE_TOKENS", "").strip()
    if not raw:
        logger.error("TRADE_TOKENS is required (comma-separated instrument tokens)")
        return 2

    tokens = [int(t.strip()) for t in raw.split(",") if t.strip()]
    broker = os.getenv("TRADE_BROKER", "zerodha").lower()
    mode = os.getenv("TRADE_FEED_MODE", "full")
    endpoint = os.getenv("TRADE_ZMQ_ENDPOINT", DEFAULT_ENDPOINT)
    latency_ms = float(os.getenv("TRADE_LATENCY_THRESHOLD_MS", "500"))
    # Desk hosts often sit ~50–150ms off NTP without admin rights to step the clock.
    max_drift_ms = float(os.getenv("TRADE_MAX_CLOCK_DRIFT_MS", "150"))

    if os.getenv("TRADE_SKIP_CLOCK_SYNC", "").lower() not in {"1", "true", "yes"}:
        from services.clock_sync import SystemLaunchWarning, ensure_clock_synced

        try:
            sync = ensure_clock_synced(max_drift_ms=max_drift_ms)
            logger.info("Clock sync OK (drift=%.3fms)", sync.drift_ms)
        except SystemLaunchWarning:
            logger.error("Aborting tick_worker due to system clock drift")
            return 3

    ctx = mp.get_context("spawn")
    stop_event = ctx.Event()

    process_specs = [
        ("zmq-worker", run_zmq_worker, (endpoint, stop_event, latency_ms)),
        ("redis-cache", run_redis_cache_manager, (endpoint, stop_event)),
        ("db-writer", run_db_writer, (endpoint, stop_event)),
        ("websocket-streamer", run_websocket_streamer, (tokens, broker, mode, stop_event)),
    ]

    processes: list[mp.Process] = []

    def _shutdown(signum: int, _frame: object) -> None:
        logger.info("Received signal %s; shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    for name, target, target_args in process_specs:
        proc = ctx.Process(target=target, args=target_args, name=name, daemon=False)
        processes.append(proc)
        proc.start()
        logger.info("Started %s pid=%s", name, proc.pid)
        time.sleep(0.2)

    try:
        while not stop_event.is_set():
            if any(not p.is_alive() for p in processes):
                dead = [p.name for p in processes if not p.is_alive()]
                logger.error("Child exited: %s — stopping all", dead)
                stop_event.set()
                break
            time.sleep(0.5)
    finally:
        stop_event.set()
        for proc in processes:
            proc.join(timeout=8.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=3.0)
        logger.info("tick_worker stopped")

    return 0


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    raise SystemExit(main())
