"""Batched TimescaleDB writer for fno_ticks (chunk size or time flush)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import psycopg2
from psycopg2.extras import execute_values

from config.settings import get_settings

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 100
DEFAULT_FLUSH_INTERVAL_SEC = 1.0

_INSERT_SQL = """
    INSERT INTO fno_ticks (time, token, last_price, volume, oi, iv, delta)
    VALUES %s
"""


@dataclass(frozen=True, slots=True)
class TickRow:
    time: datetime
    token: int
    last_price: float | None = None
    volume: int | None = None
    oi: int | None = None
    iv: float | None = None
    delta: float | None = None

    def as_tuple(self) -> tuple[Any, ...]:
        return (
            self.time,
            self.token,
            self.last_price,
            self.volume,
            self.oi,
            self.iv,
            self.delta,
        )


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Heuristic: ms vs seconds epoch.
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def tick_from_dict(data: dict[str, Any]) -> TickRow:
    """Normalize a raw / enriched tick dict into an ``fno_ticks`` row."""
    token = data.get("token")
    if token is None:
        token = data.get("instrument_token", data.get("security_id"))
    if token is None:
        raise ValueError("tick is missing token / instrument_token / security_id")

    last_price = data.get("last_price", data.get("ltp", data.get("LTP")))
    volume = data.get("volume", data.get("volume_traded", data.get("volume_traded_today")))
    oi = data.get("oi", data.get("open_interest", data.get("OI")))
    iv = data.get("iv")
    delta = data.get("delta")
    ts = data.get("time", data.get("timestamp", data.get("exchange_timestamp")))

    return TickRow(
        time=_parse_time(ts) if ts is not None else datetime.now(timezone.utc),
        token=int(token),
        last_price=float(last_price) if last_price is not None else None,
        volume=int(volume) if volume is not None else None,
        oi=int(oi) if oi is not None else None,
        iv=float(iv) if iv is not None else None,
        delta=float(delta) if delta is not None else None,
    )


class DbWriter:
    """
    Buffer ticks and flush to TimescaleDB in batches of ``batch_size``
    **or** at least every ``flush_interval_sec`` seconds — whichever comes first.
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval_sec: float = DEFAULT_FLUSH_INTERVAL_SEC,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if flush_interval_sec <= 0:
            raise ValueError("flush_interval_sec must be > 0")

        self.database_url = database_url or get_settings().database_url
        self.batch_size = batch_size
        self.flush_interval_sec = flush_interval_sec

        self._buffer: list[TickRow] = []
        self._lock = threading.Lock()
        self._flush_event = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: Any = None
        self._rows_written = 0
        self._flush_errors = 0

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._buffer)

    @property
    def rows_written(self) -> int:
        return self._rows_written

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._ensure_connection()
        self._thread = threading.Thread(
            target=self._run,
            name="timescale-db-writer",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "DbWriter started (batch_size=%d, flush_interval=%.2fs)",
            self.batch_size,
            self.flush_interval_sec,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._flush_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        # Final drain on the caller thread.
        self.flush()
        self._close_connection()

    def write_tick(self, tick: TickRow | dict[str, Any]) -> None:
        """Enqueue one tick (non-blocking aside from a short lock)."""
        row = tick if isinstance(tick, TickRow) else tick_from_dict(tick)
        should_flush = False
        with self._lock:
            self._buffer.append(row)
            should_flush = len(self._buffer) >= self.batch_size
        if should_flush:
            self._flush_event.set()

    def write_ticks(self, ticks: Iterable[TickRow | dict[str, Any]]) -> None:
        for tick in ticks:
            self.write_tick(tick)

    def flush(self) -> int:
        """Flush the current buffer immediately. Returns rows inserted."""
        with self._lock:
            if not self._buffer:
                return 0
            batch = self._buffer
            self._buffer = []

        try:
            inserted = self._insert_batch(batch)
            self._rows_written += inserted
            return inserted
        except Exception:
            self._flush_errors += 1
            # Put rows back so a transient outage does not drop data silently.
            with self._lock:
                self._buffer = batch + self._buffer
            logger.exception(
                "Failed to flush %d ticks to TimescaleDB (errors=%d)",
                len(batch),
                self._flush_errors,
            )
            raise

    def _run(self) -> None:
        while not self._stop.is_set():
            triggered = self._flush_event.wait(timeout=self.flush_interval_sec)
            self._flush_event.clear()
            try:
                self.flush()
            except Exception:
                # Already logged inside flush; keep the loop alive.
                time.sleep(min(1.0, self.flush_interval_sec))
            if triggered and self._stop.is_set():
                break

    def _ensure_connection(self) -> Any:
        if self._conn is not None and self._conn.closed == 0:
            return self._conn
        self._conn = psycopg2.connect(self.database_url)
        self._conn.autocommit = False
        return self._conn

    def _close_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                logger.exception("Error closing TimescaleDB connection")
            self._conn = None

    def _insert_batch(self, rows: Sequence[TickRow]) -> int:
        if not rows:
            return 0

        values = [row.as_tuple() for row in rows]
        conn = self._ensure_connection()
        try:
            with conn.cursor() as cur:
                execute_values(cur, _INSERT_SQL, values, page_size=len(values))
            conn.commit()
            logger.debug("Inserted %d rows into fno_ticks", len(values))
            return len(values)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                self._close_connection()
            raise


def get_db_writer(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    flush_interval_sec: float = DEFAULT_FLUSH_INTERVAL_SEC,
) -> DbWriter:
    writer = DbWriter(
        batch_size=batch_size,
        flush_interval_sec=flush_interval_sec,
    )
    writer.start()
    return writer
