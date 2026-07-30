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
    ACCEPTABLE_LOSS_PCT,
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
    consolidation: str = ""
    sources: dict[str, Any] = field(default_factory=dict)
    disclaimer: str = (
        "Nett impact = gross MTM − estimated brokerage/STT/exchange/GST/stamp. "
        "Estimates are indicative retail F&O proxies — confirm with broker contract notes."
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


def _band_edges(capital_ref: float | None) -> dict[DayGrade, tuple[float, float | None]]:
    """Inclusive lower / exclusive-ish upper nett bounds for each grade."""
    cap = capital_ref if capital_ref and capital_ref > 0 else None
    if cap:
        phen = cap * PHENOMENAL_PCT
        okay = cap * OKAY_PCT
        flat = max(FLAT_ABS, cap * 0.0005)
        acc = cap * ACCEPTABLE_LOSS_PCT
    else:
        phen, okay, flat, acc = 15_000.0, 2_000.0, FLAT_ABS, 8_000.0

    return {
        "PHENOMENAL": (phen, None),
        "OKAY": (okay, phen),
        "FLAT": (-flat, okay),
        "ACCEPTABLE_LOSS": (-acc, -flat),
        "BREACH": (float("-inf"), -acc),
    }


def build_framework(
    *,
    capital_ref: float | None,
    session_charges: ChargeEstimate,
) -> list[GradeBand]:
    edges = _band_edges(capital_ref)
    charges = float(session_charges.total)
    bands: list[GradeBand] = []
    for idx, grade in enumerate(GRADE_PRIORITY, start=1):
        lo, hi = edges[grade]
        # Gross MTM required so that nett (gross − charges) sits at the band's entry.
        if grade == "PHENOMENAL":
            target_nett = lo
        elif grade == "BREACH":
            # Crossing below −acc (hi of breach edge tuple is −acc exclusive upper in our map)
            target_nett = hi if hi is not None else -charges
        else:
            assert hi is not None
            target_nett = (lo + hi) / 2.0
        gross_to_enter = float(target_nett) + charges
        bands.append(
            GradeBand(
                grade=grade,
                priority=idx,
                nett_min=float(lo) if lo != float("-inf") else float("-inf"),
                nett_max=float(hi) if hi is not None else None,
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
    framework = build_framework(capital_ref=capital_ref, session_charges=charges)

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
