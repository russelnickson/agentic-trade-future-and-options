"""Agent-fleet spider (radar) charts — bull/bear, trade appetite, P&L sentiment."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(value)))


def _outlook_scores(outlook: dict[str, Any]) -> tuple[float, float]:
    """Return (bullish_0_100, bearish_0_100) from Global Outlook."""
    score = outlook.get("score")
    bias = str(outlook.get("bias") or "").upper()
    if isinstance(score, (int, float)):
        # Typical composite roughly in [-3, +3]
        bull = _clamp(50.0 + float(score) * 16.0)
    elif "BULL" in bias:
        bull = 72.0
    elif "BEAR" in bias:
        bull = 28.0
    else:
        bull = 50.0
    return bull, 100.0 - bull


def _pcr_tilt(pcr: float | None) -> tuple[float, float]:
    """Soft PCR tilt: elevated PCR → mild bullish (contrarian), low → mild bearish."""
    if pcr is None:
        return 50.0, 50.0
    # Center ~1.0
    bull = _clamp(50.0 + (float(pcr) - 1.0) * 35.0)
    return bull, 100.0 - bull


def _speculation_tilt(speculation: dict[str, Any]) -> tuple[float, float]:
    signals = speculation.get("signals") or {}
    text = " ".join(str(v) for v in signals.values()).upper()
    headline = str(speculation.get("headline") or "").upper()
    blob = f"{text} {headline}"
    bull_hits = sum(1 for w in ("BULL", "LONG", "CALL", "UPSIDE", "RISK_ON") if w in blob)
    bear_hits = sum(1 for w in ("BEAR", "SHORT", "PUT", "DOWNSIDE", "RISK_OFF") if w in blob)
    if bull_hits == bear_hits == 0:
        return 50.0, 50.0
    total = bull_hits + bear_hits
    bull = _clamp(100.0 * bull_hits / total)
    return bull, 100.0 - bull


def build_fleet_radar_scores(
    *,
    client: Any | None,
    symbol: str,
    day: Any,
    controls: dict[str, Any],
    trading_disabled: bool,
) -> dict[str, Any]:
    """
    Build three radar payloads:

    1. bull_bear — directional lean
    2. trade_more — should more risk be taken today
    3. pnl_sentiment — profit vs loss day sentiment
    """
    outlook: dict[str, Any] = {}
    chain: dict[str, Any] = {}
    speculation: dict[str, Any] = {}
    thesis: dict[str, Any] = {}

    try:
        from dashboard.components.console_runtime import (
            _chain_bits,
            _load_outlook_bits,
            _load_speculation_bits,
        )

        outlook = _load_outlook_bits()
        chain = _chain_bits(client, symbol)
        speculation = _load_speculation_bits()
    except Exception:
        logger.debug("radar input load failed", exc_info=True)

    try:
        from services.day_thesis import load_thesis

        thesis = load_thesis(symbol, redis_client=client) or {}
    except Exception:
        logger.debug("radar thesis load failed", exc_info=True)

    pcr = chain.get("pcr")
    if not isinstance(pcr, (int, float)):
        pcr = None

    o_bull, o_bear = _outlook_scores(outlook)
    p_bull, p_bear = _pcr_tilt(float(pcr) if pcr is not None else None)
    s_bull, s_bear = _speculation_tilt(speculation)

    # Thesis chase grade lean
    primary = str(thesis.get("primary_target") or "").upper()
    if primary == "PHENOMENAL":
        t_bull, t_bear = 78.0, 22.0
    elif primary == "OKAY":
        t_bull, t_bear = 62.0, 38.0
    elif primary in {"ACCEPTABLE_LOSS", "BREACH"}:
        t_bull, t_bear = 30.0, 70.0
    else:
        t_bull, t_bear = 50.0, 50.0

    bull_axes = ["Outlook", "PCR tilt", "Speculation", "Thesis chase", "Tape lean"]
    bull_vals = [o_bull, p_bull, s_bull, t_bull, (o_bull + p_bull) / 2]
    bear_vals = [o_bear, p_bear, s_bear, t_bear, (o_bear + p_bear) / 2]

    # --- Trade more? ---
    clock_open = 80.0
    try:
        from dashboard.components.console_runtime import session_clock

        clock = session_clock()
        if clock.phase in {"OPEN", "PRE_OPEN"}:
            clock_open = 90.0
        elif clock.phase == "CLOSING":
            clock_open = 35.0
        elif clock.phase in {"CLOSED", "WEEKEND"}:
            clock_open = 10.0
        else:
            clock_open = 55.0
    except Exception:
        pass

    kill = bool(controls.get("kill_switch") or controls.get("trading_disabled") or trading_disabled)
    risk_room = 15.0 if kill else (25.0 if getattr(day, "grade", None) == "BREACH" else 85.0)
    if getattr(day, "grade", None) == "ACCEPTABLE_LOSS":
        risk_room = min(risk_room, 45.0)

    capital_ok = 70.0
    try:
        from dashboard.components.capital import fetch_capital
        from config.settings import get_settings

        broker = (get_settings().trade_broker or "dhan").lower()
        cap = fetch_capital(broker)  # type: ignore[arg-type]
        avail = float(cap.available_margin or 0)
        capital_ok = _clamp(30.0 + min(avail, 100_000) / 100_000 * 70.0)
        if cap.error:
            capital_ok = 40.0
    except Exception:
        capital_ok = 55.0

    progress = thesis.get("progress_pct")
    if isinstance(progress, (int, float)):
        # Already near target → less need to add risk; far below with OK tape → more
        thesis_gap = _clamp(100.0 - float(progress) * 0.6)
    else:
        thesis_gap = 55.0

    edge = _clamp((o_bull + p_bull) / 2)  # directional clarity as "edge"
    conviction = _clamp(abs(o_bull - 50.0) * 2)  # strong lean either way

    trade_axes = ["Session window", "Risk headroom", "Capital", "Edge clarity", "Room to target"]
    trade_vals = [clock_open, risk_room, capital_ok, max(edge, conviction), thesis_gap]
    trade_hold = [100.0 - v for v in trade_vals]  # "sit tight" opposite

    # --- P&L sentiment ---
    pnl = getattr(day, "pnl", None)
    grade = str(getattr(day, "grade", "NO_DATA") or "NO_DATA")
    grade_profit = {
        "PHENOMENAL": 95.0,
        "OKAY": 78.0,
        "FLAT": 50.0,
        "ACCEPTABLE_LOSS": 32.0,
        "BREACH": 10.0,
        "NO_DATA": 50.0,
    }.get(grade, 50.0)

    if isinstance(pnl, (int, float)):
        # Map INR P&L softly onto 0–100 (₹±10k scale)
        mtm_score = _clamp(50.0 + float(pnl) / 200.0)
    else:
        mtm_score = 50.0

    target = thesis.get("target_profit_nett")
    achieved = thesis.get("current_nett_pnl")
    if isinstance(target, (int, float)) and target > 0 and isinstance(achieved, (int, float)):
        path_score = _clamp(50.0 + (float(achieved) / float(target)) * 50.0)
    else:
        path_score = grade_profit

    budget = float(thesis.get("day_budget") or 5000.0)
    if isinstance(pnl, (int, float)) and budget > 0:
        budget_score = _clamp(100.0 + (float(pnl) / budget) * 50.0)  # 0 pnl → 100, -budget → 50
    else:
        budget_score = 70.0 if grade != "BREACH" else 20.0

    fee_drag = 55.0
    charges = (thesis.get("session_charges") or {}).get("total")
    if isinstance(charges, (int, float)) and isinstance(pnl, (int, float)) and abs(pnl) + charges > 0:
        # High fees vs gross → lossy sentiment
        fee_drag = _clamp(100.0 - (float(charges) / max(abs(float(pnl)) + float(charges), 1.0)) * 80.0)

    pnl_axes = ["Day grade", "Live MTM", "Path to target", "Budget headroom", "After fees"]
    profit_vals = [grade_profit, mtm_score, path_score, budget_score, fee_drag]
    loss_vals = [100.0 - v for v in profit_vals]

    return {
        "bull_bear": {
            "title": "Bullish vs bearish",
            "axes": bull_axes,
            "traces": [
                {"name": "Bullish", "values": bull_vals, "color": "#0F6E56"},
                {"name": "Bearish", "values": bear_vals, "color": "#B42318"},
            ],
            "verdict": (
                "Bullish lean"
                if sum(bull_vals) > sum(bear_vals) + 40
                else ("Bearish lean" if sum(bear_vals) > sum(bull_vals) + 40 else "Balanced / mixed")
            ),
        },
        "trade_more": {
            "title": "Take more trades today?",
            "axes": trade_axes,
            "traces": [
                {"name": "Add risk", "values": trade_vals, "color": "#1D4E89"},
                {"name": "Sit tight", "values": trade_hold, "color": "#6B7280"},
            ],
            "verdict": (
                "Yes — conditions favor more prints"
                if (not kill and sum(trade_vals) > sum(trade_hold) + 30)
                else (
                    "No — capital/risk blocked"
                    if kill or getattr(day, "grade", None) == "BREACH"
                    else "Selective — only high-conviction"
                )
            ),
        },
        "pnl_sentiment": {
            "title": "Profit vs loss sentiment",
            "axes": pnl_axes,
            "traces": [
                {"name": "Profit path", "values": profit_vals, "color": "#0B3D1E"},
                {"name": "Loss path", "values": loss_vals, "color": "#7F1D1D"},
            ],
            "verdict": (
                "Profit-leaning day"
                if sum(profit_vals) > sum(loss_vals) + 40
                else (
                    "Loss-leaning day"
                    if sum(loss_vals) > sum(profit_vals) + 40
                    else "Unsettled — protect capital"
                )
            ),
        },
    }


def render_fleet_radars(scores: dict[str, Any]) -> None:
    """Render three spider graphs side-by-side in Streamlit."""
    import streamlit as st

    try:
        import plotly.graph_objects as go
    except ImportError:
        st.warning("Install `plotly` to show fleet spider graphs (`pip install plotly`).")
        return

    st.subheader("Fleet dials")
    st.caption("Spider read on direction, whether to add risk, and profit vs loss path — from live desk inputs.")

    cols = st.columns(3)
    keys = ("bull_bear", "trade_more", "pnl_sentiment")
    for col, key in zip(cols, keys):
        block = scores.get(key) or {}
        axes = list(block.get("axes") or [])
        if not axes:
            continue
        fig = go.Figure()
        for trace in block.get("traces") or []:
            vals = list(trace.get("values") or [])
            # Close the polygon
            r = vals + vals[:1]
            theta = axes + axes[:1]
            fig.add_trace(
                go.Scatterpolar(
                    r=r,
                    theta=theta,
                    fill="toself",
                    name=str(trace.get("name") or ""),
                    line=dict(color=trace.get("color") or "#334155", width=2),
                    fillcolor=trace.get("color") or "#334155",
                    opacity=0.45,
                )
            )
        fig.update_layout(
            title=dict(text=str(block.get("title") or ""), font=dict(size=14)),
            polar=dict(
                radialaxis=dict(visible=True, range=[0, 100], tickfont=dict(size=9)),
                angularaxis=dict(tickfont=dict(size=10)),
            ),
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=-0.25, font=dict(size=10)),
            margin=dict(l=40, r=40, t=48, b=48),
            height=340,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
        )
        with col:
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
            st.caption(str(block.get("verdict") or ""))
