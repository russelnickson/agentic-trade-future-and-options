"""System clock sync check against NTP (IST trading context)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import ntplib

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_NTP_HOST = "time.google.com"
DEFAULT_MAX_DRIFT_MS = 50.0


class SystemLaunchWarning(RuntimeError):
    """Raised when clock drift is too large to safely launch the trading stack."""


@dataclass(frozen=True)
class ClockSyncResult:
    ntp_host: str
    drift_ms: float
    system_utc: datetime
    ntp_utc: datetime
    system_ist: datetime
    ntp_ist: datetime
    within_tolerance: bool
    max_drift_ms: float

    def to_dict(self) -> dict[str, object]:
        return {
            "ntp_host": self.ntp_host,
            "drift_ms": self.drift_ms,
            "system_utc": self.system_utc.isoformat(),
            "ntp_utc": self.ntp_utc.isoformat(),
            "system_ist": self.system_ist.isoformat(),
            "ntp_ist": self.ntp_ist.isoformat(),
            "within_tolerance": self.within_tolerance,
            "max_drift_ms": self.max_drift_ms,
        }


def measure_clock_drift(
    ntp_host: str = DEFAULT_NTP_HOST,
    *,
    timeout: float = 5.0,
    version: int = 3,
    max_drift_ms: float = DEFAULT_MAX_DRIFT_MS,
) -> ClockSyncResult:
    """
    Compare local system time to an NTP reference used for IST trading hours.

    NTP payloads are UTC; IST (Asia/Kolkata) is derived for operator-facing logs.
    ``drift_ms`` is the signed NTP offset in milliseconds (positive ⇒ local clock
    is behind the server / must step forward to match).
    """
    client = ntplib.NTPClient()
    response = client.request(ntp_host, version=version, timeout=timeout)

    # ntplib offset already compensates for network delay (RFC 5905).
    drift_ms = float(response.offset) * 1000.0
    ntp_epoch = float(response.tx_time)
    local_epoch = ntp_epoch - float(response.offset)

    system_utc = datetime.fromtimestamp(local_epoch, tz=timezone.utc)
    ntp_utc = datetime.fromtimestamp(ntp_epoch, tz=timezone.utc)

    return ClockSyncResult(
        ntp_host=ntp_host,
        drift_ms=drift_ms,
        system_utc=system_utc,
        ntp_utc=ntp_utc,
        system_ist=system_utc.astimezone(IST),
        ntp_ist=ntp_utc.astimezone(IST),
        within_tolerance=abs(drift_ms) <= max_drift_ms,
        max_drift_ms=max_drift_ms,
    )


def check_clock_sync(
    *,
    ntp_host: str = DEFAULT_NTP_HOST,
    max_drift_ms: float = DEFAULT_MAX_DRIFT_MS,
    timeout: float = 5.0,
) -> ClockSyncResult:
    """
    Measure drift and enforce the launch threshold.

    If absolute drift exceeds ``max_drift_ms`` (default **50ms**), logs an error
    and raises :class:`SystemLaunchWarning`.
    """
    result = measure_clock_drift(
        ntp_host, timeout=timeout, max_drift_ms=max_drift_ms
    )

    logger.info(
        "Clock sync vs %s: drift=%.3fms (limit=%.1fms) system_ist=%s ntp_ist=%s",
        result.ntp_host,
        result.drift_ms,
        result.max_drift_ms,
        result.system_ist.isoformat(),
        result.ntp_ist.isoformat(),
    )

    if not result.within_tolerance:
        msg = (
            f"SYSTEM LAUNCH WARNING: clock drift {result.drift_ms:.3f}ms exceeds "
            f"{max_drift_ms:.1f}ms vs IST reference NTP host {ntp_host} "
            f"(system_ist={result.system_ist.isoformat()}, "
            f"ntp_ist={result.ntp_ist.isoformat()}). "
            "Correct system time before starting the trading engine."
        )
        logger.error(msg)
        raise SystemLaunchWarning(msg)

    return result


def ensure_clock_synced(
    *,
    ntp_host: str = DEFAULT_NTP_HOST,
    max_drift_ms: float = DEFAULT_MAX_DRIFT_MS,
) -> ClockSyncResult:
    """Alias used by process supervisors before launch."""
    return check_clock_sync(ntp_host=ntp_host, max_drift_ms=max_drift_ms)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        sync = check_clock_sync()
        print(sync.to_dict())
    except SystemLaunchWarning as exc:
        print(f"FAILED: {exc}")
        raise SystemExit(1) from exc
