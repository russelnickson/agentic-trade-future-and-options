"""LangGraph strategic controller — regime, sentiment, risk limits (no orders).

Runs on a slow cadence (default every few minutes). Publishes a StrategyDirective
to Redis for the deterministic tactical executor.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph

from services.strategic_controller.directive import (
    RiskLimits,
    StrategyDirective,
    publish_directive,
)

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class StrategicState(TypedDict, total=False):
    symbol: str
    asof: str
    # gather
    outlook_bias: str
    outlook_score: float
    pcr: float | None
    underlying_ltp: float | None
    atm: float | None
    chain_live: bool
    voices_week: int
    speculation_signals: dict[str, Any]
    top_strategy_id: str
    top_strategy_name: str
    top_strategy_confidence: float | None
    day_pnl: float | None
    max_daily_loss: float
    trading_disabled: bool
    kill_switch: bool
    deployed_risk: float
    # classify
    regime: str
    sentiment: str
    sentiment_score: float
    stance: str
    preferred_side: str
    strategy_hint: str
    confidence: float
    risk: dict[str, Any]
    directive: dict[str, Any]
    error: str


def _max_daily_loss() -> float:
    try:
        return max(500.0, float(os.getenv("MAX_DAILY_LOSS") or 5000))
    except ValueError:
        return 5000.0


def gather_market(state: StrategicState) -> StrategicState:
    """Collect factual snapshots — no LLM, no orders."""
    symbol = (state.get("symbol") or "NIFTY").upper()
    out: StrategicState = {
        "symbol": symbol,
        "asof": datetime.now(IST).isoformat(),
        "max_daily_loss": _max_daily_loss(),
        "chain_live": False,
        "voices_week": 0,
        "speculation_signals": {},
        "outlook_bias": "",
        "outlook_score": 0.0,
        "trading_disabled": False,
        "kill_switch": False,
        "deployed_risk": 0.0,
    }
    try:
        from database.redis_client import get_redis_client
        from dashboard.components.risk_controls import is_trading_disabled, load_terminal_controls
        from services.intraday_hunt import load_day_risk, top_strategy_hint
        from services.oi_tracker import compute_pcr

        client = get_redis_client()
        controls = load_terminal_controls(client)
        out["kill_switch"] = bool(controls.get("kill_switch"))
        out["trading_disabled"] = bool(
            is_trading_disabled(controls, client=client) or controls.get("trading_disabled")
        )

        chain = client.get_option_chain_state(symbol) or {}
        call_oi = put_oi = 0
        for sides in (chain.get("strikes") or {}).values():
            if not isinstance(sides, dict):
                continue
            ce = (sides.get("CE") or {}).get("oi")
            pe = (sides.get("PE") or {}).get("oi")
            if ce is not None:
                call_oi += int(ce)
            if pe is not None:
                put_oi += int(pe)
        pcr = compute_pcr(call_oi, put_oi)
        out["pcr"] = pcr
        out["underlying_ltp"] = chain.get("underlying_ltp")
        out["atm"] = chain.get("atm")
        out["chain_live"] = bool(chain.get("strikes") and chain.get("underlying_ltp") is not None)

        day_risk = load_day_risk(client)
        out["deployed_risk"] = float(day_risk.get("deployed_risk") or 0.0)

        sid, sname, sconf = top_strategy_hint(client)
        out["top_strategy_id"] = sid
        out["top_strategy_name"] = sname
        out["top_strategy_confidence"] = sconf
    except Exception as exc:
        logger.exception("gather_market redis/chain failed")
        out["error"] = f"gather:{exc}"

    try:
        from services.global_outlook import load_snapshot

        snap = load_snapshot()
        if snap:
            out["outlook_bias"] = str(getattr(snap, "bias", "") or "")
            out["outlook_score"] = float(getattr(snap, "score", 0) or 0)
    except Exception as exc:
        out["error"] = (out.get("error") or "") + f" outlook:{exc}"

    try:
        from services.live_market_voices import load_snapshot as load_voices_snap

        voices = load_voices_snap()
        if voices:
            counts = getattr(voices, "counts_by_horizon", None) or {}
            out["voices_week"] = int(counts.get("week") or counts.get("day") or 0)
    except Exception:
        pass

    try:
        from dashboard.components.broker_speculation import load_speculation

        spec = load_speculation()
        if spec:
            out["speculation_signals"] = dict(spec.signals or {})
    except Exception:
        pass

    try:
        from dashboard.components.positions import fetch_positions
        from config.settings import get_settings

        broker = (get_settings().trade_broker or "dhan").lower()
        rows, _ = fetch_positions(broker)  # type: ignore[arg-type]
        out["day_pnl"] = float(sum(r.pnl for r in rows)) if rows else 0.0
    except Exception:
        out["day_pnl"] = None

    return out


def classify_regime(state: StrategicState) -> StrategicState:
    """Map tape + outlook into a coarse regime.

    Strong directional outlook scores are TREND_UP/DOWN — not HIGH_VOL.
    HIGH_VOL is reserved for explicit high-volatility speculation without a
    clear directional bias (so we do not freeze hunting on a bullish open).
    """
    pcr = state.get("pcr")
    bias = (state.get("outlook_bias") or "").upper()
    score = float(state.get("outlook_score") or 0)
    spec = state.get("speculation_signals") or {}
    vol_high = isinstance(spec.get("volatility"), str) and "high" in str(
        spec.get("volatility")
    ).lower()

    regime = "UNKNOWN"
    if not state.get("chain_live"):
        regime = "UNKNOWN"
    elif vol_high and abs(score) < 2.5 and not (
        bias.startswith("BULL") or bias.startswith("BEAR")
    ):
        regime = "HIGH_VOL"
    elif bias.startswith("BULL") and (pcr is None or pcr >= 0.95):
        regime = "TREND_UP"
    elif bias.startswith("BEAR") and (pcr is None or pcr <= 1.05):
        regime = "TREND_DOWN"
    elif pcr is not None and 0.85 <= pcr <= 1.25 and abs(score) < 2.5:
        regime = "RANGE"
    elif bias.startswith("BULL") or score >= 1.5:
        regime = "TREND_UP"
    elif bias.startswith("BEAR") or score <= -1.5:
        regime = "TREND_DOWN"
    elif vol_high:
        regime = "HIGH_VOL"
    else:
        regime = "RANGE"

    return {**state, "regime": regime}


def score_sentiment(state: StrategicState) -> StrategicState:
    """Blend Scout bias, PCR, Voices volume, speculation — still deterministic."""
    score = float(state.get("outlook_score") or 0)
    pcr = state.get("pcr")
    voices = int(state.get("voices_week") or 0)
    bias = (state.get("outlook_bias") or "").upper()

    sent = 0.0
    sent += max(-3.0, min(3.0, score / 2.0))
    if pcr is not None:
        # Put-heavy → mild bullish lean for dips; call-heavy → mild bearish
        if pcr >= 1.2:
            sent += 0.6
        elif pcr <= 0.8:
            sent -= 0.6
    if voices >= 100:
        sent += 0.15  # information richness, not direction
    if bias.startswith("BULL"):
        sent += 0.4
    elif bias.startswith("BEAR"):
        sent -= 0.4

    if sent >= 0.75:
        label = "BULLISH"
    elif sent <= -0.75:
        label = "BEARISH"
    else:
        label = "NEUTRAL"

    return {**state, "sentiment": label, "sentiment_score": round(sent, 3)}


def enforce_risk_limits(state: StrategicState) -> StrategicState:
    """Hard risk policy for the next tactical window."""
    mdl = float(state.get("max_daily_loss") or _max_daily_loss())
    pnl = state.get("day_pnl")
    deployed = float(state.get("deployed_risk") or 0.0)
    kill = bool(state.get("kill_switch") or state.get("trading_disabled"))
    util_cap = mdl * 0.70

    allow = True
    reason = "within limits"
    max_sleeve = 0.28

    if kill:
        allow = False
        reason = "kill-switch / trading disabled"
        max_sleeve = 0.0
    elif pnl is not None and pnl <= -mdl:
        allow = False
        reason = f"day P&L ₹{pnl:,.0f} at/below MAX_DAILY_LOSS ₹{mdl:,.0f}"
        max_sleeve = 0.0
    elif pnl is not None and pnl <= -0.7 * mdl:
        allow = False
        reason = "approaching day-loss limit — no new entries"
        max_sleeve = 0.12
    elif deployed >= util_cap:
        allow = False
        reason = f"deployed risk ₹{deployed:,.0f} ≥ util cap ₹{util_cap:,.0f}"
    elif not state.get("chain_live"):
        allow = False
        reason = "no live option chain"
        max_sleeve = 0.0

    conf = state.get("top_strategy_confidence")
    if conf is not None:
        # Scale sleeve with confidence but never all-in
        max_sleeve = min(max_sleeve, 0.12 + float(conf) * 0.16)

    # Aggressive desk day: allow slightly larger sleeves when entries are open
    # (still capped; solid stop is enforced by TRADE_STOP_FRACTION on tactical).
    if allow and (os.getenv("TRADE_AGGRESSIVE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        max_sleeve = min(0.32, max(max_sleeve, 0.22))

    risk = {
        "max_daily_loss": mdl,
        "allow_new_entries": allow,
        "max_sleeve_weight": round(max_sleeve, 4),
        "kill": kill or (pnl is not None and pnl <= -mdl),
        "reason": reason,
    }
    return {**state, "risk": risk}


def decide_stance(state: StrategicState) -> StrategicState:
    """Strategic stance + preferred side for tactical sizing (no order)."""
    risk = state.get("risk") or {}
    regime = state.get("regime") or "UNKNOWN"
    sentiment = state.get("sentiment") or "NEUTRAL"
    sid = (state.get("top_strategy_id") or "").lower()

    if risk.get("kill") or not risk.get("allow_new_entries"):
        stance = "FLAT" if risk.get("kill") else "HOLD"
        side = "NONE"
        hint = str(risk.get("reason") or "risk hold")
        conf = 0.85
    elif regime == "HIGH_VOL":
        # Still hunt when bias is clear — smaller sleeve via risk.max_sleeve_weight.
        if sentiment == "BULLISH":
            stance = "HUNT"
            side = "CE"
            hint = "High-vol · directional BULL — reduced-size CE hunt, solid stop"
            conf = 0.62
        elif sentiment == "BEARISH":
            stance = "HUNT"
            side = "PE"
            hint = "High-vol · directional BEAR — reduced-size PE hunt, solid stop"
            conf = 0.62
        else:
            stance = "REDUCE"
            side = "NONE"
            hint = "High-vol · no clear side — reduce / no fresh risk"
            conf = 0.7
    elif regime == "UNKNOWN" or not state.get("chain_live"):
        stance = "HOLD"
        side = "NONE"
        hint = "Await live chain"
        conf = 0.6
    else:
        stance = "HUNT"
        if "bear" in sid or sentiment == "BEARISH" or regime == "TREND_DOWN":
            side = "PE"
        elif "bull" in sid or "breakout" in sid or sentiment == "BULLISH" or regime == "TREND_UP":
            side = "CE"
        elif regime == "RANGE":
            # Mild lean from PCR via sentiment score
            side = "CE" if float(state.get("sentiment_score") or 0) >= 0 else "PE"
        else:
            side = "CE" if sentiment != "BEARISH" else "PE"
        hint = (
            f"{regime} · {sentiment} · prefer BUY {side} sleeve · "
            f"strategy {state.get('top_strategy_name') or sid or 'n/a'}"
        )
        conf = float(state.get("top_strategy_confidence") or 0.65)

    return {
        **state,
        "stance": stance,
        "preferred_side": side,
        "strategy_hint": hint[:240],
        "confidence": round(conf, 3),
    }


def publish_node(state: StrategicState) -> StrategicState:
    risk_raw = state.get("risk") or {}
    directive = StrategyDirective(
        asof=str(state.get("asof") or datetime.now(IST).isoformat()),
        symbol=str(state.get("symbol") or "NIFTY"),
        regime=state.get("regime") or "UNKNOWN",  # type: ignore[arg-type]
        sentiment=state.get("sentiment") or "NEUTRAL",  # type: ignore[arg-type]
        sentiment_score=float(state.get("sentiment_score") or 0),
        stance=state.get("stance") or "HOLD",  # type: ignore[arg-type]
        preferred_side=state.get("preferred_side") or "NONE",  # type: ignore[arg-type]
        strategy_hint=str(state.get("strategy_hint") or ""),
        confidence=float(state.get("confidence") or 0),
        risk=RiskLimits(
            max_daily_loss=float(risk_raw.get("max_daily_loss") or _max_daily_loss()),
            allow_new_entries=bool(risk_raw.get("allow_new_entries", False)),
            max_sleeve_weight=float(risk_raw.get("max_sleeve_weight") or 0.28),
            kill=bool(risk_raw.get("kill", False)),
            reason=str(risk_raw.get("reason") or ""),
        ),
        ttl_sec=int(os.getenv("STRATEGIC_TTL_SEC") or 180),
        meta={
            "pcr": state.get("pcr"),
            "outlook_bias": state.get("outlook_bias"),
            "outlook_score": state.get("outlook_score"),
            "day_pnl": state.get("day_pnl"),
            "deployed_risk": state.get("deployed_risk"),
            "top_strategy_id": state.get("top_strategy_id"),
            "error": state.get("error"),
        },
    )
    try:
        publish_directive(directive)
    except Exception as exc:
        logger.exception("publish_directive failed")
        return {**state, "error": f"publish:{exc}", "directive": directive.to_dict()}
    return {**state, "directive": directive.to_dict()}


def build_strategic_graph():
    """Compile LangGraph: gather → regime → sentiment → risk → stance → publish."""
    g = StateGraph(StrategicState)
    g.add_node("gather_market", gather_market)
    g.add_node("classify_regime", classify_regime)
    g.add_node("score_sentiment", score_sentiment)
    g.add_node("enforce_risk_limits", enforce_risk_limits)
    g.add_node("decide_stance", decide_stance)
    g.add_node("publish", publish_node)

    g.add_edge(START, "gather_market")
    g.add_edge("gather_market", "classify_regime")
    g.add_edge("classify_regime", "score_sentiment")
    g.add_edge("score_sentiment", "enforce_risk_limits")
    g.add_edge("enforce_risk_limits", "decide_stance")
    g.add_edge("decide_stance", "publish")
    g.add_edge("publish", END)
    return g.compile()


_GRAPH = None


def get_strategic_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_strategic_graph()
    return _GRAPH


def run_strategic_cycle(symbol: str = "NIFTY") -> dict[str, Any]:
    """One strategic pass; returns final state including published directive."""
    graph = get_strategic_graph()
    result = graph.invoke({"symbol": symbol.upper()})
    return dict(result)
