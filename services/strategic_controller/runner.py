"""Periodic runner for the LangGraph strategic controller."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from services.strategic_controller.graph import run_strategic_cycle

logger = logging.getLogger(__name__)


def strategic_interval_sec() -> float:
    raw = (os.getenv("STRATEGIC_INTERVAL_SEC") or "120").strip()
    try:
        return max(30.0, float(raw))
    except ValueError:
        return 120.0


def run_forever(symbol: str = "NIFTY", *, interval_sec: float | None = None) -> None:
    interval = interval_sec if interval_sec is not None else strategic_interval_sec()
    logger.info(
        "Strategic LangGraph controller started (symbol=%s, interval=%.0fs) — no order placement",
        symbol,
        interval,
    )
    while True:
        started = time.perf_counter()
        try:
            state = run_strategic_cycle(symbol)
            d = state.get("directive") or {}
            logger.info(
                "Strategic cycle · regime=%s sentiment=%s stance=%s side=%s allow=%s · %s",
                d.get("regime"),
                d.get("sentiment"),
                d.get("stance"),
                d.get("preferred_side"),
                (d.get("risk") or {}).get("allow_new_entries"),
                d.get("strategy_hint"),
            )
        except Exception:
            logger.exception("Strategic cycle failed")
        elapsed = time.perf_counter() - started
        time.sleep(max(1.0, interval - elapsed))


def run_once(symbol: str = "NIFTY") -> dict[str, Any]:
    return run_strategic_cycle(symbol)
