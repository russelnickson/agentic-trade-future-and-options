"""India-local (IST) display times — Today / Yesterday when applicable."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _is_humanized(value: str) -> bool:
    s = value.strip()
    if s.startswith(("Today ", "Yesterday ", "Today", "Yesterday")):
        return True
    if s[0:1].isdigit() and any(m in s for m in _MONTHS) and (":" in s or ", " in s or " " in s):
        # e.g. "28 Jul, 11:40" or "28 Jul 2025, 11:40" or "28 Jul"
        if "-" not in s[:12]:  # not ISO date
            return True
    return False


def to_ist(value: Any) -> datetime | None:
    """Parse assorted timestamp values into an aware IST datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # ms
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(IST)
    s = str(value).strip()
    if not s or s in {"—", "-", "None", "nan"}:
        return None
    if _is_humanized(s):
        return None
    # Date-only → IST calendar day (no UTC shift)
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            d = date.fromisoformat(s)
            return datetime(d.year, d.month, d.day, tzinfo=IST)
        except ValueError:
            pass
    # Pandas / ISO-ish (handles "2026-07-27 18:30:00+00:00")
    try:
        import pandas as pd

        parsed = pd.to_datetime(s, utc=True, errors="coerce")
        if parsed is not None and not pd.isna(parsed):
            py = parsed.to_pydatetime()
            if isinstance(py, datetime):
                if py.tzinfo is None:
                    py = py.replace(tzinfo=timezone.utc)
                return py.astimezone(IST)
    except Exception:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y",
    ):
        try:
            raw = s.replace("Z", "+0000") if "Z" in s and "%z" in fmt else s
            # Normalize +00:00 → +0000 for strptime %z
            if "%z" in fmt and len(raw) >= 6 and raw[-3] == ":":
                raw = raw[:-3] + raw[-2:]
            dt = datetime.strptime(
                raw[:32] if "%f" in fmt else raw[:25] if "%z" in fmt else raw,
                fmt,
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(IST)
        except ValueError:
            continue
    return None


def format_ist(
    value: Any,
    *,
    with_time: bool = True,
    seconds: bool = False,
    relative_day: bool = True,
) -> str:
    """Human IST stamp, e.g. ``Today 09:15``, ``Yesterday 14:02``, ``27 Jul, 11:40``.

    Older years include the year: ``28 Jul 2025, 11:40``.
    """
    if value is None or value == "":
        return "—"
    if isinstance(value, str) and _is_humanized(value):
        return value.strip()

    dt = to_ist(value)
    if dt is None:
        return "—"

    today = datetime.now(IST).date()
    d = dt.date()
    time_fmt = "%H:%M:%S" if seconds else "%H:%M"
    clock = dt.strftime(time_fmt)

    if relative_day:
        if d == today:
            day = "Today"
        elif d == date.fromordinal(today.toordinal() - 1):
            day = "Yesterday"
        elif d.year == today.year:
            day = f"{dt.day} {dt.strftime('%b')}"
        else:
            day = f"{dt.day} {dt.strftime('%b %Y')}"
    else:
        if d.year == today.year:
            day = f"{dt.day} {dt.strftime('%b')}"
        else:
            day = f"{dt.day} {dt.strftime('%b %Y')}"

    if not with_time:
        return day
    if relative_day and day in {"Today", "Yesterday"}:
        return f"{day} {clock}"
    return f"{day}, {clock}"


def format_ist_series(values, **kwargs) -> list[str]:
    return [format_ist(v, **kwargs) for v in values]
