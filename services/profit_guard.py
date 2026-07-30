"""Profit booking rules for long option sleeves (deterministic, no LLM).

Aims for consistent winners by:
- Hard take-profit at a modest premium upside (default +28%)
- Trailing lock once unrealized reaches arm threshold (default +15%)
- Never loosening the protective stop once raised
"""

from __future__ import annotations

import os
from typing import Any


# Default: book ~1R-ish vs stop (~35% stop → ~28% take keeps asymmetric but realistic)
TAKE_PROFIT_FRAC = 0.28
TRAIL_ARM_FRAC = 0.15
# Once trailing, give back at most this fraction of peak unrealized gain
TRAIL_GIVEBACK_FRAC = 0.40
# After trail arms, floor stop at +2% over entry (breakeven+)
TRAIL_BE_BUFFER = 0.02


def _env_frac(name: str, default: float, *, lo: float, hi: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return min(hi, max(lo, float(raw)))
    except ValueError:
        return default


def take_profit_fraction() -> float:
    return _env_frac("TRADE_TAKE_PROFIT_FRAC", TAKE_PROFIT_FRAC, lo=0.10, hi=0.80)


def trail_arm_fraction() -> float:
    return _env_frac("TRADE_TRAIL_ARM_FRAC", TRAIL_ARM_FRAC, lo=0.05, hi=0.50)


def trail_giveback_fraction() -> float:
    return _env_frac("TRADE_TRAIL_GIVEBACK_FRAC", TRAIL_GIVEBACK_FRAC, lo=0.10, hi=0.80)


def target_price(entry: float, *, tp_frac: float | None = None) -> float:
    frac = take_profit_fraction() if tp_frac is None else float(tp_frac)
    return round(float(entry) * (1.0 + frac), 2)


def trail_arm_price(entry: float, *, arm_frac: float | None = None) -> float:
    frac = trail_arm_fraction() if arm_frac is None else float(arm_frac)
    return round(float(entry) * (1.0 + frac), 2)


def evaluate_long_premium(
    *,
    entry: float,
    ltp: float,
    peak_ltp: float | None,
    stop_price: float | None,
    tp_frac: float | None = None,
    arm_frac: float | None = None,
    giveback_frac: float | None = None,
) -> dict[str, Any]:
    """
    Update peak / trailing stop for a long option premium.

    Returns keys:
      peak_ltp, stop_price, target_price, trail_armed,
      exit_reason (TAKE_PROFIT | TRAIL_EXIT | STOP | None),
      unrealized_pct
    """
    entry_f = float(entry)
    ltp_f = float(ltp)
    if entry_f <= 0 or ltp_f <= 0:
        return {
            "peak_ltp": peak_ltp,
            "stop_price": stop_price,
            "target_price": None,
            "trail_armed": False,
            "exit_reason": None,
            "unrealized_pct": None,
        }

    tp = take_profit_fraction() if tp_frac is None else float(tp_frac)
    arm = trail_arm_fraction() if arm_frac is None else float(arm_frac)
    give = trail_giveback_fraction() if giveback_frac is None else float(giveback_frac)

    peak = max(float(peak_ltp) if peak_ltp is not None else entry_f, ltp_f, entry_f)
    tgt = target_price(entry_f, tp_frac=tp)
    arm_px = trail_arm_price(entry_f, arm_frac=arm)
    stop = float(stop_price) if stop_price is not None else None
    unrealized_pct = (ltp_f - entry_f) / entry_f
    trail_armed = peak >= arm_px

    if trail_armed:
        locked = entry_f + (peak - entry_f) * (1.0 - give)
        floor = entry_f * (1.0 + TRAIL_BE_BUFFER)
        trail_stop = max(floor, locked)
        if stop is None or trail_stop > stop:
            stop = round(trail_stop, 2)

    exit_reason: str | None = None
    if ltp_f >= tgt:
        exit_reason = "TAKE_PROFIT"
    elif stop is not None and ltp_f <= stop:
        exit_reason = "TRAIL_EXIT" if trail_armed and ltp_f > entry_f * 0.98 else "STOP"

    return {
        "peak_ltp": round(peak, 2),
        "stop_price": None if stop is None else round(float(stop), 2),
        "target_price": tgt,
        "trail_arm_price": arm_px,
        "trail_armed": trail_armed,
        "exit_reason": exit_reason,
        "unrealized_pct": round(unrealized_pct * 100.0, 2),
    }
