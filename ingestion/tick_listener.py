"""Live F&O tick listener (Zerodha KiteTicker or Dhan MarketFeed) -> ZeroMQ PUB."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Iterable, Literal

from config.settings import get_settings
from ingestion.zmq_pub import publish_tick

logger = logging.getLogger(__name__)

BrokerName = Literal["zerodha", "dhan"]

# Bound the fan-in queue so WS callbacks never block on a slow publisher.
_PUBLISH_QUEUE_SIZE = 50_000


class TickListener:
    """
    Subscribe to active F&O tokens and fan ticks out to zmq_pub.

    WebSocket callbacks only enqueue; a dedicated thread calls publish_tick
    so market-data threads are never blocked on ZeroMQ I/O.
    """

    def __init__(
        self,
        tokens: Iterable[int | str],
        *,
        broker: BrokerName = "zerodha",
        mode: str = "full",
    ) -> None:
        self.broker = broker
        self.mode = mode.lower()
        self.tokens = sorted({int(t) for t in tokens})
        if not self.tokens:
            raise ValueError("tokens must not be empty")

        self._settings = get_settings()
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_PUBLISH_QUEUE_SIZE)
        self._running = threading.Event()
        self._publisher_thread: threading.Thread | None = None
        self._feed_thread: threading.Thread | None = None
        self._kite: Any = None
        self._dhan_feed: Any = None
        self._dropped = 0

    # ------------------------------------------------------------------ publish
    def _enqueue_tick(self, tick: dict[str, Any]) -> None:
        """Non-blocking handoff from the broker WS thread to the publisher."""
        try:
            self._queue.put_nowait(tick)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 1000 == 1:
                logger.warning(
                    "Tick publish queue full; dropped=%d (latest token=%s)",
                    self._dropped,
                    tick.get("instrument_token") or tick.get("security_id"),
                )

    def _publisher_loop(self) -> None:
        while self._running.is_set() or not self._queue.empty():
            try:
                tick = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                publish_tick(tick)
            except Exception:
                logger.exception("Failed to publish tick to ZeroMQ")
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ Zerodha
    def _start_zerodha(self) -> None:
        from kiteconnect import KiteTicker

        tokens = list(self.tokens)
        kws = KiteTicker(
            self._settings.zerodha_api_key,
            self._settings.zerodha_access_token,
            reconnect=True,
        )

        def on_ticks(_ws: Any, ticks: list[dict[str, Any]]) -> None:
            for tick in ticks:
                self._enqueue_tick(tick)

        def on_connect(ws: Any, _response: Any) -> None:
            logger.info("Zerodha WS connected; subscribing to %d tokens", len(tokens))
            ws.subscribe(tokens)
            mode = {
                "ltp": ws.MODE_LTP,
                "quote": ws.MODE_QUOTE,
                "full": ws.MODE_FULL,
            }.get(self.mode, ws.MODE_FULL)
            ws.set_mode(mode, tokens)

        def on_reconnect(_ws: Any, attempts_count: int) -> None:
            logger.warning("Zerodha WS reconnecting (attempt %s)", attempts_count)

        def on_noreconnect(_ws: Any) -> None:
            logger.error("Zerodha WS gave up reconnecting")

        def on_close(_ws: Any, code: Any, reason: Any) -> None:
            logger.warning("Zerodha WS closed code=%s reason=%s", code, reason)

        def on_error(_ws: Any, code: Any, reason: Any) -> None:
            logger.error("Zerodha WS error code=%s reason=%s", code, reason)

        kws.on_ticks = on_ticks
        kws.on_connect = on_connect
        kws.on_reconnect = on_reconnect
        kws.on_noreconnect = on_noreconnect
        kws.on_close = on_close
        kws.on_error = on_error

        self._kite = kws
        # threaded=True keeps reconnect/resubscribe off the caller thread.
        kws.connect(threaded=True)

    # --------------------------------------------------------------------- Dhan
    def _dhan_instruments(self) -> list[tuple[int, str, int]]:
        from dhanhq.marketfeed import MarketFeed

        # Mode / segment constants live on the MarketFeed class (dhanhq ≥ current).
        feed_mode = {
            "ltp": MarketFeed.Ticker,
            "quote": MarketFeed.Quote,
            "full": MarketFeed.Full,
        }.get(self.mode, MarketFeed.Full)

        return [
            (MarketFeed.NSE_FNO, str(token), feed_mode) for token in self.tokens
        ]

    def _start_dhan(self) -> None:
        from dhanhq import DhanContext, MarketFeed

        ctx = DhanContext(
            self._settings.dhan_client_id,
            self._settings.dhan_access_token,
        )
        instruments = self._dhan_instruments()

        def on_ticks(_feed: Any, data: dict[str, Any] | Any) -> None:
            if isinstance(data, dict):
                self._enqueue_tick(data)
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        self._enqueue_tick(item)

        def on_connect(_feed: Any) -> None:
            logger.info("Dhan WS connected; subscribed to %d NSE_FNO tokens", len(instruments))

        def on_close(_feed: Any) -> None:
            logger.warning("Dhan WS closed")

        def on_error(_feed: Any, error: Any) -> None:
            logger.error("Dhan WS error: %s", error)

        feed = MarketFeed(
            ctx,
            instruments,
            version="v2",
            on_connect=on_connect,
            on_ticks=on_ticks,
            on_close=on_close,
            on_error=on_error,
        )
        self._dhan_feed = feed
        # MarketFeed.run() already reconnects when the socket drops.
        self._feed_thread = feed.start()

    # --------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Start the publisher worker and broker WebSocket (non-blocking)."""
        if self._running.is_set():
            return

        self._running.set()
        self._publisher_thread = threading.Thread(
            target=self._publisher_loop,
            name="zmq-tick-publisher",
            daemon=True,
        )
        self._publisher_thread.start()

        logger.info(
            "Starting %s tick listener for %d tokens",
            self.broker,
            len(self.tokens),
        )
        if self.broker == "zerodha":
            self._start_zerodha()
        elif self.broker == "dhan":
            self._start_dhan()
        else:
            raise ValueError(f"Unsupported broker: {self.broker!r}")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the feed and drain/stop the publisher thread."""
        self._running.clear()

        if self._kite is not None:
            try:
                self._kite.close()
            except Exception:
                logger.exception("Error closing Zerodha ticker")
            self._kite = None

        if self._dhan_feed is not None:
            try:
                self._dhan_feed.close_connection()
            except Exception:
                logger.exception("Error closing Dhan market feed")
            self._dhan_feed = None

        if self._publisher_thread is not None:
            self._publisher_thread.join(timeout=timeout)
            self._publisher_thread = None

        if self._feed_thread is not None:
            self._feed_thread.join(timeout=timeout)
            self._feed_thread = None

    def run_forever(self) -> None:
        """Start and block until interrupted."""
        self.start()
        try:
            while self._running.is_set():
                time.sleep(1.0)
        except KeyboardInterrupt:
            logger.info("Interrupted; shutting down tick listener")
        finally:
            self.stop()


def start_tick_listener(
    tokens: Iterable[int | str],
    *,
    broker: BrokerName = "zerodha",
    mode: str = "full",
) -> TickListener:
    """Convenience: construct and start a listener."""
    listener = TickListener(tokens, broker=broker, mode=mode)
    listener.start()
    return listener


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="F&O tick listener -> ZeroMQ")
    parser.add_argument("--broker", choices=("zerodha", "dhan"), default="zerodha")
    parser.add_argument(
        "--tokens",
        required=True,
        help="Comma-separated active F&O instrument / security IDs",
    )
    parser.add_argument("--mode", choices=("ltp", "quote", "full"), default="full")
    args = parser.parse_args()

    token_list = [t.strip() for t in args.tokens.split(",") if t.strip()]
    start_tick_listener(token_list, broker=args.broker, mode=args.mode).run_forever()
