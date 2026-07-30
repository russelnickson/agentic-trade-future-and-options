"""Day Thesis agent — consolidate the session into a nett-impact grade framework.

Priority order (best → worst):
  PHENOMENAL → OKAY → FLAT → ACCEPTABLE_LOSS → BREACH

All bands are evaluated on **nett** P&L (gross MTM − estimated trade charges).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import get_settings
from dashboard.components.console_runtime import (
    FLAT_ABS,
    OKAY_PCT,
    PHENOMENAL_PCT,
    DayGrade,
    classify_day_outcome,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = PROJECT_ROOT / "data" / "insights"
SNAPSHOT_PATH = OUT_DIR / "day_thesis.json"
REDIS_KEY = "agent:thesis:today"

# Priority: lower number = chase first.
GRADE_PRIORITY: tuple[DayGrade, ...] = (
    "PHENOMENAL",
    "OKAY",
    "FLAT",
    "ACCEPTABLE_LOSS",
    "BREACH",
)

GRADE_MEANING = {
    "PHENOMENAL": "Strong green vs capital — clear outperformance (nett)",
    "OKAY": "Modest profit after charges — solid trade day",
    "FLAT": "No meaningful nett P&L — capital preserved",
    "ACCEPTABLE_LOSS": "Small nett loss inside budget — still a decent close",
    "BREACH": "Beyond budget after charges — cut / flatten",
}

GRADE_PLAYBOOK = {
    "PHENOMENAL": "Size only high-conviction setups; protect winners; do not overtrade into charges.",
    "OKAY": "Take clean edge; one or two structured prints; bank partials before close.",
    "FLAT": "Preserve capital; skip marginal ideas — idle cash beats fee-bleed.",
    "ACCEPTABLE_LOSS": "Tighten risk; no revenge size; stay inside day budget nett of fees.",
    "BREACH": "Square off / halt new risk — day budget broken after charges.",
}


@dataclass(frozen=True)
class ChargeEstimate:
    """Approximate NSE F&O options round-trip cost (INR)."""

    premium_turnover: float
    buy_orders: int
    sell_orders: int
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    total: float
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GradeBand:
    grade: DayGrade
    priority: int
    nett_min: float
    nett_max: float | None
    gross_to_enter: float
    estimated_charges_at_target: float
    meaning: str
    playbook: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data.get("nett_min") == float("-inf"):
            data["nett_min"] = None
        return data


@dataclass
class DayThesis:
    asof: str
    symbol: str
    capital_ref: float
    day_budget: float
    session_charges: ChargeEstimate
    current_gross_pnl: float | None
    current_nett_pnl: float | None
    current_grade: DayGrade
    framework: list[GradeBand] = field(default_factory=list)
    primary_target: DayGrade = "OKAY"
    target_profit_nett: float = 0.0
    target_profit_gross: float = 0.0
    gap_to_target_nett: float | None = None
    progress_pct: float | None = None
    consolidation: str = ""
    sources: dict[str, Any] = field(default_factory=dict)
    disclaimer: str = (
        "Nett P&L / loss = gross MTM − brokerage − STT − exchange − SEBI − stamp − GST. "
        "All Thesis grades and day targets use this nett figure. "
        "Fee model is an indicative retail F&O proxy — confirm with Dhan contract notes."
    )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["framework"] = [b.to_dict() if hasattr(b, "to_dict") else b for b in self.framework]
        data["session_charges"] = (
            self.session_charges.to_dict()
            if hasattr(self.session_charges, "to_dict")
            else self.session_charges
        )
        data["priority_order"] = list(GRADE_PRIORITY)
        return data


def estimate_option_charges(
    premium_turnover: float,
    *,
    buy_orders: int = 1,
    sell_orders: int = 1,
    brokerage_per_order: float = 20.0,
) -> ChargeEstimate:
    """
    Indicative NSE equity options cost model (INR).

    - Brokerage: flat ₹/order (common discount plan ceiling)
    - STT: 0.1% of sell-side premium (options)
    - Exchange txn: ~0.053% of premium turnover (buy+sell notionals approximated)
    - SEBI: ₹10 / crore of turnover
    - Stamp: 0.003% of buy-side premium
    - GST: 18% on (brokerage + exchange + sebi)
    """
    turnover = max(0.0, float(premium_turnover))
    buys = max(0, int(buy_orders))
    sells = max(0, int(sell_orders))
    # Split turnover roughly half buy / half sell when both sides present.
    sides = max(buys + sells, 1)
    buy_notional = turnover * (buys / sides)
    sell_notional = turnover * (sells / sides)

    brokerage = brokerage_per_order * (buys + sells)
    stt = 0.001 * sell_notional
    exchange = 0.00053 * turnover
    sebi = (10.0 / 1e7) * turnover
    stamp = 0.00003 * buy_notional
    gst = 0.18 * (brokerage + exchange + sebi)
    total = brokerage + stt + exchange + sebi + stamp + gst

    return ChargeEstimate(
        premium_turnover=round(turnover, 2),
        buy_orders=buys,
        sell_orders=sells,
        brokerage=round(brokerage, 2),
        stt=round(stt, 2),
        exchange=round(exchange, 2),
        sebi=round(sebi, 2),
        stamp=round(stamp, 2),
        gst=round(gst, 2),
        total=round(total, 2),
        notes=(
            f"Proxy on ₹{turnover:,.0f} premium turnover · "
            f"{buys} buy / {sells} sell orders @ ₹{brokerage_per_order:.0f}/order"
        ),
    )


def nett_pnl(gross: float | None, charges: float) -> float | None:
    if gross is None:
        return None
    return float(gross) - float(charges)


def _band_edges(
    capital_ref: float | None,
    *,
    day_budget: float,
) -> dict[DayGrade, tuple[float | None, float | None]]:
    """Nett bounds per grade: ``(min_inclusive, max_exclusive)``; ``None`` = open end."""
    cap = capital_ref if capital_ref and capital_ref > 0 else None
    budget = max(float(day_budget), 1.0)
    if cap:
        phen = cap * PHENOMENAL_PCT
        okay = cap * OKAY_PCT
        flat_thr = max(FLAT_ABS, cap * 0.0005)
    else:
        phen, okay, flat_thr = 15_000.0, 2_000.0, FLAT_ABS

    # Acceptable loss sits between flat and the hard day budget (ensure lo < hi).
    acc_floor = -budget
    flat_floor = -flat_thr
    if acc_floor >= flat_floor:
        # Tiny budget vs flat threshold — still expose a sliver for ACCEPTABLE_LOSS.
        acc_floor = flat_floor - max(budget, 1.0)

    return {
        "PHENOMENAL": (phen, None),
        "OKAY": (okay, phen),
        "FLAT": (flat_floor, okay),
        "ACCEPTABLE_LOSS": (acc_floor, flat_floor),
        "BREACH": (None, acc_floor),
    }


def build_framework(
    *,
    capital_ref: float | None,
    session_charges: ChargeEstimate,
    day_budget: float,
) -> list[GradeBand]:
    edges = _band_edges(capital_ref, day_budget=day_budget)
    charges = float(session_charges.total)
    bands: list[GradeBand] = []
    for idx, grade in enumerate(GRADE_PRIORITY, start=1):
        lo, hi = edges[grade]
        if grade == "PHENOMENAL":
            target_nett = float(lo or 0.0)
        elif grade == "BREACH":
            target_nett = float(hi or -charges)
        else:
            assert lo is not None and hi is not None
            target_nett = (lo + hi) / 2.0
        gross_to_enter = float(target_nett) + charges
        bands.append(
            GradeBand(
                grade=grade,
                priority=idx,
                nett_min=float("-inf") if lo is None else float(lo),
                nett_max=None if hi is None else float(hi),
                gross_to_enter=round(gross_to_enter, 2),
                estimated_charges_at_target=round(charges, 2),
                meaning=GRADE_MEANING[grade],
                playbook=GRADE_PLAYBOOK[grade],
            )
        )
    return bands


def _pick_primary_target(
    current: DayGrade,
    *,
    outlook_bias: str,
) -> DayGrade:
    """What the desk should aim for given where we are and the tape."""
    if current == "BREACH":
        return "ACCEPTABLE_LOSS"  # climb out; do not chase PHENOMENAL from breach
    if current == "ACCEPTABLE_LOSS":
        return "FLAT"
    if current == "FLAT":
        bias = (outlook_bias or "").upper()
        if bias in {"BULLISH", "BEARISH", "RISK_ON"}:
            return "OKAY"
        return "FLAT"
    if current == "OKAY":
        return "PHENOMENAL"
    if current == "PHENOMENAL":
        return "PHENOMENAL"
    # NO_DATA / unknown — start of day
    return "OKAY"


def _consolidate_narrative(
    *,
    symbol: str,
    current_grade: DayGrade,
    primary: DayGrade,
    capital_ref: float,
    day_budget: float,
    charges: ChargeEstimate,
    outlook: dict[str, Any],
    strategies: list[dict[str, Any]],
    speculation: dict[str, Any],
) -> str:
    top = strategies[0] if strategies else {}
    strat = top.get("name") or top.get("strategy_id") or "no ranked strategy yet"
    conf = top.get("confidence")
    conf_s = f"{float(conf):.0%}" if isinstance(conf, (int, float)) else "n/a"
    bias = outlook.get("bias") or outlook.get("headline") or "bias n/a"
    spec_line = ""
    if speculation.get("status") == "LIVE" or speculation.get("headline"):
        spec_line = f" Broker speculation: {speculation.get('headline') or 'n/a'}."
    return (
        f"Thesis agent · {symbol}: chase **{primary}** (priority ladder nett of charges). "
        f"Now marked **{current_grade}**. Capital ref ₹{capital_ref:,.0f}; "
        f"day loss budget ₹{day_budget:,.0f}; session fee proxy ₹{charges.total:,.2f} "
        f"on ₹{charges.premium_turnover:,.0f} premium turnover. "
        f"Scout/outlook: {bias}. Top structure: {strat} (conf {conf_s})."
        f"{spec_line} "
        f"All decisions use **nett impact** after trade charges — not raw MTM."
    )


def resolve_day_profit_target(
    framework: list[GradeBand],
    primary: DayGrade,
) -> tuple[float, float]:
    """
    Day profit target = nett needed for a productive close.

    Uses the primary chase when it is OKAY/PHENOMENAL; otherwise defaults to
    entering **OKAY** so FLAT/repair modes still show a positive day target.
    """
    by_grade = {b.grade: b for b in framework}
    okay = by_grade.get("OKAY")
    phen = by_grade.get("PHENOMENAL")
    if primary == "PHENOMENAL" and phen is not None:
        band = phen
        target_nett = float(band.nett_min if band.nett_min != float("-inf") else 0.0)
    elif primary == "OKAY" and okay is not None:
        band = okay
        target_nett = float(band.nett_min if band.nett_min != float("-inf") else 0.0)
    elif okay is not None:
        # FLAT / ACCEPTABLE_LOSS / BREACH / NO_DATA — still aim for OKAY nett profit
        band = okay
        target_nett = float(band.nett_min if band.nett_min != float("-inf") else 0.0)
    else:
        band = framework[0]
        target_nett = float(band.nett_min if band.nett_min != float("-inf") else 0.0)
    fees = float(band.estimated_charges_at_target)
    target_gross = target_nett + fees
    return round(target_nett, 2), round(target_gross, 2)


def progress_to_target(achieved_nett: float | None, target_nett: float) -> tuple[float | None, float | None]:
    """Return ``(gap_to_target, progress_pct)`` where progress is 0–100+ toward target."""
    if achieved_nett is None:
        return None, None
    gap = round(float(target_nett) - float(achieved_nett), 2)
    if target_nett <= 0:
        # Flat / loss-repair targets: 100% when achieved >= target
        pct = 100.0 if achieved_nett >= target_nett else max(0.0, min(99.0, 50.0 + achieved_nett))
        return gap, round(pct, 1)
    pct = max(0.0, (float(achieved_nett) / float(target_nett)) * 100.0)
    return gap, round(pct, 1)


def _tick_ltp(redis_client: Any, token: str | int | None) -> float | None:
    if redis_client is None or token is None:
        return None
    try:
        raw = redis_client.client.get(f"tick:{token}")
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not raw:
            return None
        if str(raw).startswith("{"):
            data = json.loads(raw)
            for k in ("ltp", "last_price", "last_traded_price", "LTP"):
                if data.get(k) is not None:
                    return float(data[k])
        return float(raw)
    except Exception:
        return None


def _live_trade_rows(
    *,
    broker: str,
    redis_client: Any | None,
) -> tuple[list[dict[str, Any]], float, int, int]:
    """Open + closed day positions with live LTP where available.

    Returns ``(rows, premium_turnover, buy_orders, sell_orders)``.
    """
    rows: list[dict[str, Any]] = []
    turnover = 0.0
    buys = 0
    sells = 0
    try:
        from services.circuit_breaker import _unwrap_list
        from config.settings import get_settings

        settings = get_settings()
        if broker == "dhan":
            from dhanhq import DhanContext, Portfolio

            ctx = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
            payload = Portfolio(ctx).get_positions()
            for pos in _unwrap_list(payload):
                qty = int(pos.get("netQty") or 0)
                buy_qty = int(pos.get("dayBuyQty") or pos.get("buyQty") or 0)
                sell_qty = int(pos.get("daySellQty") or pos.get("sellQty") or 0)
                realized = float(pos.get("realizedProfit") or 0)
                unrealized = float(pos.get("unrealizedProfit") or 0)
                if qty == 0 and buy_qty == 0 and sell_qty == 0 and not realized and not unrealized:
                    continue
                token = pos.get("securityId")
                live = _tick_ltp(redis_client, token)
                entry = float(pos.get("costPrice") or pos.get("buyAvg") or 0) or None
                if qty < 0:
                    entry = float(pos.get("sellAvg") or entry or 0) or entry
                if live is None and qty != 0 and entry is not None and unrealized:
                    live = entry + (unrealized / qty)
                day_buy_val = float(pos.get("dayBuyValue") or 0)
                day_sell_val = float(pos.get("daySellValue") or 0)
                if day_buy_val or day_sell_val:
                    turnover += abs(day_buy_val) + abs(day_sell_val)
                else:
                    prem = float(live or entry or 0)
                    turnover += prem * max(abs(qty), abs(buy_qty), abs(sell_qty), 0)
                if buy_qty > 0:
                    buys += 1
                elif qty > 0:
                    buys += 1
                if sell_qty > 0:
                    sells += 1
                elif qty < 0:
                    sells += 1
                status = (
                    "OPEN"
                    if qty != 0
                    else ("CLOSED" if (buy_qty or sell_qty or realized) else "FLAT")
                )
                opt_raw = str(pos.get("drvOptionType") or "").upper()
                if opt_raw in {"CALL", "CE", "C"}:
                    opt = "CE"
                elif opt_raw in {"PUT", "PE", "P"}:
                    opt = "PE"
                else:
                    opt = None
                rows.append(
                    {
                        "symbol": str(pos.get("tradingSymbol") or token or "—"),
                        "security_id": str(token) if token is not None else None,
                        "option_type": opt,
                        "strike": pos.get("drvStrikePrice"),
                        "qty": qty,
                        "entry": entry,
                        "ltp": None if live is None else round(float(live), 2),
                        "realized": round(realized, 2),
                        "unrealized": round(unrealized, 2),
                        "pnl": round(realized + unrealized, 2),
                        "status": status,
                    }
                )
        else:
            from dashboard.components.positions import fetch_positions

            open_rows, _err = fetch_positions(broker, redis_client=redis_client)  # type: ignore[arg-type]
            for r in open_rows or []:
                if r.qty > 0:
                    buys += 1
                elif r.qty < 0:
                    sells += 1
                turnover += abs(float(r.ltp or r.entry_price or 0) * abs(int(r.qty)))
                rows.append(
                    {
                        "symbol": r.symbol,
                        "security_id": str(r.token) if r.token is not None else None,
                        "option_type": r.option_type,
                        "strike": r.strike,
                        "qty": r.qty,
                        "entry": r.entry_price,
                        "ltp": r.ltp,
                        "realized": 0.0,
                        "unrealized": float(r.pnl or 0),
                        "pnl": float(r.pnl or 0),
                        "status": "OPEN",
                    }
                )
    except Exception:
        logger.debug("thesis live trade rows failed", exc_info=True)
    return rows, turnover, max(buys, 0), max(sells, 0)


def live_market_tick(
    symbol: str,
    *,
    broker: str = "dhan",
    redis_client: Any | None = None,
    session_charges_total: float = 0.0,
) -> dict[str, Any]:
    """Live underlying + day P&L (realized+unrealized) + executed trade insights."""
    symbol_u = symbol.strip().upper()
    ltp = None
    atm = None
    pcr = None
    chain_age = None
    try:
        if redis_client is not None:
            chain = redis_client.get_option_chain_state(symbol_u) or {}
            ltp = chain.get("underlying_ltp")
            atm = chain.get("atm_strike") or chain.get("atm")
            pcr = chain.get("pcr")
            chain_age = chain.get("asof") or chain.get("updated_at")
            if pcr is None:
                try:
                    from services.oi_tracker import compute_pcr

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
                except Exception:
                    pass
    except Exception:
        logger.debug("thesis live chain failed", exc_info=True)

    realized = 0.0
    unrealized = 0.0
    gross = None
    pnl_err = None
    try:
        from services.circuit_breaker import fetch_daily_pnl

        snap = fetch_daily_pnl(broker if broker in {"dhan", "zerodha"} else "dhan")  # type: ignore[arg-type]
        if snap.error:
            pnl_err = snap.error
        realized = float(snap.realized_pnl or 0)
        unrealized = float(snap.unrealized_pnl or 0)
        gross = float(snap.total_pnl)
    except Exception as exc:
        logger.debug("thesis daily pnl failed", exc_info=True)
        pnl_err = str(exc)

    trades, turnover, buys, sells = _live_trade_rows(broker=broker, redis_client=redis_client)

    # Prefer live fee estimate from today's churn; fall back to thesis session model.
    live_fees = float(session_charges_total or 0)
    if turnover > 0 and (buys + sells) > 0:
        live_fees = estimate_option_charges(
            turnover,
            buy_orders=max(buys, 1),
            sell_orders=max(sells, 0) or (1 if buys else 0),
        ).total

    # Enrich open sleeves from day-risk book (planned stop / order id)
    sleeves: list[dict[str, Any]] = []
    try:
        from services.intraday_hunt import load_day_risk

        state = load_day_risk(redis_client)
        for s in state.get("sleeves") or []:
            if not isinstance(s, dict):
                continue
            sec = s.get("security_id")
            live = _tick_ltp(redis_client, sec)
            entry = s.get("ltp")
            qty = int(s.get("qty") or 0)
            upnl = None
            if live is not None and entry is not None and qty:
                upnl = round((float(live) - float(entry)) * qty, 2)
            sleeves.append(
                {
                    "order_id": s.get("order_id"),
                    "security_id": sec,
                    "strike": s.get("strike"),
                    "option_type": s.get("option_type"),
                    "qty": qty,
                    "entry": entry,
                    "ltp": live,
                    "stop_price": s.get("stop_price"),
                    "unrealized": upnl,
                    "stopped": bool(s.get("stopped")),
                    "asof": s.get("asof"),
                }
            )
    except Exception:
        logger.debug("thesis day-risk sleeves failed", exc_info=True)

    nett = nett_pnl(gross, live_fees) if gross is not None else None
    open_n = sum(1 for t in trades if t.get("status") == "OPEN")
    closed_n = sum(1 for t in trades if t.get("status") == "CLOSED")
    insight = (
        f"{open_n} open · {closed_n} closed · "
        f"realized ₹{realized:+,.0f} · unrealized ₹{unrealized:+,.0f}"
    )
    if not trades and gross == 0:
        insight = "No executed day trades yet — capital idle vs target"

    return {
        "symbol": symbol_u,
        "underlying_ltp": None if ltp is None else float(ltp),
        "atm": atm,
        "pcr": pcr,
        "chain_asof": chain_age,
        "gross_pnl": None if gross is None else round(float(gross), 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "nett_pnl": None if nett is None else round(float(nett), 2),
        "fees_live": round(live_fees, 2),
        "trades": trades,
        "sleeves": sleeves,
        "insight": insight,
        "pnl_error": pnl_err,
        "asof": datetime.now(timezone.utc).isoformat(),
    }


def build_day_thesis(
    symbol: str = "NIFTY",
    *,
    gross_pnl: float | None = None,
    capital_ref: float | None = None,
    premium_turnover: float | None = None,
    buy_orders: int = 2,
    sell_orders: int = 2,
    redis_client: Any | None = None,
) -> DayThesis:
    """Consolidate desk inputs into a nett-impact day framework."""
    settings = get_settings()
    day_budget = float(settings.max_daily_loss)

    if capital_ref is None or capital_ref <= 0:
        try:
            from dashboard.components.capital import fetch_capital

            snap = fetch_capital(settings.trade_broker if settings.trade_broker in {"dhan", "zerodha"} else "dhan")  # type: ignore[arg-type]
            capital_ref = float(snap.available_margin or snap.total_capital or 0) or 50_000.0
        except Exception:
            capital_ref = 50_000.0

    if premium_turnover is None or premium_turnover <= 0:
        # Default: ~2% of capital as options premium churn for fee proxy.
        premium_turnover = max(5_000.0, float(capital_ref) * 0.02)

    charges = estimate_option_charges(
        float(premium_turnover),
        buy_orders=buy_orders,
        sell_orders=sell_orders,
    )
    nett = nett_pnl(gross_pnl, charges.total)
    outcome = classify_day_outcome(nett, capital_ref=capital_ref)
    framework = build_framework(
        capital_ref=capital_ref,
        session_charges=charges,
        day_budget=day_budget,
    )

    outlook: dict[str, Any] = {}
    try:
        from services.global_outlook import load_snapshot

        snap = load_snapshot() or {}
        outlook = {
            "bias": snap.get("bias") or snap.get("regime"),
            "headline": snap.get("headline") or snap.get("summary"),
            "score": snap.get("score"),
        }
    except Exception:
        logger.debug("thesis outlook load failed", exc_info=True)

    strategies: list[dict[str, Any]] = []
    try:
        from services.day_strategy_backtest import load_strategies

        payload = load_strategies(symbol, redis_client=redis_client) or {}
        strategies = list(payload.get("strategies") or [])[:5]
    except Exception:
        logger.debug("thesis strategies load failed", exc_info=True)

    speculation: dict[str, Any] = {}
    try:
        from dashboard.components.broker_speculation import load_speculation

        spec = load_speculation(redis_client)
        if spec:
            speculation = {
                "status": "LIVE",
                "headline": getattr(spec, "headline", None) or str(spec),
                "signals": dict(getattr(spec, "signals", {}) or {}),
            }
    except Exception:
        logger.debug("thesis speculation load failed", exc_info=True)

    primary = _pick_primary_target(
        outcome.grade if outcome.grade != "NO_DATA" else "FLAT",
        outlook_bias=str(outlook.get("bias") or ""),
    )
    if outcome.grade == "NO_DATA":
        primary = "OKAY"

    target_nett, target_gross = resolve_day_profit_target(framework, primary)
    gap, progress = progress_to_target(nett, target_nett)

    narrative = _consolidate_narrative(
        symbol=symbol.strip().upper(),
        current_grade=outcome.grade,
        primary=primary,
        capital_ref=float(capital_ref),
        day_budget=day_budget,
        charges=charges,
        outlook=outlook,
        strategies=strategies,
        speculation=speculation,
    )
    narrative = (
        f"{narrative} Day target nett ₹{target_nett:+,.0f} "
        f"(gross ≈ ₹{target_gross:+,.0f} incl. fees)."
    )

    return DayThesis(
        asof=datetime.now(timezone.utc).isoformat(),
        symbol=symbol.strip().upper(),
        capital_ref=round(float(capital_ref), 2),
        day_budget=day_budget,
        session_charges=charges,
        current_gross_pnl=None if gross_pnl is None else round(float(gross_pnl), 2),
        current_nett_pnl=None if nett is None else round(float(nett), 2),
        current_grade=outcome.grade,
        framework=framework,
        primary_target=primary,
        target_profit_nett=target_nett,
        target_profit_gross=target_gross,
        gap_to_target_nett=gap,
        progress_pct=progress,
        consolidation=narrative,
        sources={
            "outlook": outlook,
            "strategies": [
                {
                    "rank": s.get("rank"),
                    "name": s.get("name") or s.get("strategy_id"),
                    "confidence": s.get("confidence"),
                    "structure": s.get("structure"),
                }
                for s in strategies
            ],
            "speculation": speculation,
            "gross_grade_message": outcome.message,
        },
    )


def persist_thesis(thesis: DayThesis, *, redis_client: Any | None = None) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = thesis.to_dict()
    path = OUT_DIR / f"day_thesis_{thesis.symbol.lower()}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    SNAPSHOT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    if redis_client is not None:
        try:
            raw = json.dumps(payload, default=str)
            redis_client.client.set(REDIS_KEY, raw)
            redis_client.client.set(f"{REDIS_KEY}:{thesis.symbol}", raw)
        except Exception:
            logger.debug("Redis thesis persist failed", exc_info=True)
    return path


def load_thesis(
    symbol: str = "NIFTY",
    *,
    redis_client: Any | None = None,
) -> dict[str, Any] | None:
    symbol_u = symbol.strip().upper()
    if redis_client is not None:
        try:
            raw = redis_client.client.get(f"{REDIS_KEY}:{symbol_u}") or redis_client.client.get(
                REDIS_KEY
            )
            if raw:
                data = json.loads(raw)
                if str(data.get("symbol", "")).upper() in {"", symbol_u}:
                    return data
        except Exception:
            logger.debug("Redis thesis load failed", exc_info=True)
    path = OUT_DIR / f"day_thesis_{symbol_u.lower()}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    if SNAPSHOT_PATH.is_file():
        data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        if str(data.get("symbol", "")).upper() in {"", symbol_u}:
            return data
    return None


def refresh_day_thesis(
    symbol: str = "NIFTY",
    *,
    gross_pnl: float | None = None,
    capital_ref: float | None = None,
    premium_turnover: float | None = None,
    redis_client: Any | None = None,
) -> dict[str, Any]:
    thesis = build_day_thesis(
        symbol,
        gross_pnl=gross_pnl,
        capital_ref=capital_ref,
        premium_turnover=premium_turnover,
        redis_client=redis_client,
    )
    persist_thesis(thesis, redis_client=redis_client)
    return thesis.to_dict()
