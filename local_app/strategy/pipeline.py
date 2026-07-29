"""Strategy execution pipeline — sole entry for order signals from local engine.

Every signal must pass the LangGraph controller (regime → risk → authorize)
before ``remote_client`` may talk to the EC2 worker.
"""

from __future__ import annotations

import logging
from typing import Any

from local_app.strategy.langgraph_controller import run_controller

logger = logging.getLogger(__name__)


class SignalRejected(RuntimeError):
    """Raised when the LangGraph controller aborts a signal."""

    def __init__(self, reason: str, state: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.state = state or {}


def execute_signal(
    symbol: str,
    action: str,
    qty: int,
    order_type: str = "LIMIT",
    *,
    exchange: str = "NFO",
    price: float = 0.0,
    day_pnl: float | None = None,
    open_positions: int | None = None,
    india_vix: float | None = None,
    trend_strength: float | None = None,
    raise_on_reject: bool = False,
) -> dict[str, Any]:
    """Gate + dispatch. Returns controller result including ``dispatch_result``."""
    signal: dict[str, Any] = {
        "symbol": symbol,
        "action": action,
        "qty": qty,
        "order_type": order_type,
        "exchange": exchange,
        "price": price,
    }
    if day_pnl is not None:
        signal["day_pnl"] = day_pnl
    if open_positions is not None:
        signal["open_positions"] = open_positions
    if india_vix is not None:
        signal["india_vix"] = india_vix
    if trend_strength is not None:
        signal["trend_strength"] = trend_strength

    state = run_controller(signal)
    result = state.get("dispatch_result") or {}
    if not state.get("authorized"):
        reason = state.get("abort_reason") or result.get("reason") or "signal rejected"
        logger.warning("pipeline reject · %s %s x%s · %s", action, symbol, qty, reason)
        if raise_on_reject:
            raise SignalRejected(str(reason), dict(state))
        return {
            "ok": False,
            "aborted": True,
            "reason": reason,
            "regime": state.get("regime"),
            "risk_reason": state.get("risk_reason"),
            "controller": state,
        }

    return {
        "ok": True,
        "aborted": False,
        "regime": state.get("regime"),
        "dispatch_result": result,
        "controller": {
            "regime": state.get("regime"),
            "regime_reason": state.get("regime_reason"),
            "risk_reason": state.get("risk_reason"),
            "india_vix": state.get("india_vix"),
            "trend_strength": state.get("trend_strength"),
        },
    }
