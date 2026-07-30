"""Intraday hunt — probabilistic capital deployment inside today's NSE F&O session.

Doctrine
--------
- Idle cash is opportunity cost, but **never all-in**.
- Size by confidence-weighted sleeves of ``MAX_DAILY_LOSS``.
- Strict stop: planned loss ≤ stop_fraction × premium (default 35%).
- Leave reserve for later sleeves (max capital efficiency under a hard day-loss cap).
- One protected LIMIT BUY per sleeve; up to ``TRADE_MAX_SLEEVES`` (default 3) per day.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from database.redis_client import RedisClient, get_redis_client

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
ENTRY_LOCK_KEY = "agent:console:today_entry_lock"
DAY_RISK_KEY = "agent:console:day_risk"
DEFAULT_LOT = {"NIFTY": 65, "BANKNIFTY": 15}

# Probabilistic sleeve: share of MAX_DAILY_LOSS risked on this hunt (never ≥ 1/3).
SLEEVE_MIN = 0.12
SLEEVE_MAX = 0.28
# Hard ceiling on total day risk deployed across sleeves (keep powder dry).
DAY_RISK_UTIL_CAP = 0.70
# Planned stop as fraction of entry premium (strict loss control for long options).
STOP_FRACTION = 0.35
# Max premium notional vs available margin (capital efficiency, not margin blow-up).
MARGIN_PREMIUM_CAP = 0.15


def _today() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def auto_execute_enabled() -> bool:
    """Hunt & execute on EC2 live; local paper defaults to propose/dry-run only."""
    try:
        from config.runtime_mode import auto_execute_default

        return bool(auto_execute_default())
    except Exception:
        raw = (os.getenv("TRADE_AUTO_EXECUTE") or "1").strip().lower()
        return raw not in {"0", "false", "no", "off"}


def lot_size(symbol: str) -> int:
    env = (os.getenv("TRADE_LOT_SIZE") or "").strip()
    if env.isdigit():
        return max(1, int(env))
    return int(DEFAULT_LOT.get(symbol.upper(), 65))


def max_daily_loss() -> float:
    raw = (os.getenv("MAX_DAILY_LOSS") or "5000").strip()
    try:
        return max(500.0, float(raw))
    except ValueError:
        return 5000.0


def max_sleeves() -> int:
    raw = (os.getenv("TRADE_MAX_SLEEVES") or "3").strip()
    try:
        return max(1, min(5, int(raw)))
    except ValueError:
        return 3


def stop_fraction() -> float:
    raw = (os.getenv("TRADE_STOP_FRACTION") or str(STOP_FRACTION)).strip()
    try:
        return min(0.6, max(0.15, float(raw)))
    except ValueError:
        return STOP_FRACTION


def sleeve_weight(confidence: float | None) -> float:
    """Map strategy confidence → probabilistic share of day-loss budget (not all-in)."""
    if confidence is None:
        c = 0.65
    else:
        c = min(0.95, max(0.45, float(confidence)))
    # Linear map: 0.45 → SLEEVE_MIN, 0.95 → SLEEVE_MAX
    t = (c - 0.45) / (0.95 - 0.45)
    return round(SLEEVE_MIN + t * (SLEEVE_MAX - SLEEVE_MIN), 4)


def _lock_payload(client: RedisClient) -> dict[str, Any] | None:
    try:
        raw = client.client.get(ENTRY_LOCK_KEY)
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_day_risk(client: RedisClient | None) -> dict[str, Any]:
    empty = {
        "date": _today(),
        "deployed_risk": 0.0,
        "sleeves": [],
        "max_daily_loss": max_daily_loss(),
    }
    if client is None:
        return empty
    try:
        raw = client.client.get(DAY_RISK_KEY)
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not raw:
            return empty
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("date") != _today():
            return empty
        data.setdefault("deployed_risk", 0.0)
        data.setdefault("sleeves", [])
        data["max_daily_loss"] = max_daily_loss()
        return data
    except Exception:
        return empty


def save_day_risk(client: RedisClient, state: dict[str, Any]) -> None:
    body = dict(state)
    body["date"] = _today()
    body["asof"] = datetime.now(IST).isoformat()
    body["max_daily_loss"] = max_daily_loss()
    client.client.set(DAY_RISK_KEY, json.dumps(body, default=str), ex=60 * 60 * 14)


def remaining_risk_budget(client: RedisClient | None) -> dict[str, Any]:
    """How much day-loss budget is left for the next sleeve."""
    mdl = max_daily_loss()
    state = load_day_risk(client)
    deployed = float(state.get("deployed_risk") or 0.0)
    util_cap = mdl * DAY_RISK_UTIL_CAP
    remaining = max(0.0, util_cap - deployed)
    n = len(state.get("sleeves") or [])
    return {
        "max_daily_loss": mdl,
        "util_cap": util_cap,
        "deployed_risk": deployed,
        "remaining": remaining,
        "sleeves": n,
        "max_sleeves": max_sleeves(),
        "can_hunt": remaining >= 200.0 and n < max_sleeves(),
        "state": state,
    }


def entry_lock_state(client: RedisClient | None) -> dict[str, Any]:
    """Classify last entry attempt: none | success | cooldown_fail.

    Success no longer blocks the whole day if risk budget remains — see remaining_risk_budget.
    """
    if client is None:
        return {"state": "none"}
    lock = _lock_payload(client)
    if not lock or lock.get("date") != _today():
        return {"state": "none"}
    if lock.get("success") is True:
        return {"state": "success", "lock": lock}
    try:
        asof = datetime.fromisoformat(str(lock.get("asof")))
        if asof.tzinfo is None:
            asof = asof.replace(tzinfo=IST)
        age = (datetime.now(IST) - asof).total_seconds()
    except Exception:
        age = 0.0
    err = str(lock.get("error") or "")
    hard = "DH-905" in err or "Invalid IP" in err or "Input_Exception" in err
    ttl = 1800 if hard else 180
    if age < ttl:
        return {"state": "cooldown_fail", "lock": lock, "error": err, "ttl": ttl, "age": age}
    return {"state": "none", "lock": lock}


def already_entered_today(client: RedisClient | None) -> bool:
    """True when we should not fire another order right now."""
    st = entry_lock_state(client)
    if st.get("state") == "cooldown_fail":
        return True
    budget = remaining_risk_budget(client)
    if not budget["can_hunt"]:
        return True
    # Recent success: short cool-down so we don't double-fire same pulse
    if st.get("state") == "success":
        lock = st.get("lock") or {}
        try:
            asof = datetime.fromisoformat(str(lock.get("asof")))
            if asof.tzinfo is None:
                asof = asof.replace(tzinfo=IST)
            if (datetime.now(IST) - asof).total_seconds() < 120:
                return True
        except Exception:
            return True
    return False


def _set_lock(client: RedisClient, payload: dict[str, Any]) -> None:
    body = dict(payload)
    body["date"] = _today()
    body["asof"] = datetime.now(IST).isoformat()
    err = str(body.get("error") or "")
    hard = "DH-905" in err or "Invalid IP" in err
    if body.get("success"):
        ttl = 60 * 60 * 8
    elif hard:
        ttl = 1800
    else:
        ttl = 180
    client.client.set(ENTRY_LOCK_KEY, json.dumps(body, default=str), ex=ttl)


def clear_entry_lock(client: RedisClient | None = None) -> bool:
    client = client or get_redis_client()
    try:
        client.client.delete(ENTRY_LOCK_KEY)
        return True
    except Exception:
        return False


def _pick_side(
    *,
    bias: str,
    pcr: float | None,
    strategy_id: str,
) -> tuple[str, str]:
    """Return (CE|PE, reason). Always BUY (defined debit risk = premium × stop)."""
    b = (bias or "").upper()
    sid = (strategy_id or "").lower()

    if "bear_call" in sid or sid.startswith("bear"):
        return "PE", "top strategy bearish → BUY PE debit (sized sleeve)"
    if "bull_put" in sid or "breakout_long" in sid or "buy_dip" in sid:
        return "CE", f"top strategy {strategy_id or 'bullish'} → BUY CE debit (sized sleeve)"
    if "iron_fly" in sid or "range" in sid or "vol_crush" in sid:
        if pcr is not None and pcr >= 1.1:
            return "CE", "range plan + put-heavy PCR → lean BUY CE sleeve"
        if pcr is not None and pcr <= 0.85:
            return "PE", "range plan + call-heavy PCR → lean BUY PE sleeve"
        if b.startswith("BEAR"):
            return "PE", "range plan + Scout bear → BUY PE sleeve"
        return "CE", "range plan → BUY CE sleeve (partial capital)"

    if b.startswith("BEAR"):
        return "PE", f"Scout {bias} → BUY PE sleeve"
    if b.startswith("BULL"):
        return "CE", f"Scout {bias} → BUY CE sleeve"

    if pcr is not None and pcr >= 1.05:
        return "CE", f"mixed · PCR {pcr:.2f} → BUY CE sleeve"
    if pcr is not None and pcr <= 0.9:
        return "PE", f"mixed · PCR {pcr:.2f} → BUY PE sleeve"
    return "CE", "mixed · default BUY CE sleeve (not all-in)"


def _strike_candidates(chain: dict[str, Any], atm: float, opt: str) -> list[tuple[float, dict[str, Any]]]:
    """ATM first, then OTM in direction of option type (cheaper premium → fits loss budget)."""
    strikes = chain.get("strikes") or {}
    parsed: list[tuple[float, dict[str, Any]]] = []
    for k, sides in strikes.items():
        try:
            sk = float(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(sides, dict):
            continue
        side = sides.get(opt) or {}
        if not isinstance(side, dict):
            continue
        if side.get("token") is None or not side.get("ltp"):
            continue
        parsed.append((sk, side))
    if opt == "CE":
        # ATM then higher (OTM calls)
        parsed.sort(key=lambda x: (abs(x[0] - atm), x[0]))
    else:
        # ATM then lower (OTM puts)
        parsed.sort(key=lambda x: (abs(x[0] - atm), -x[0]))
    return parsed


def size_sleeve(
    *,
    ltp: float,
    lot: int,
    confidence: float | None,
    remaining_day_risk: float,
    available_margin: float | None = None,
) -> dict[str, Any]:
    """Probabilistic size: ≤1–2 lots; planned ₹ risk ≤ confidence sleeve of MAX_DAILY_LOSS.

    Preferred stop = ``stop_fraction`` of premium. If that exceeds the sleeve,
    tighten the rupee stop to the sleeve (still never all-in). Reject if the
    implied stop is < 8% of premium (noise / untradeable).
    """
    mdl = max_daily_loss()
    weight = sleeve_weight(confidence)
    sleeve_budget = min(remaining_day_risk, mdl * weight)
    sf = stop_fraction()
    premium_per_lot = float(ltp) * lot
    preferred_risk_per_lot = sf * premium_per_lot

    max_lots_margin = 2
    if available_margin and available_margin > 0:
        max_premium = available_margin * MARGIN_PREMIUM_CAP
        max_lots_margin = max(0, int(math.floor(max_premium / premium_per_lot)))

    if preferred_risk_per_lot <= 0 or sleeve_budget < 200:
        return {
            "ok": False,
            "reason": "sleeve budget too small",
            "sleeve_weight": weight,
            "sleeve_budget": sleeve_budget,
        }

    # Prefer 1 lot; allow 2 only if preferred risk × 2 fits with residual headroom
    lots = 1
    if (
        preferred_risk_per_lot * 2 <= sleeve_budget
        and max_lots_margin >= 2
        and sleeve_budget >= preferred_risk_per_lot * 2.2
    ):
        lots = 2
    lots = min(lots, max(1, max_lots_margin), 2)

    preferred_total = preferred_risk_per_lot * lots
    if preferred_total <= sleeve_budget:
        planned_risk = preferred_total
        stop_frac_used = sf
    else:
        # Tighten stop to hard sleeve rupee cap (strict loss control)
        planned_risk = sleeve_budget
        stop_frac_used = planned_risk / (premium_per_lot * lots)
        if stop_frac_used < 0.08:
            return {
                "ok": False,
                "reason": (
                    f"Even tight stop for {lots} lot @ LTP {ltp:.2f} needs ≥8% premium; "
                    f"sleeve ₹{sleeve_budget:,.0f} only covers {stop_frac_used:.1%}. "
                    f"Try cheaper OTM or raise MAX_DAILY_LOSS."
                ),
                "sleeve_weight": weight,
                "sleeve_budget": sleeve_budget,
                "risk_per_lot": preferred_risk_per_lot,
                "stop_fraction": sf,
            }

    qty = lots * lot
    return {
        "ok": True,
        "lots": lots,
        "qty": qty,
        "sleeve_weight": weight,
        "sleeve_budget": sleeve_budget,
        "planned_risk": planned_risk,
        "risk_per_lot": planned_risk / lots,
        "stop_fraction": stop_frac_used,
        "stop_fraction_preferred": sf,
        "premium_notional": premium_per_lot * lots,
        "max_daily_loss": mdl,
    }


def build_hunt_plan(
    *,
    client: RedisClient,
    symbol: str,
    bias: str,
    pcr: float | None,
    strategy_id: str = "",
    strategy_title: str = "",
    confidence: float | None = None,
    available_margin: float | None = None,
) -> dict[str, Any] | None:
    chain = client.get_option_chain_state(symbol) or {}
    atm = chain.get("atm")
    if atm is None or not chain.get("strikes"):
        return None
    atm_f = float(atm)
    opt, reason = _pick_side(bias=bias, pcr=pcr, strategy_id=strategy_id)
    lot = lot_size(symbol)
    budget = remaining_risk_budget(client)
    if not budget["can_hunt"]:
        return {
            "skip": True,
            "reason": (
                f"Day risk utilised ₹{budget['deployed_risk']:,.0f}/₹{budget['util_cap']:,.0f} "
                f"or sleeves {budget['sleeves']}/{budget['max_sleeves']} — preserve loss budget"
            ),
            "budget": budget,
        }

    candidates = _strike_candidates(chain, atm_f, opt)
    if not candidates:
        return None

    last_fail: dict[str, Any] | None = None
    for strike, side in candidates:
        ltp = float(side["ltp"])
        sizing = size_sleeve(
            ltp=ltp,
            lot=lot,
            confidence=confidence,
            remaining_day_risk=float(budget["remaining"]),
            available_margin=available_margin,
        )
        if not sizing.get("ok"):
            last_fail = sizing
            continue  # try further OTM (cheaper)
        token = side["token"]
        trading_symbol = f"{symbol}{chain.get('expiry') or ''}{int(strike)}{opt}"
        return {
            "symbol": symbol,
            "trading_symbol": trading_symbol,
            "option_type": opt,
            "action": "BUY",
            "strike": float(strike),
            "atm": atm_f,
            "expiry": chain.get("expiry"),
            "security_id": str(int(token)),
            "ltp": ltp,
            "qty": int(sizing["qty"]),
            "lots": int(sizing["lots"]),
            "underlying_ltp": chain.get("underlying_ltp"),
            "reason": reason,
            "strategy_id": strategy_id,
            "strategy_title": strategy_title,
            "bias": bias,
            "pcr": pcr,
            "confidence": confidence,
            "sizing": sizing,
            "budget": budget,
            "stop_price": round(ltp * (1.0 - float(sizing["stop_fraction"])), 2),
        }

    return {
        "skip": True,
        "reason": (last_fail or {}).get("reason")
        or "No strike fits probabilistic loss sleeve — capital held in reserve",
        "budget": budget,
        "sizing_fail": last_fail,
    }


def execute_hunt(
    plan: dict[str, Any],
    *,
    redis_client: RedisClient | None = None,
    dry_run: bool | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Place protected LIMIT BUY for a sized sleeve; book risk against day budget."""
    client = redis_client or get_redis_client()
    if plan.get("skip"):
        return {"ok": False, "skipped": True, "reason": plan.get("reason"), "plan": plan}
    if not force and already_entered_today(client):
        return {
            "ok": False,
            "skipped": True,
            "reason": "cooldown or day risk budget exhausted",
            "lock": _lock_payload(client),
            "budget": remaining_risk_budget(client),
        }

    if dry_run is None:
        dry_run = not auto_execute_enabled()

    # Localhost desk never sends live broker orders from this process
    try:
        from config.runtime_mode import is_local_paper_desk, paper_trading_enabled

        if is_local_paper_desk() or paper_trading_enabled():
            dry_run = True
    except Exception:
        pass

    from services.order_guard import OrderGuardError, place_protected_limit_order

    try:
        result = place_protected_limit_order(
            str(plan["trading_symbol"]),
            "BUY",
            int(plan["qty"]),
            float(plan["ltp"]),
            security_id=plan["security_id"],
            product="INTRADAY",
            tag="INTRA_HUNT",
            redis_client=client,
            dry_run=bool(dry_run),
        )
    except OrderGuardError as exc:
        payload = {"plan": plan, "success": False, "error": str(exc), "dry_run": dry_run}
        _set_lock(client, payload)
        return {"ok": False, "error": str(exc), "plan": plan, "dry_run": dry_run}
    except Exception as exc:
        logger.exception("intraday hunt order failed")
        payload = {"plan": plan, "success": False, "error": str(exc), "dry_run": dry_run}
        _set_lock(client, payload)
        return {"ok": False, "error": str(exc), "plan": plan, "dry_run": dry_run}

    payload = {
        "plan": plan,
        "order_id": result.order_id,
        "limit_price": result.limit_price,
        "dry_run": dry_run,
        "success": result.success,
    }
    _set_lock(client, payload)

    if result.success:
        state = load_day_risk(client)
        planned = float((plan.get("sizing") or {}).get("planned_risk") or 0.0)
        state["deployed_risk"] = float(state.get("deployed_risk") or 0.0) + planned
        sleeves = list(state.get("sleeves") or [])
        sleeves.append(
            {
                "order_id": result.order_id,
                "security_id": plan.get("security_id"),
                "strike": plan.get("strike"),
                "option_type": plan.get("option_type"),
                "qty": plan.get("qty"),
                "planned_risk": planned,
                "ltp": plan.get("ltp"),
                "stop_price": plan.get("stop_price"),
                "asof": datetime.now(IST).isoformat(),
            }
        )
        state["sleeves"] = sleeves
        save_day_risk(client, state)
        payload["day_risk"] = state

    return {"ok": bool(result.success), "dry_run": dry_run, **payload}


def top_strategy_hint(client: RedisClient | None) -> tuple[str, str, float | None]:
    """Return (strategy_id, title, confidence) from today's backtest ranking."""
    if client is None:
        return "", "", None
    try:
        raw = client.client.get("agent:strategies:today")
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not raw:
            return "", "", None
        data = json.loads(raw)
        strategies = data.get("strategies") if isinstance(data, dict) else None
        if not strategies:
            return "", "", None
        top = strategies[0]
        return (
            str(top.get("strategy_id") or top.get("id") or ""),
            str(top.get("name") or top.get("title") or ""),
            float(top["confidence"]) if top.get("confidence") is not None else None,
        )
    except Exception:
        return "", "", None
