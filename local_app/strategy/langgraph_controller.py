"""LangGraph controller — market regime → risk → authorize remote dispatch.

No order signal may reach EC2 without passing ``assess_market_regime``,
``evaluate_risk``, and ``authorize_execution``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Literal, TypedDict
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

Regime = Literal["CALM", "TRENDING", "ELEVATED_VOL", "EXTREME_VOL", "UNKNOWN"]


class ControllerState(TypedDict, total=False):
    # Input signal
    symbol: str
    action: str
    qty: int
    order_type: str
    exchange: str
    price: float
    # Context overrides (optional)
    day_pnl: float | None
    open_positions: int | None
    india_vix: float | None
    trend_strength: float | None
    # assess_market_regime
    regime: Regime
    regime_ok: bool
    regime_reason: str
    # evaluate_risk
    risk_ok: bool
    risk_reason: str
    trailing_sl_ok: bool
    # authorize_execution
    authorized: bool
    abort_reason: str
    dispatch_result: dict[str, Any]
    asof: str


def _fenv(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def _ienv(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _load_india_vix() -> float | None:
    """Best-effort India VIX from cached Global Outlook markers."""
    try:
        from services.global_outlook import load_markers_table

        df = load_markers_table()
        if df is None or df.empty:
            return None
        # expect columns like symbol/name + last/ltp
        for col_sym in ("symbol", "key", "name", "marker"):
            if col_sym not in df.columns:
                continue
            mask = df[col_sym].astype(str).str.upper().str.contains("INDIA.?VIX|INDIA_VIX", regex=True)
            rows = df.loc[mask]
            if rows.empty:
                continue
            for price_col in ("last", "ltp", "close", "value", "price"):
                if price_col in rows.columns:
                    val = rows.iloc[0][price_col]
                    if val is not None and str(val) not in {"", "nan", "None"}:
                        return float(val)
        return None
    except Exception:
        logger.debug("India VIX load failed", exc_info=True)
        return None


def _load_trend_strength() -> float:
    """Proxy trend strength from outlook score (−1..+1 scaled)."""
    try:
        from services.global_outlook import load_snapshot

        snap = load_snapshot()
        if snap is None:
            return 0.0
        score = float(getattr(snap, "score", 0) or 0)
        # Clamp to roughly −1..+1 for controller thresholds
        return max(-1.0, min(1.0, score / 5.0))
    except Exception:
        return 0.0


def assess_market_regime(state: ControllerState) -> ControllerState:
    """Check India VIX and current trend strength."""
    vix = state.get("india_vix")
    if vix is None:
        vix = _load_india_vix()
    trend = state.get("trend_strength")
    if trend is None:
        trend = _load_trend_strength()

    vix_f = float(vix) if vix is not None else None
    trend_f = float(trend or 0.0)
    vix_elevated = _fenv("REGIME_VIX_ELEVATED", 18.0)
    vix_extreme = _fenv("REGIME_VIX_EXTREME", 28.0)
    min_trend = _fenv("REGIME_MIN_TREND", 0.15)

    if vix_f is None:
        regime: Regime = "UNKNOWN"
        ok = _ienv("REGIME_ALLOW_UNKNOWN", 0) == 1
        reason = "India VIX unavailable — refuse new risk unless REGIME_ALLOW_UNKNOWN=1"
    elif vix_f >= vix_extreme:
        regime = "EXTREME_VOL"
        ok = False
        reason = f"India VIX {vix_f:.2f} ≥ extreme {vix_extreme:.1f} — block entries"
    elif vix_f >= vix_elevated:
        regime = "ELEVATED_VOL"
        # Allow only strong trend continuation
        ok = abs(trend_f) >= min_trend * 1.5
        reason = (
            f"Elevated VIX {vix_f:.2f}; trend={trend_f:+.2f} "
            f"{'ok' if ok else 'too weak for elevated vol'}"
        )
    elif abs(trend_f) >= min_trend:
        regime = "TRENDING"
        ok = True
        reason = f"Trending tape trend={trend_f:+.2f}, VIX={vix_f:.2f}"
    else:
        regime = "CALM"
        ok = True
        reason = f"Calm/range regime VIX={vix_f:.2f}, trend={trend_f:+.2f}"

    logger.info("assess_market_regime · %s · ok=%s · %s", regime, ok, reason)
    return {
        **state,
        "india_vix": vix_f,
        "trend_strength": trend_f,
        "regime": regime,
        "regime_ok": ok,
        "regime_reason": reason,
        "asof": datetime.now(IST).isoformat(),
    }


def evaluate_risk(state: ControllerState) -> ControllerState:
    """Verify max daily drawdown, open position limits, trailing SL rules."""
    if not state.get("regime_ok", False):
        return {
            **state,
            "risk_ok": False,
            "risk_reason": f"Skipped risk sizing — regime blocked: {state.get('regime_reason')}",
            "trailing_sl_ok": False,
        }

    max_dd = _fenv("MAX_DAILY_LOSS", 5000.0)
    max_positions = _ienv("MAX_OPEN_POSITIONS", 3)
    day_pnl = state.get("day_pnl")
    if day_pnl is None:
        try:
            day_pnl = float(os.getenv("DAY_PNL_OVERRIDE") or 0)
        except ValueError:
            day_pnl = 0.0

    open_n = state.get("open_positions")
    if open_n is None:
        try:
            from local_app.remote_client import get_client

            pos = get_client().get_positions()
            open_n = int(pos.get("count") or len(pos.get("positions") or []))
        except Exception:
            open_n = 0
            logger.warning("evaluate_risk · could not fetch positions; assuming 0 open")

    reasons: list[str] = []
    ok = True

    if day_pnl is not None and float(day_pnl) <= -abs(max_dd):
        ok = False
        reasons.append(f"daily drawdown ₹{day_pnl:,.0f} ≤ -MAX_DAILY_LOSS ₹{max_dd:,.0f}")
    elif day_pnl is not None and float(day_pnl) <= -0.8 * abs(max_dd):
        ok = False
        reasons.append(f"approaching day loss (₹{day_pnl:,.0f} / limit ₹{max_dd:,.0f})")

    if int(open_n) >= max_positions:
        ok = False
        reasons.append(f"open positions {open_n} ≥ MAX_OPEN_POSITIONS {max_positions}")

    # Trailing SL: require env flag that tactical stop book is armed, or default allow
    trailing_required = _ienv("REQUIRE_TRAILING_SL", 1) == 1
    trailing_armed = _ienv("TRAILING_SL_ARMED", 1) == 1
    trailing_ok = (not trailing_required) or trailing_armed
    if not trailing_ok:
        ok = False
        reasons.append("trailing SL not armed (set TRAILING_SL_ARMED=1)")

    qty = int(state.get("qty") or 0)
    max_qty = _ienv("MAX_ORDER_QTY", 130)
    if qty <= 0 or qty > max_qty:
        ok = False
        reasons.append(f"qty {qty} outside (0, {max_qty}]")

    reason = "; ".join(reasons) if reasons else "risk checks passed"
    logger.info("evaluate_risk · ok=%s · %s", ok, reason)
    return {
        **state,
        "day_pnl": float(day_pnl) if day_pnl is not None else None,
        "open_positions": int(open_n),
        "risk_ok": ok,
        "risk_reason": reason,
        "trailing_sl_ok": trailing_ok,
    }


def authorize_execution(state: ControllerState) -> ControllerState:
    """Approve signal for remote_client dispatch or abort with reason logging."""
    if not state.get("regime_ok", False):
        reason = state.get("regime_reason") or "regime blocked"
        logger.warning("authorize_execution ABORT · %s", reason)
        return {
            **state,
            "authorized": False,
            "abort_reason": reason,
            "dispatch_result": {"ok": False, "aborted": True, "reason": reason},
        }
    if not state.get("risk_ok", False):
        reason = state.get("risk_reason") or "risk blocked"
        logger.warning("authorize_execution ABORT · %s", reason)
        return {
            **state,
            "authorized": False,
            "abort_reason": reason,
            "dispatch_result": {"ok": False, "aborted": True, "reason": reason},
        }

    symbol = str(state.get("symbol") or "").strip().upper()
    action = str(state.get("action") or "").strip().upper()
    qty = int(state.get("qty") or 0)
    order_type = str(state.get("order_type") or "LIMIT")
    dry_run = (os.getenv("STRATEGY_DRY_RUN") or "").strip().lower() in {"1", "true", "yes"}

    if dry_run:
        result = {
            "ok": True,
            "dry_run": True,
            "order": {
                "symbol": symbol,
                "action": action,
                "qty": qty,
                "order_type": order_type,
                "status": "AUTHORIZED_DRY_RUN",
            },
        }
        logger.info("authorize_execution DRY-RUN · %s %s x%s", action, symbol, qty)
        return {
            **state,
            "authorized": True,
            "abort_reason": "",
            "dispatch_result": result,
        }

    try:
        from local_app.remote_client import get_client

        result = get_client().send_order(
            symbol=symbol,
            action=action,
            qty=qty,
            order_type=order_type,
        )
        logger.info(
            "authorize_execution DISPATCHED · %s %s x%s → %s",
            action,
            symbol,
            qty,
            (result.get("order") or {}).get("order_id") or result.get("ok"),
        )
        return {
            **state,
            "authorized": True,
            "abort_reason": "",
            "dispatch_result": result,
        }
    except Exception as exc:
        logger.exception("authorize_execution DISPATCH FAILED")
        return {
            **state,
            "authorized": False,
            "abort_reason": str(exc),
            "dispatch_result": {"ok": False, "error": str(exc)},
        }


def build_controller_graph():
    """Compile: START → regime → risk → authorize → END."""
    g = StateGraph(ControllerState)
    g.add_node("assess_market_regime", assess_market_regime)
    g.add_node("evaluate_risk", evaluate_risk)
    g.add_node("authorize_execution", authorize_execution)
    g.add_edge(START, "assess_market_regime")
    g.add_edge("assess_market_regime", "evaluate_risk")
    g.add_edge("evaluate_risk", "authorize_execution")
    g.add_edge("authorize_execution", END)
    return g.compile()


_GRAPH = None


def get_controller_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_controller_graph()
    return _GRAPH


def run_controller(signal: dict[str, Any]) -> ControllerState:
    """Run one signal through the full risk gate."""
    graph = get_controller_graph()
    initial: ControllerState = {
        "symbol": str(signal.get("symbol") or ""),
        "action": str(signal.get("action") or ""),
        "qty": int(signal.get("qty") or 0),
        "order_type": str(signal.get("order_type") or "LIMIT"),
        "exchange": str(signal.get("exchange") or "NFO"),
        "price": float(signal.get("price") or 0),
        "asof": datetime.now(IST).isoformat(),
    }
    for key in ("day_pnl", "open_positions", "india_vix", "trend_strength"):
        if key in signal and signal[key] is not None:
            initial[key] = signal[key]  # type: ignore[literal-required]
    return dict(graph.invoke(initial))  # type: ignore[return-value]
