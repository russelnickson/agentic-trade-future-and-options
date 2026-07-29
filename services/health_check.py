"""Tick-path latency health check: exchange_timestamp → server receipt_time."""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LATENCY_THRESHOLD_MS = 500.0


def _to_utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        # Heuristic: ns / ms / seconds.
        if ts > 1e16:
            ts /= 1e9
        elif ts > 1e12:
            ts /= 1e3
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def extract_exchange_timestamp(tick: dict[str, Any]) -> datetime | None:
    for key in (
        "exchange_timestamp",
        "exchange_time",
        "exch_timestamp",
        "timestamp",
        "LTT",
        "ltt",
    ):
        if key in tick and tick[key] is not None:
            return _to_utc_datetime(tick[key])
    return None


def extract_receipt_time(tick: dict[str, Any]) -> datetime:
    for key in ("receipt_time", "server_receipt_time", "received_at"):
        if key in tick and tick[key] is not None:
            parsed = _to_utc_datetime(tick[key])
            if parsed is not None:
                return parsed
    return datetime.now(timezone.utc)


def measure_tick_latency_ms(
    tick: dict[str, Any],
    *,
    receipt_time: datetime | None = None,
) -> float | None:
    """
    Latency in milliseconds between exchange_timestamp and server receipt_time.

    Returns None when the exchange timestamp is missing or unparsable.
    """
    exchange_ts = extract_exchange_timestamp(tick)
    if exchange_ts is None:
        return None
    received = receipt_time or extract_receipt_time(tick)
    return (received - exchange_ts).total_seconds() * 1000.0


@dataclass
class LatencySample:
    token: int | str | None
    latency_ms: float
    exchange_timestamp: datetime
    receipt_time: datetime
    alerted: bool


@dataclass
class HealthCheck:
    """
    Observe ticks and alert (console logging) when delay exceeds ``threshold_ms``.

    Default threshold: **500ms**.
    """

    threshold_ms: float = DEFAULT_LATENCY_THRESHOLD_MS
    window_size: int = 1_000
    _latencies_ms: list[float] = field(default_factory=list, repr=False)
    _alert_count: int = 0
    _sample_count: int = 0
    _missing_exchange_ts: int = 0

    def observe(self, tick: dict[str, Any]) -> LatencySample | None:
        """
        Measure one tick's latency. Logs a WARNING when delay > threshold.

        Stamps ``receipt_time`` on the tick dict when absent so downstream
        consumers share the same receipt clock.
        """
        if "receipt_time" not in tick or tick["receipt_time"] is None:
            tick["receipt_time"] = datetime.now(timezone.utc)

        exchange_ts = extract_exchange_timestamp(tick)
        receipt_time = extract_receipt_time(tick)
        if exchange_ts is None:
            self._missing_exchange_ts += 1
            return None

        latency_ms = (receipt_time - exchange_ts).total_seconds() * 1000.0
        self._sample_count += 1
        self._latencies_ms.append(latency_ms)
        if len(self._latencies_ms) > self.window_size:
            self._latencies_ms.pop(0)

        token = tick.get("instrument_token", tick.get("token", tick.get("security_id")))
        alerted = latency_ms > self.threshold_ms
        if alerted:
            self._alert_count += 1
            logger.warning(
                "TICK_LATENCY_ALERT token=%s delay=%.1fms threshold=%.0fms "
                "exchange_timestamp=%s receipt_time=%s",
                token,
                latency_ms,
                self.threshold_ms,
                exchange_ts.isoformat(),
                receipt_time.isoformat(),
            )

        return LatencySample(
            token=token,
            latency_ms=latency_ms,
            exchange_timestamp=exchange_ts,
            receipt_time=receipt_time,
            alerted=alerted,
        )

    def observe_many(self, ticks: list[dict[str, Any]]) -> list[LatencySample]:
        samples: list[LatencySample] = []
        for tick in ticks:
            sample = self.observe(tick)
            if sample is not None:
                samples.append(sample)
        return samples

    def stats(self) -> dict[str, Any]:
        vals = self._latencies_ms
        if not vals:
            return {
                "samples": self._sample_count,
                "alerts": self._alert_count,
                "missing_exchange_timestamp": self._missing_exchange_ts,
                "threshold_ms": self.threshold_ms,
                "window": 0,
                "avg_ms": None,
                "p50_ms": None,
                "p95_ms": None,
                "max_ms": None,
            }

        ordered = sorted(vals)
        p95_idx = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
        return {
            "samples": self._sample_count,
            "alerts": self._alert_count,
            "missing_exchange_timestamp": self._missing_exchange_ts,
            "threshold_ms": self.threshold_ms,
            "window": len(vals),
            "avg_ms": statistics.fmean(vals),
            "p50_ms": statistics.median(vals),
            "p95_ms": ordered[p95_idx],
            "max_ms": max(vals),
        }

    def log_summary(self) -> None:
        s = self.stats()
        logger.info(
            "Tick latency summary samples=%s alerts=%s avg_ms=%s p50_ms=%s "
            "p95_ms=%s max_ms=%s missing_exchange_ts=%s",
            s["samples"],
            s["alerts"],
            None if s["avg_ms"] is None else f"{s['avg_ms']:.1f}",
            None if s["p50_ms"] is None else f"{s['p50_ms']:.1f}",
            None if s["p95_ms"] is None else f"{s['p95_ms']:.1f}",
            None if s["max_ms"] is None else f"{s['max_ms']:.1f}",
            s["missing_exchange_timestamp"],
        )


# Module-level helper for one-off checks.
_default_checker = HealthCheck()


def check_tick_latency(
    tick: dict[str, Any],
    *,
    threshold_ms: float = DEFAULT_LATENCY_THRESHOLD_MS,
) -> LatencySample | None:
    """Convenience wrapper using a process-wide HealthCheck instance."""
    if _default_checker.threshold_ms != threshold_ms:
        _default_checker.threshold_ms = threshold_ms
    return _default_checker.observe(tick)
