"""Console runtime — agent fleet, session clock, day outcome, factual briefings.

Agents do not invent market claims. Briefing lines are templated from live
snapshots (Global Outlook, Live Market, chain, P&L, risk controls).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from dashboard.components.agent_journal import (
    append_conversation,
    append_decision,
    build_insight_from_market,
    load_conversations,
    load_decisions,
    load_strategy_snapshot,
)
from dashboard.timefmt import format_ist
from database.redis_client import RedisClient

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

DayGrade = Literal[
    "PHENOMENAL",
    "OKAY",
    "FLAT",
    "ACCEPTABLE_LOSS",
    "BREACH",
    "NO_DATA",
]

SessionPhase = Literal[
    "PRE_MARKET",
    "PRE_OPEN",
    "OPEN",
    "CLOSING",
    "CLOSED",
    "WEEKEND",
]

BRIEFING_HASH_KEY = "agent:console:briefing_hash"
DAY_STATE_KEY = "agent:console:day_state"

# Soft thresholds vs starting capital (or absolute INR fallbacks).
PHENOMENAL_PCT = 0.012  # ≥1.2% of capital
OKAY_PCT = 0.002  # ≥0.2%
FLAT_ABS = 500.0  # |pnl| < ₹500 ≈ flat
ACCEPTABLE_LOSS_PCT = 0.008  # loss within 0.8% of capital = still "decent"


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    name: str
    role: str
    mandate: str
    color: str


AGENT_FLEET: tuple[AgentSpec, ...] = (
    AgentSpec(
        "scout",
        "Scout",
        "global",
        "TODAY only — overnight/global cues that drive this IST session’s open & first impulse",
        "#0F6E56",
    ),
    AgentSpec(
        "voices",
        "Voices",
        "live_market",
        "TODAY only — same-day filings/policy that can move the cash tape this session",
        "#1D4E89",
    ),
    AgentSpec(
        "research",
        "Research",
        "researcher",
        "TODAY only — live chain/PCR/OI + top backtested day strategies → actionable setups now",
        "#6B4F9A",
    ),
    AgentSpec(
        "thesis",
        "Thesis",
        "thesis",
        "TODAY only — consolidate Scout/Voices/Research/Risk into nett-impact day grades "
        "(PHENOMENAL→OKAY→FLAT→ACCEPTABLE_LOSS→BREACH) after trade charges",
        "#0E7490",
    ),
    AgentSpec(
        "risk",
        "Risk",
        "risk",
        "TODAY only — size & kill-switch so capital works the session without blowing the day budget",
        "#A15C00",
    ),
    AgentSpec(
        "trade",
        "Trade",
        "execution",
        "TODAY only — mirrors LangGraph directive; tactical Python owns orders/stops",
        "#B42318",
    ),
)

# Desk doctrine: maximise utilisation inside today's market hours.
DESK_DOCTRINE = (
    "Strict focus: TODAY’s market hours (09:15–15:30 IST). "
    "LangGraph = strategic controller (regime/sentiment/risk every few minutes). "
    "Deterministic tactical Python = orders & stop-loss. Never all-in."
)

ENTRY_LOCK_KEY = "agent:console:today_entry_lock"


@dataclass
class SessionClock:
    now_ist: str
    phase: SessionPhase
    label: str
    minutes_to_open: int | None = None
    minutes_to_close: int | None = None
    is_live_desk: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DayOutcome:
    grade: DayGrade
    decent: bool
    pnl: float | None
    capital_ref: float | None
    message: str
    ladder: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentStatus:
    agent_id: str
    name: str
    role: str
    mandate: str
    status: str  # LIVE | IDLE | BLOCKED | STALE
    headline: str
    detail: str
    color: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def session_clock(now: datetime | None = None) -> SessionClock:
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)

    wd = now.weekday()
    if wd >= 5:
        return SessionClock(
            now_ist=format_ist(now, seconds=True),
            phase="WEEKEND",
            label="Weekend — desk idle",
            is_live_desk=False,
        )

    t = now.time()
    open_t = time(9, 15)
    close_t = time(15, 30)
    pre_open = time(9, 0)
    closing = time(15, 15)

    def _mins(target: time) -> int:
        target_dt = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
        return int((target_dt - now).total_seconds() // 60)

    if t < pre_open:
        return SessionClock(
            now_ist=format_ist(now, seconds=True),
            phase="PRE_MARKET",
            label="Pre-market — arm today’s hunt (Scout/Voices/Research)",
            minutes_to_open=_mins(open_t),
            is_live_desk=False,
        )
    if t < open_t:
        return SessionClock(
            now_ist=format_ist(now, seconds=True),
            phase="PRE_OPEN",
            label="Pre-open — Trade standing by to deploy capital at 09:15",
            minutes_to_open=_mins(open_t),
            is_live_desk=True,
        )
    if t < closing:
        return SessionClock(
            now_ist=format_ist(now, seconds=True),
            phase="OPEN",
            label="LIVE session — hunt & execute; idle cash is opportunity cost",
            minutes_to_close=_mins(close_t),
            is_live_desk=True,
        )
    if t <= close_t:
        return SessionClock(
            now_ist=format_ist(now, seconds=True),
            phase="CLOSING",
            label="Closing window — harvest/protect today’s P&L; no fresh overnight risk",
            minutes_to_close=_mins(close_t),
            is_live_desk=True,
        )
    return SessionClock(
        now_ist=format_ist(now, seconds=True),
        phase="CLOSED",
        label="Session closed — review today; prep tomorrow’s open hunt",
        is_live_desk=False,
    )


def classify_day_outcome(
    pnl: float | None,
    *,
    capital_ref: float | None = None,
) -> DayOutcome:
    ladder = [
        {"grade": "PHENOMENAL", "meaning": "Strong green — clear outperformance"},
        {"grade": "OKAY", "meaning": "Modest profit — solid trade day"},
        {"grade": "FLAT", "meaning": "No meaningful P&L — capital preserved"},
        {"grade": "ACCEPTABLE_LOSS", "meaning": "Small loss inside budget — still decent"},
        {"grade": "BREACH", "meaning": "Beyond acceptable loss — not a decent close"},
    ]
    if pnl is None:
        return DayOutcome(
            grade="NO_DATA",
            decent=True,
            pnl=None,
            capital_ref=capital_ref,
            message="No open P&L yet — day grade pending first mark.",
            ladder=ladder,
        )

    cap = capital_ref if capital_ref and capital_ref > 0 else None
    abs_pnl = abs(pnl)

    if cap:
        phen = cap * PHENOMENAL_PCT
        okay = cap * OKAY_PCT
        acc_loss = cap * ACCEPTABLE_LOSS_PCT
    else:
        phen, okay, acc_loss = 15_000.0, 2_000.0, 8_000.0

    if pnl >= phen:
        grade: DayGrade = "PHENOMENAL"
        decent = True
        msg = f"Phenomenal pace · MTM ₹{pnl:+,.0f}"
    elif pnl >= okay:
        grade = "OKAY"
        decent = True
        msg = f"Okay profit · MTM ₹{pnl:+,.0f}"
    elif abs_pnl <= FLAT_ABS or (cap and abs_pnl <= cap * 0.0005):
        grade = "FLAT"
        decent = True
        msg = f"Flat / no profit · MTM ₹{pnl:+,.0f} — capital preserved"
    elif pnl < 0 and abs_pnl <= acc_loss:
        grade = "ACCEPTABLE_LOSS"
        decent = True
        msg = f"Acceptable loss · MTM ₹{pnl:+,.0f} — still a decent close path"
    elif pnl < 0:
        grade = "BREACH"
        decent = False
        msg = f"Beyond acceptable loss · MTM ₹{pnl:+,.0f} — Risk/Trade must cut"
    else:
        grade = "FLAT"
        decent = True
        msg = f"Near-flat · MTM ₹{pnl:+,.0f}"

    return DayOutcome(
        grade=grade,
        decent=decent,
        pnl=float(pnl),
        capital_ref=cap,
        message=msg,
        ladder=ladder,
    )


def _load_outlook_bits() -> dict[str, Any]:
    try:
        from services.global_outlook import load_snapshot

        snap = load_snapshot()
        if not snap:
            return {"status": "STALE", "headline": "No Global Outlook snapshot", "detail": "Refresh Global Outlook"}
        return {
            "status": "LIVE",
            "headline": f"Bias {snap.bias} · score {snap.score:+.2f}",
            "detail": (snap.summary or "")[:220],
            "bias": snap.bias,
            "score": snap.score,
        }
    except Exception as exc:
        return {"status": "STALE", "headline": "Outlook unavailable", "detail": str(exc)[:160]}


def _load_voices_bits() -> dict[str, Any]:
    try:
        from services.live_market_voices import filter_horizon, load_snapshot, load_voices

        snap = load_snapshot()
        df = load_voices()
        week = filter_horizon(df, "week") if not df.empty else df
        n = len(week)
        classes = (
            week["voice_class"].value_counts().head(3).to_dict() if n and "voice_class" in week.columns else {}
        )
        class_txt = ", ".join(f"{k} {v}" for k, v in classes.items()) or "—"
        health_ok = sum(1 for h in (snap.source_health if snap else []) if h.get("status") == "ok")
        health_n = len(snap.source_health) if snap else 0
        return {
            "status": "LIVE" if n else "IDLE",
            "headline": f"{n} direct-source items (week) · feeds {health_ok}/{health_n}",
            "detail": f"Mix by class: {class_txt}",
            "count": n,
        }
    except Exception as exc:
        return {"status": "STALE", "headline": "Voices unavailable", "detail": str(exc)[:160]}


def _chain_bits(client: RedisClient | None, symbol: str) -> dict[str, Any]:
    if client is None:
        return {"status": "STALE", "headline": "Redis down — no chain", "detail": ""}
    try:
        from services.oi_tracker import compute_pcr

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
        ltp = chain.get("underlying_ltp")
        atm = chain.get("atm")
        if ltp is None and not chain.get("strikes"):
            return {"status": "IDLE", "headline": f"{symbol} chain empty", "detail": "Waiting for tick workers"}
        return {
            "status": "LIVE",
            "headline": f"{symbol} LTP {ltp or '—'} · ATM {atm or '—'} · PCR {pcr:.3f}" if pcr is not None else f"{symbol} LTP {ltp or '—'}",
            "detail": f"Expiry {chain.get('expiry') or '—'} · updated {str(chain.get('updated_at') or '—')[:19]}",
            "ltp": ltp,
            "pcr": pcr,
            "atm": atm,
        }
    except Exception as exc:
        return {"status": "STALE", "headline": "Chain read failed", "detail": str(exc)[:160]}


def _load_speculation_bits() -> dict[str, Any]:
    try:
        from dashboard.components.broker_speculation import load_speculation, speculation_summary

        spec = load_speculation()
        if not spec:
            return {"status": "IDLE", "headline": "No broker speculation loaded", "detail": ""}
        sig = ", ".join(f"{k}={v}" for k, v in list(spec.signals.items())[:4])
        return {
            "status": "LIVE",
            "headline": f"SPECULATION · {sig}",
            "detail": speculation_summary(spec)[:240],
            "credibility": spec.credibility,
            "signals": spec.signals,
            "nifty": spec.nifty,
            "banknifty": spec.banknifty,
            "summary": speculation_summary(spec),
        }
    except Exception as exc:
        return {"status": "STALE", "headline": "Speculation unavailable", "detail": str(exc)[:160]}


def _load_insight_bits(client: RedisClient | None, symbol: str) -> dict[str, Any]:
    try:
        snap = load_strategy_snapshot(client)
        if not snap:
            return {
                "status": "IDLE",
                "headline": "No Insights strategy snapshot",
                "detail": "Generate from Insights page or desk rerun",
            }
        title = str(snap.get("title") or "Strategy")[:80]
        strategy = str(snap.get("strategy_for_tomorrow") or snap.get("outlook") or "")[:220]
        why = str(snap.get("why") or "")[:160]
        metrics = snap.get("supporting_metrics") or {}
        pcr = metrics.get("pcr") if isinstance(metrics, dict) else None
        extra = f" · PCR {float(pcr):.2f}" if pcr is not None else ""
        return {
            "status": "LIVE",
            "headline": f"{title}{extra}",
            "detail": strategy or why,
            "strategy": strategy,
            "why": why,
            "title": title,
            "metrics": metrics if isinstance(metrics, dict) else {},
            "trade_date": snap.get("trade_date"),
        }
    except Exception as exc:
        return {"status": "STALE", "headline": "Insights unavailable", "detail": str(exc)[:160]}


def _compose_trade_decision(
    *,
    symbol: str,
    clock: SessionClock,
    day: DayOutcome,
    controls: dict[str, Any],
    outlook: dict[str, Any],
    voices: dict[str, Any],
    chain: dict[str, Any],
    insight: dict[str, Any],
    speculation: dict[str, Any] | None = None,
    client: RedisClient | None = None,
    has_open_positions: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Build Trade stance: TODAY hours only — hunt & execute; idle cash = opportunity cost."""
    speculation = speculation or {}
    if controls.get("kill_switch") or controls.get("trading_disabled"):
        return (
            "Trade: blocked — kill-switch / day disable armed. No new risk.",
            {
                "kind": "SKIP",
                "summary": "SKIP — trading disabled",
                "rationale": "Kill-switch or trading_disabled is active.",
                "confidence": 1.0,
                "status": "BLOCKED",
            },
        )
    if day.grade == "BREACH":
        return (
            "Trade: BREACH day grade — cut risk / flatten; no new units.",
            {
                "kind": "SQUARE_OFF",
                "summary": "SQUARE_OFF — beyond acceptable loss",
                "rationale": day.message,
                "confidence": 0.85,
                "status": "PROPOSED",
            },
        )

    bias = str(outlook.get("bias") or "")
    score = outlook.get("score")
    voices_n = voices.get("count")
    strategy = str(insight.get("strategy") or "").strip()
    pcr = chain.get("pcr")
    ltp = chain.get("ltp")

    from services.intraday_hunt import (
        build_hunt_plan,
        entry_lock_state,
        remaining_risk_budget,
        top_strategy_hint,
    )

    strat_id, strat_title, strat_conf = top_strategy_hint(client)
    if not strat_title and insight.get("title"):
        strat_title = str(insight.get("title") or "")
    if not strategy and strat_title:
        strategy = strat_title

    rationale_parts = [
        DESK_DOCTRINE,
        f"Scout: {outlook.get('headline') or 'n/a'}",
        f"Voices: {voices.get('headline') or 'n/a'}",
        f"Research tape: {chain.get('headline') or 'n/a'}",
        f"Insights: {insight.get('headline') or strat_title or 'n/a'}",
    ]
    if speculation.get("status") == "LIVE":
        rationale_parts.append(
            f"Broker SPECULATION (cred {speculation.get('credibility', 0.55):.2f}): "
            f"{speculation.get('headline') or 'n/a'}"
        )
    if insight.get("why"):
        rationale_parts.append(f"Why: {insight['why']}")
    rationale = " | ".join(rationale_parts)[:900]

    meta_base: dict[str, Any] = {
        "source": "console_runtime",
        "doctrine": "today_hunt_execute_probabilistic",
        "phase": clock.phase,
        "grade": day.grade,
        "outlook_bias": bias,
        "outlook_score": score,
        "voices_week": voices_n,
        "pcr": pcr,
        "ltp": ltp,
        "insight_title": insight.get("title") or strat_title,
        "top_strategy_id": strat_id or None,
        "top_strategy_confidence": strat_conf,
        "broker_speculation": speculation.get("signals") if speculation.get("status") == "LIVE" else None,
        "nifty_zones": speculation.get("nifty") if speculation.get("status") == "LIVE" else None,
    }

    if clock.phase in {"CLOSED", "WEEKEND"}:
        return (
            "Trade: session closed — no overnight risk. Prep tomorrow’s open hunt from Insights.",
            {
                "kind": "OBSERVE",
                "summary": "OBSERVE — session closed; prep next open",
                "rationale": rationale,
                "confidence": 0.7,
                "status": "PROPOSED",
                "meta": meta_base,
            },
        )
    if clock.phase == "CLOSING":
        return (
            f"Trade: closing window — harvest/protect {day.grade}. No fresh risk; capital already at work or flatten.",
            {
                "kind": "OBSERVE",
                "summary": f"OBSERVE — lock decent day ({day.grade})",
                "rationale": rationale,
                "confidence": 0.75,
                "status": "PROPOSED",
                "meta": meta_base,
            },
        )
    if clock.phase in {"PRE_MARKET", "PRE_OPEN"}:
        return (
            "Trade: pre-open — arming sized sleeves. Capital deploys at 09:15 once chain is LIVE.",
            {
                "kind": "OBSERVE",
                "summary": "OBSERVE — arming today’s probabilistic hunt",
                "rationale": rationale,
                "confidence": 0.7,
                "status": "PROPOSED",
                "meta": meta_base,
            },
        )
    if chain.get("status") != "LIVE":
        return (
            f"Trade: HUNT blocked — no live {symbol} chain. Idle capital waiting on ticks (opportunity cost).",
            {
                "kind": "SKIP",
                "summary": "SKIP — no live chain yet (capital idle)",
                "rationale": rationale,
                "confidence": 0.8,
                "status": "PROPOSED",
                "meta": meta_base,
            },
        )

    lock_st = entry_lock_state(client)
    budget = remaining_risk_budget(client)
    meta_base["day_risk"] = {
        "deployed": budget["deployed_risk"],
        "remaining": budget["remaining"],
        "util_cap": budget["util_cap"],
        "max_daily_loss": budget["max_daily_loss"],
        "sleeves": budget["sleeves"],
        "max_sleeves": budget["max_sleeves"],
    }

    # Live manage open sleeves toward take-profit / trail (tactical owns exits)
    open_manage: list[dict[str, Any]] = []
    try:
        from services.intraday_hunt import load_day_risk, open_sleeves

        for s in open_sleeves(load_day_risk(client)):
            entry = float(s.get("entry_ltp") or s.get("ltp") or 0)
            last = s.get("last_ltp")
            pct = s.get("unrealized_pct")
            open_manage.append(
                {
                    "security_id": s.get("security_id"),
                    "option_type": s.get("option_type"),
                    "strike": s.get("strike"),
                    "qty": s.get("qty"),
                    "entry": entry or None,
                    "ltp": last,
                    "stop": s.get("stop_price"),
                    "target": s.get("target_price"),
                    "peak": s.get("peak_ltp"),
                    "trail_armed": bool(s.get("trail_armed")),
                    "unrealized_pct": pct,
                }
            )
    except Exception:
        open_manage = []
    if open_manage:
        meta_base["open_sleeves"] = open_manage
        bits = []
        for m in open_manage[:3]:
            side = m.get("option_type") or "?"
            strike = m.get("strike") or "?"
            pct = m.get("unrealized_pct")
            tgt = m.get("target")
            bits.append(
                f"{int(strike) if isinstance(strike, (int, float)) else strike}{side}"
                f"{'' if pct is None else f' {float(pct):+.1f}%'}"
                f"{'' if tgt is None else f' →tp {tgt}'}"
            )
        return (
            "Trade: MANAGE open sleeves — trail/lock profits at reasonable upside; "
            "hard take-profit armed. "
            + " · ".join(bits),
            {
                "kind": "MANAGE",
                "summary": (
                    f"MANAGE — {len(open_manage)} open · "
                    + " · ".join(bits)
                )[:240],
                "rationale": rationale,
                "confidence": 0.82,
                "status": "PROPOSED",
                "meta": meta_base,
            },
        )

    # Prefer LangGraph strategic directive when fresh
    try:
        from services.strategic_controller.directive import load_directive

        directive = load_directive(client)
    except Exception:
        directive = None
    if directive is not None:
        meta_base["strategic_directive"] = directive.to_dict()
        if directive.is_fresh():
            risk = directive.risk
            if risk.kill or directive.stance == "FLAT":
                return (
                    f"Trade: strategic FLAT — {risk.reason or directive.strategy_hint}. "
                    "Tactical will not open risk; stops still armed.",
                    {
                        "kind": "SKIP",
                        "summary": f"SKIP — strategic {directive.stance} · {directive.regime}",
                        "rationale": rationale,
                        "confidence": directive.confidence,
                        "status": "PROPOSED",
                        "meta": meta_base,
                    },
                )
            if directive.stance in {"HOLD", "REDUCE"} or not risk.allow_new_entries:
                return (
                    f"Trade: strategic {directive.stance} — {directive.strategy_hint}. "
                    "Orders/stops remain with tactical executor.",
                    {
                        "kind": "OBSERVE",
                        "summary": (
                            f"OBSERVE — {directive.regime}/{directive.sentiment} · "
                            f"{directive.stance}"
                        )[:240],
                        "rationale": rationale,
                        "confidence": directive.confidence,
                        "status": "PROPOSED",
                        "meta": meta_base,
                    },
                )
            if directive.allows_entry():
                return (
                    f"Trade: strategic HUNT → tactical owns ENTRY "
                    f"(prefer {directive.preferred_side}, sleeve≤{risk.max_sleeve_weight:.0%}). "
                    f"{directive.strategy_hint}",
                    {
                        "kind": "ENTRY",
                        "summary": (
                            f"ENTRY — strategic {directive.regime} · "
                            f"{directive.preferred_side} · conf {directive.confidence:.2f} "
                            f"(tactical executes)"
                        )[:240],
                        "rationale": rationale,
                        "confidence": directive.confidence,
                        "status": "PROPOSED",
                        "meta": meta_base,
                    },
                )

    if lock_st.get("state") == "cooldown_fail":
        err = str(lock_st.get("error") or "broker reject")[:200]
        hard_ip = "Invalid IP" in err or "DH-905" in err
        summary = (
            "ENTRY FAILED — Dhan Invalid IP (whitelist this machine on Dhan API)"
            if hard_ip
            else f"ENTRY FAILED — cooldown · {err[:120]}"
        )
        return (
            f"Trade: hunt fired but broker rejected — {err}. "
            f"{'Fix Dhan API IP whitelist, then clear entry lock + rerun.' if hard_ip else 'Retrying after cooldown.'} "
            f"Capital still idle (opportunity cost).",
            {
                "kind": "ENTRY",
                "summary": summary[:240],
                "rationale": rationale,
                "confidence": 0.85,
                "status": "FAILED",
                "meta": {**meta_base, "entry_lock": lock_st.get("lock"), "broker_error": err},
            },
        )

    if not budget["can_hunt"]:
        return (
            "Trade: day-loss sleeve budget utilised — MANAGE open risk toward decent close "
            f"({day.grade}). Deployed ₹{budget['deployed_risk']:,.0f} / cap ₹{budget['util_cap']:,.0f}.",
            {
                "kind": "MANAGE",
                "summary": (
                    f"MANAGE — risk budget {budget['deployed_risk']:.0f}/{budget['util_cap']:.0f} "
                    f"· sleeves {budget['sleeves']}/{budget['max_sleeves']}"
                )[:240],
                "rationale": rationale,
                "confidence": 0.8,
                "status": "PROPOSED",
                "meta": {**meta_base, "entry_lock": lock_st.get("lock")},
            },
        )

    available_margin = None
    try:
        from dashboard.components.capital import fetch_capital
        from config.settings import get_settings

        cap = fetch_capital((get_settings().trade_broker or "dhan").lower())  # type: ignore[arg-type]
        available_margin = float(cap.available_margin or 0) or None
    except Exception:
        available_margin = None

    plan = None
    if client is not None:
        try:
            plan = build_hunt_plan(
                client=client,
                symbol=symbol,
                bias=bias,
                pcr=float(pcr) if pcr is not None else None,
                strategy_id=strat_id,
                strategy_title=strat_title or strategy,
                confidence=strat_conf,
                available_margin=available_margin,
            )
        except Exception as exc:
            logger.debug("hunt plan failed: %s", exc)

    if not plan:
        return (
            "Trade: HUNT — chain LIVE but no sized plan yet; retry next pulse.",
            {
                "kind": "HUNT",
                "summary": "HUNT — waiting sized sleeve",
                "rationale": rationale,
                "confidence": 0.55,
                "status": "PROPOSED",
                "meta": meta_base,
            },
        )

    if plan.get("skip"):
        return (
            f"Trade: HOLD reserve — {plan.get('reason')}",
            {
                "kind": "SKIP",
                "summary": f"SKIP — {str(plan.get('reason') or 'size/fit')[:200]}"[:240],
                "rationale": rationale,
                "confidence": 0.7,
                "status": "PROPOSED",
                "meta": {**meta_base, "hunt_skip": plan},
            },
        )

    sizing = plan.get("sizing") or {}
    conf = 0.72
    if strat_conf is not None:
        conf = min(0.9, max(0.6, 0.55 + float(strat_conf) * 0.35))
    summary = (
        f"ENTRY sleeve — BUY {plan['lots']} lot ({plan['qty']} qty) "
        f"{int(plan['strike'])}{plan['option_type']} @~{plan['ltp']:.2f} · "
        f"risk ₹{float(sizing.get('planned_risk') or 0):,.0f} "
        f"({float(sizing.get('sleeve_weight') or 0):.0%} of day loss) · "
        f"stop ~{plan.get('stop_price')} · tp ~{plan.get('target_price')}"
    )
    msg = (
        f"Trade: EXECUTE sized sleeve (not all-in) — {summary}. "
        f"Day risk left ₹{budget['remaining']:,.0f}/{budget['util_cap']:,.0f}. "
        f"{plan.get('reason')}"
    )[:800]
    meta_base["hunt_plan"] = {
        k: plan[k]
        for k in (
            "trading_symbol",
            "option_type",
            "action",
            "strike",
            "atm",
            "expiry",
            "security_id",
            "ltp",
            "qty",
            "lots",
            "reason",
            "stop_price",
            "target_price",
            "sizing",
        )
        if k in plan
    }
    return msg, {
        "kind": "ENTRY",
        "summary": summary[:240],
        "rationale": rationale,
        "confidence": conf,
        "status": "PROPOSED",
        "meta": meta_base,
    }


def build_agent_statuses(
    *,
    client: RedisClient | None,
    symbol: str,
    controls: dict[str, Any],
    day: DayOutcome,
    trading_disabled: bool,
) -> list[AgentStatus]:
    outlook = _load_outlook_bits()
    voices = _load_voices_bits()
    chain = _chain_bits(client, symbol)
    insight = _load_insight_bits(client, symbol)
    speculation = _load_speculation_bits()

    has_open = False
    try:
        from dashboard.components.positions import fetch_positions
        from config.settings import get_settings

        broker = (get_settings().trade_broker or "dhan").lower()
        rows, _ = fetch_positions(broker, redis_client=client)  # type: ignore[arg-type]
        has_open = bool(rows)
    except Exception:
        has_open = False

    risk_status = "BLOCKED" if trading_disabled else ("LIVE" if day.grade != "BREACH" else "BLOCKED")
    risk_headline = (
        "Kill-switch / trading disabled"
        if trading_disabled
        else (f"Day grade {day.grade}" + (f" · ₹{day.pnl:+,.0f}" if day.pnl is not None else ""))
    )
    risk_detail = day.message
    if controls.get("square_off_requested") or controls.get("emergency_square_off"):
        risk_headline = "Square-off pending"
        risk_status = "LIVE"

    # Research combines live tape + Insights strategy (+ soft speculation note in detail)
    if chain.get("status") == "LIVE" and insight.get("status") == "LIVE":
        research_status = "LIVE"
        research_headline = f"{chain.get('headline')} · Insight: {insight.get('title')}"
        research_detail = str(insight.get("strategy") or insight.get("detail") or chain.get("detail") or "")
    elif insight.get("status") == "LIVE":
        research_status = "LIVE"
        research_headline = str(insight.get("headline") or "")
        research_detail = str(insight.get("detail") or "")
    else:
        research_status = chain.get("status", "IDLE")
        research_headline = str(chain.get("headline") or "")
        research_detail = str(chain.get("detail") or "")
    if speculation.get("status") == "LIVE":
        research_detail = (
            f"{research_detail} · Soft SPEC: {speculation.get('headline')}"
        ).strip(" ·")

    thesis_status = "IDLE"
    thesis_headline = "No day thesis yet — open Thesis · Rebuild"
    thesis_detail = ""
    try:
        from services.day_thesis import load_thesis

        thesis = load_thesis(symbol, redis_client=client) or {}
        if thesis:
            thesis_status = "LIVE"
            target = thesis.get("target_profit_nett")
            achieved = thesis.get("current_nett_pnl")
            primary = thesis.get("primary_target") or "OKAY"
            grade = thesis.get("current_grade") or "NO_DATA"
            target_s = f"₹{float(target):+,.0f}" if isinstance(target, (int, float)) else "—"
            achieved_s = (
                f"₹{float(achieved):+,.0f}" if isinstance(achieved, (int, float)) else "—"
            )
            thesis_headline = (
                f"Target {target_s} nett · achieved {achieved_s} · chase {primary}"
            )
            thesis_detail = str(thesis.get("consolidation") or "")[:220]
            thesis_detail = f"Grade {grade}. {thesis_detail}".strip()
    except Exception:
        logger.debug("thesis status load failed", exc_info=True)

    draft_msg, draft_dec = _compose_trade_decision(
        symbol=symbol,
        clock=session_clock(),
        day=day,
        controls=controls,
        outlook=outlook,
        voices=voices,
        chain=chain,
        insight=insight,
        speculation=speculation,
        client=client,
        has_open_positions=has_open,
    )
    trade_status = "BLOCKED" if trading_disabled else ("LIVE" if session_clock().is_live_desk else "IDLE")
    trade_headline = str(draft_dec.get("summary") or "")[:120]
    trade_detail = draft_msg[:220]

    mapping = {
        "scout": (outlook.get("status", "IDLE"), outlook.get("headline", ""), outlook.get("detail", "")),
        "voices": (voices.get("status", "IDLE"), voices.get("headline", ""), voices.get("detail", "")),
        "research": (research_status, research_headline, research_detail),
        "thesis": (thesis_status, thesis_headline, thesis_detail),
        "risk": (risk_status, risk_headline, risk_detail),
        "trade": (trade_status, trade_headline, trade_detail),
    }
    # Stash for briefing decision write
    stashed = {
        "outlook": outlook,
        "voices": voices,
        "chain": chain,
        "insight": insight,
        "speculation": speculation,
        "trade_msg": draft_msg,
        "trade_dec": draft_dec,
    }

    out: list[AgentStatus] = []
    for spec in AGENT_FLEET:
        status, headline, detail = mapping[spec.agent_id]
        agent = AgentStatus(
            agent_id=spec.agent_id,
            name=spec.name,
            role=spec.role,
            mandate=spec.mandate,
            status=str(status),
            headline=str(headline)[:160],
            detail=str(detail)[:240],
            color=spec.color,
        )
        out.append(agent)
    # Attach private stash on first element via module-level for briefing (clean enough)
    build_agent_statuses._last_inputs = stashed  # type: ignore[attr-defined]
    return out


def sync_agent_briefing(
    *,
    client: RedisClient | None,
    symbol: str,
    controls: dict[str, Any],
    day: DayOutcome,
    statuses: list[AgentStatus],
    force: bool = False,
) -> bool:
    """Publish one factual round of agent discussion if the state hash changed."""
    clock = session_clock()
    payload = {
        "phase": clock.phase,
        "symbol": symbol,
        "day": day.grade,
        "pnl": day.pnl,
        "kill": bool(controls.get("kill_switch") or controls.get("trading_disabled")),
        "heads": {s.agent_id: s.headline for s in statuses},
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]

    prev = None
    if client is not None:
        try:
            prev = client.client.get(BRIEFING_HASH_KEY)
            if isinstance(prev, bytes):
                prev = prev.decode()
        except Exception:
            prev = None

    if not force and prev == digest:
        return False

    session = f"console-{datetime.now(IST).strftime('%Y%m%d')}"
    lines = [
        (
            "orchestrator",
            "system",
            f"Console pulse · {clock.label} · {symbol} · {DESK_DOCTRINE}",
        ),
    ]
    for s in statuses:
        agent_name = s.agent_id if s.agent_id != "trade" else "execution"
        role = s.role if s.role in {"researcher", "risk", "execution", "system"} else "system"
        if s.agent_id == "scout":
            agent_name, role = "scout", "system"
        elif s.agent_id == "voices":
            agent_name, role = "voices", "system"
        elif s.agent_id == "research":
            agent_name, role = "researcher", "researcher"
        elif s.agent_id == "thesis":
            agent_name, role = "thesis", "system"
        elif s.agent_id == "risk":
            agent_name, role = "risk", "risk"
        elif s.agent_id == "trade":
            agent_name, role = "execution", "execution"
        lines.append(
            (
                agent_name,
                role,
                f"{s.name}: [{s.status}] {s.headline}" + (f" — {s.detail}" if s.detail else ""),
            )
        )

    # Trade agent stance from composed inputs (Outlook + Voices + Insights + tape)
    inputs = getattr(build_agent_statuses, "_last_inputs", None) or {}
    trade_msg = inputs.get("trade_msg")
    trade_dec = inputs.get("trade_dec")

    has_open = False
    try:
        from dashboard.components.positions import fetch_positions
        from config.settings import get_settings

        broker = (get_settings().trade_broker or "dhan").lower()
        rows, _ = fetch_positions(broker, redis_client=client)  # type: ignore[arg-type]
        has_open = bool(rows)
    except Exception:
        has_open = False

    if not trade_dec or has_open:
        trade_msg, trade_dec = _compose_trade_decision(
            symbol=symbol,
            clock=clock,
            day=day,
            controls=controls,
            outlook=inputs.get("outlook") or _load_outlook_bits(),
            voices=inputs.get("voices") or _load_voices_bits(),
            chain=inputs.get("chain") or _chain_bits(client, symbol),
            insight=inputs.get("insight") or _load_insight_bits(client, symbol),
            speculation=inputs.get("speculation"),
            client=client,
            has_open_positions=has_open,
        )

    # Execution is owned by workers/tactical_executor (deterministic). Console only records stance.
    exec_meta: dict[str, Any] = {"execution_owner": "tactical_executor"}
    try:
        raw = client.client.get("agent:tactical:state") if client is not None else None
        if isinstance(raw, bytes):
            raw = raw.decode()
        if raw:
            exec_meta["tactical_state"] = json.loads(raw)
    except Exception:
        pass

    lines.append(("execution", "execution", str(trade_msg)[:800]))
    meta_out = dict(trade_dec.get("meta") or {})
    meta_out.update(exec_meta)
    append_decision(
        {
            "agent": "execution",
            "kind": trade_dec.get("kind") or "OBSERVE",
            "symbol": symbol,
            "summary": trade_dec.get("summary") or "",
            "rationale": trade_dec.get("rationale") or "",
            "confidence": trade_dec.get("confidence"),
            "status": trade_dec.get("status") or "PROPOSED",
            "meta": meta_out,
        },
        redis_client=client,
    )

    for agent, role, message in lines:
        append_conversation(
            {
                "agent": agent,
                "role": role,
                "message": message[:800],
                "session_id": session,
                "tags": ["console", "briefing", clock.phase.lower()],
            },
            redis_client=client,
        )

    if client is not None:
        try:
            client.client.set(BRIEFING_HASH_KEY, digest)
            client.client.set(
                DAY_STATE_KEY,
                json.dumps(
                    {
                        "grade": day.grade,
                        "decent": day.decent,
                        "pnl": day.pnl,
                        "message": day.message,
                        "asof": datetime.now(IST).isoformat(),
                    },
                    default=str,
                ),
            )
        except Exception:
            logger.debug("Failed persisting console briefing hash", exc_info=True)
    return True


def reset_trade_decisions(client: RedisClient | None = None) -> dict[str, Any]:
    """Wipe stale Trade decisions (Redis stream + JSONL). Keeps today's entry lock."""
    from dashboard.components.agent_journal import DECISIONS_LOG, DECISIONS_STREAM

    cleared = {"redis_stream": False, "jsonl": False}
    if client is not None:
        try:
            client.client.delete(DECISIONS_STREAM)
            client.client.delete(BRIEFING_HASH_KEY)
            cleared["redis_stream"] = True
        except Exception:
            logger.exception("Failed deleting decisions stream")
    try:
        DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        DECISIONS_LOG.write_text("", encoding="utf-8")
        cleared["jsonl"] = True
    except OSError:
        logger.exception("Failed clearing decisions JSONL")
    return cleared


def rerun_desk(
    *,
    client: RedisClient | None,
    symbol: str = "NIFTY",
    controls: dict[str, Any] | None = None,
    refresh_sources: bool = True,
    broker: str = "dhan",
) -> dict[str, Any]:
    """Reset decisions, refresh Global/Voices/Insights inputs, force agent pulse + Trade decision."""
    from dashboard.components.capital import fetch_capital
    from dashboard.components.positions import fetch_positions
    from dashboard.components.risk_controls import is_trading_disabled, load_terminal_controls

    result: dict[str, Any] = {"symbol": symbol}
    result["cleared"] = reset_trade_decisions(client)

    if refresh_sources:
        try:
            from services.global_outlook import refresh_global_outlook

            snap = refresh_global_outlook()
            result["outlook"] = {"bias": snap.bias, "score": snap.score}
        except Exception as exc:
            result["outlook_error"] = str(exc)
        try:
            from services.live_market_voices import refresh_live_market

            voices = refresh_live_market(nse_lookback_days=31)
            result["voices"] = {
                "week": (voices.counts_by_horizon or {}).get("week"),
                "sources_ok": sum(1 for h in voices.source_health if h.get("status") == "ok"),
            }
        except Exception as exc:
            result["voices_error"] = str(exc)

    try:
        note = build_insight_from_market(symbol, redis_client=client)
        result["insight"] = {
            "title": note.title,
            "strategy": note.strategy_for_tomorrow[:160],
            "why": note.why[:160],
        }
    except Exception as exc:
        result["insight_error"] = str(exc)

    if controls is None and client is not None:
        controls = load_terminal_controls(client)
    controls = controls or {}

    try:
        rows, _ = fetch_positions(broker, redis_client=client)  # type: ignore[arg-type]
        pnl = float(sum(r.pnl for r in rows)) if rows else 0.0
    except Exception:
        pnl = None
    try:
        cap = fetch_capital(broker)  # type: ignore[arg-type]
        capital_ref = float(cap.available_margin or cap.total_capital or 0) or None
    except Exception:
        capital_ref = None

    day = classify_day_outcome(pnl, capital_ref=capital_ref)
    disabled = is_trading_disabled(controls)
    statuses = build_agent_statuses(
        client=client,
        symbol=symbol,
        controls=controls,
        day=day,
        trading_disabled=disabled,
    )
    wrote = sync_agent_briefing(
        client=client,
        symbol=symbol,
        controls=controls,
        day=day,
        statuses=statuses,
        force=True,
    )
    decisions, _ = load_decisions(client, limit=3)
    result.update(
        {
            "briefing_wrote": wrote,
            "day_grade": day.grade,
            "phase": session_clock().phase,
            "agents": [{"name": s.name, "status": s.status, "headline": s.headline} for s in statuses],
            "latest_decision": decisions[0] if decisions else None,
        }
    )
    return result


def recent_discussion(client: RedisClient | None, *, limit: int = 40) -> list[dict[str, Any]]:
    """Newest-first discussion rows for Console (latest updates on top)."""
    rows, _ = load_conversations(client, limit=limit)
    return rows
