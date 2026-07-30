"""Deterministic tactical executor — entries, stops, and profit booking.

Reads the latest StrategyDirective from Redis and acts instantly:
- Track open sleeves dynamically (peak LTP, trailing lock, hard take-profit)
- Stop-loss / trail exit / take-profit SELL LIMIT
- Entry: sized hunt only when directive.allows_entry()

Never re-analyzes regimes or sentiment — that is the strategic controller's job.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from database.redis_client import RedisClient, get_redis_client
from services.strategic_controller.directive import StrategyDirective, load_directive

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

TACTICAL_STATE_KEY = "agent:tactical:state"
STOP_COOLDOWN_KEY = "agent:tactical:stop_cooldown"
EXIT_COOLDOWN_KEY = "agent:tactical:exit_cooldown"


def tactical_poll_sec() -> float:
    raw = (os.getenv("TACTICAL_POLL_SEC") or "1.0").strip()
    try:
        return max(0.25, float(raw))
    except ValueError:
        return 1.0


def _dry_run_flag(explicit: bool | None) -> bool:
    from services.intraday_hunt import auto_execute_enabled

    if explicit is not None:
        dry = bool(explicit)
    else:
        dry = not auto_execute_enabled()
    try:
        from config.runtime_mode import is_local_paper_desk, paper_trading_enabled

        if is_local_paper_desk() or paper_trading_enabled():
            dry = True
    except Exception:
        pass
    return dry


def _tick_ltp(client: RedisClient, token: str | int) -> float | None:
    key = f"tick:{token}"
    try:
        raw = client.client.get(key)
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not raw:
            return None
        if raw.startswith("{"):
            data = json.loads(raw)
            for k in ("ltp", "last_price", "last_traded_price", "LTP"):
                if data.get(k) is not None:
                    return float(data[k])
        return float(raw)
    except Exception:
        return None


def _chain_side_ltp(
    client: RedisClient, symbol: str, strike: float, opt: str
) -> tuple[float | None, str | None]:
    chain = client.get_option_chain_state(symbol) or {}
    strikes = chain.get("strikes") or {}
    key = str(int(strike)) if float(strike) == int(strike) else str(strike)
    side = (strikes.get(key) or strikes.get(str(strike)) or {}).get(opt) or {}
    if not isinstance(side, dict):
        return None, None
    token = side.get("token")
    ltp = side.get("ltp")
    if ltp is None and token is not None:
        ltp = _tick_ltp(client, token)
    return (float(ltp) if ltp is not None else None), (str(int(token)) if token is not None else None)


def _sleeve_ltp(client: RedisClient, sleeve: dict[str, Any], symbol: str = "NIFTY") -> float | None:
    sec = sleeve.get("security_id")
    opt = sleeve.get("option_type") or "CE"
    strike = sleeve.get("strike")
    ltp = _tick_ltp(client, sec) if sec is not None else None
    if ltp is None and strike is not None:
        ltp, _ = _chain_side_ltp(client, symbol, float(strike), str(opt))
    return ltp


def sync_open_positions(
    *,
    redis_client: RedisClient | None = None,
    symbol: str = "NIFTY",
) -> dict[str, Any]:
    """Ensure broker open F&O longs appear in day_risk sleeves for live manage."""
    from services.intraday_hunt import (
        load_day_risk,
        lot_size,
        recompute_deployed_risk,
        save_day_risk,
        take_profit_frac,
    )
    from services.profit_guard import target_price

    client = redis_client or get_redis_client()
    state = load_day_risk(client)
    sleeves = list(state.get("sleeves") or [])
    known = {
        str(s.get("security_id"))
        for s in sleeves
        if isinstance(s, dict) and s.get("security_id") is not None
        and not (s.get("stopped") or s.get("exited"))
    }
    added = 0
    try:
        from dashboard.components.positions import fetch_positions
        from config.settings import get_settings

        broker = (get_settings().trade_broker or "dhan").lower()
        rows, _err = fetch_positions(broker, redis_client=client)  # type: ignore[arg-type]
    except Exception:
        rows = []
    for r in rows or []:
        if int(r.qty or 0) <= 0:
            continue
        tok = str(r.token) if r.token is not None else None
        if not tok or tok in known:
            continue
        entry = float(r.entry_price or r.ltp or 0)
        if entry <= 0:
            continue
        stop_frac = float(os.getenv("TRADE_STOP_FRACTION") or 0.35)
        sleeves.append(
            {
                "order_id": f"SYNC-{tok}",
                "security_id": tok,
                "trading_symbol": r.symbol,
                "strike": r.strike,
                "option_type": r.option_type or "CE",
                "qty": int(r.qty),
                "planned_risk": round(entry * abs(int(r.qty)) * stop_frac, 2),
                "ltp": entry,
                "entry_ltp": entry,
                "peak_ltp": float(r.ltp or entry),
                "stop_price": round(entry * (1.0 - stop_frac), 2),
                "target_price": target_price(entry, tp_frac=take_profit_frac()),
                "synced": True,
                "asof": datetime.now(IST).isoformat(),
            }
        )
        known.add(tok)
        added += 1

    if added:
        state["sleeves"] = sleeves
        state["deployed_risk"] = recompute_deployed_risk(state)
        save_day_risk(client, state)
    return {"added": added, "open": len(known)}


def _place_exit(
    *,
    client: RedisClient,
    sleeve: dict[str, Any],
    ltp: float,
    reason: str,
    dry_run: bool,
) -> dict[str, Any]:
    from services.order_guard import OrderGuardError, place_protected_limit_order

    sec = sleeve.get("security_id")
    qty = int(sleeve.get("qty") or 0)
    opt = sleeve.get("option_type") or "CE"
    strike = sleeve.get("strike")
    trading_symbol = str(
        sleeve.get("trading_symbol") or f"NIFTY{strike}{opt}"
    )
    tag = {
        "TAKE_PROFIT": "TACT_TP",
        "TRAIL_EXIT": "TACT_TRAIL",
        "STOP": "TACT_STOP",
    }.get(reason, "TACT_EXIT")
    cd_key = f"{EXIT_COOLDOWN_KEY}:{sec}"
    try:
        if client.client.get(cd_key):
            return {"action": reason, "ok": False, "skipped": True, "reason": "cooldown", "security_id": sec}
    except Exception:
        pass

    try:
        result = place_protected_limit_order(
            trading_symbol,
            "SELL",
            qty,
            float(ltp),
            security_id=str(sec),
            product="INTRADAY",
            tag=tag,
            redis_client=client,
            dry_run=bool(dry_run),
        )
        sleeve["stopped"] = True
        sleeve["exited"] = True
        sleeve["exit_reason"] = reason
        sleeve["exit_ltp"] = float(ltp)
        sleeve["exit_order_id"] = result.order_id
        sleeve["exit_asof"] = datetime.now(IST).isoformat()
        entry = float(sleeve.get("entry_ltp") or sleeve.get("ltp") or 0)
        if entry > 0:
            sleeve["exit_pnl_pct"] = round((float(ltp) - entry) / entry * 100.0, 2)
        try:
            client.client.set(cd_key, reason, ex=120)
        except Exception:
            pass
        return {
            "action": reason,
            "ok": result.success,
            "security_id": sec,
            "ltp": ltp,
            "entry": entry or None,
            "target": sleeve.get("target_price"),
            "stop": sleeve.get("stop_price"),
            "order_id": result.order_id,
            "dry_run": dry_run,
            "pnl_pct": sleeve.get("exit_pnl_pct"),
        }
    except OrderGuardError as exc:
        try:
            client.client.set(cd_key, "1", ex=30)
        except Exception:
            pass
        return {"action": reason, "ok": False, "error": str(exc), "security_id": sec}
    except Exception as exc:
        logger.exception("exit %s failed", reason)
        return {"action": reason, "ok": False, "error": str(exc), "security_id": sec}


def evaluate_exits(
    *,
    redis_client: RedisClient | None = None,
    dry_run: bool | None = None,
    symbol: str = "NIFTY",
) -> list[dict[str, Any]]:
    """Dynamic manage: raise trailing locks, then TP / trail / stop exits."""
    from services.intraday_hunt import (
        load_day_risk,
        recompute_deployed_risk,
        save_day_risk,
    )
    from services.profit_guard import evaluate_long_premium

    client = redis_client or get_redis_client()
    dry = _dry_run_flag(dry_run)
    state = load_day_risk(client)
    actions: list[dict[str, Any]] = []
    dirty = False

    for sleeve in list(state.get("sleeves") or []):
        if not isinstance(sleeve, dict):
            continue
        if sleeve.get("stopped") or sleeve.get("exited"):
            continue
        sec = sleeve.get("security_id")
        qty = int(sleeve.get("qty") or 0)
        entry = sleeve.get("entry_ltp") or sleeve.get("ltp")
        if sec is None or qty <= 0 or entry is None:
            continue

        ltp = _sleeve_ltp(client, sleeve, symbol=symbol)
        if ltp is None:
            continue

        decision = evaluate_long_premium(
            entry=float(entry),
            ltp=float(ltp),
            peak_ltp=sleeve.get("peak_ltp"),
            stop_price=sleeve.get("stop_price"),
        )
        # Persist live manage marks
        if decision.get("peak_ltp") != sleeve.get("peak_ltp"):
            sleeve["peak_ltp"] = decision.get("peak_ltp")
            dirty = True
        if decision.get("stop_price") is not None and decision.get("stop_price") != sleeve.get(
            "stop_price"
        ):
            sleeve["stop_price"] = decision.get("stop_price")
            sleeve["trail_armed"] = bool(decision.get("trail_armed"))
            dirty = True
        if sleeve.get("target_price") is None and decision.get("target_price") is not None:
            sleeve["target_price"] = decision.get("target_price")
            dirty = True
        sleeve["last_ltp"] = float(ltp)
        sleeve["unrealized_pct"] = decision.get("unrealized_pct")
        sleeve["managed_asof"] = datetime.now(IST).isoformat()
        dirty = True

        reason = decision.get("exit_reason")
        if not reason:
            continue

        action = _place_exit(
            client=client,
            sleeve=sleeve,
            ltp=float(ltp),
            reason=str(reason),
            dry_run=dry,
        )
        actions.append(action)
        dirty = True

    if dirty:
        state["deployed_risk"] = recompute_deployed_risk(state)
        save_day_risk(client, state)
        # If flat, clear short entry lock so next hunt can fire
        if recompute_deployed_risk(state) <= 0:
            try:
                from services.intraday_hunt import clear_entry_lock

                clear_entry_lock(client)
            except Exception:
                pass
    return actions


def evaluate_stops(
    *,
    redis_client: RedisClient | None = None,
    dry_run: bool | None = None,
) -> list[dict[str, Any]]:
    """Back-compat alias — full exit path including take-profit / trail."""
    return evaluate_exits(redis_client=redis_client, dry_run=dry_run)


def maybe_enter(
    *,
    directive: StrategyDirective | None = None,
    redis_client: RedisClient | None = None,
    symbol: str = "NIFTY",
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """Sized ENTRY only if strategic directive allows — pure Python path."""
    from services.intraday_hunt import (
        already_entered_today,
        build_hunt_plan,
        execute_hunt,
        remaining_risk_budget,
    )

    client = redis_client or get_redis_client()
    d = directive if directive is not None else load_directive(client)
    if d is None or not d.allows_entry():
        return {
            "ok": False,
            "skipped": True,
            "reason": "directive blocks entry or stale",
            "directive": d.to_dict() if d else None,
        }

    if already_entered_today(client):
        return {"ok": False, "skipped": True, "reason": "cooldown / day risk"}

    budget = remaining_risk_budget(client)
    if not budget["can_hunt"]:
        return {"ok": False, "skipped": True, "reason": "risk budget exhausted", "budget": budget}

    available_margin = None
    try:
        from dashboard.components.capital import fetch_capital
        from config.settings import get_settings

        cap = fetch_capital((get_settings().trade_broker or "dhan").lower())  # type: ignore[arg-type]
        available_margin = float(cap.available_margin or 0) or None
    except Exception:
        pass

    side = d.preferred_side
    strategy_id = "breakout_long" if side == "CE" else "bear_call_credit"
    if side == "PE" and d.sentiment == "BEARISH":
        strategy_id = "bear_call_credit"

    plan = build_hunt_plan(
        client=client,
        symbol=symbol,
        bias=d.sentiment,
        pcr=(d.meta or {}).get("pcr"),
        strategy_id=strategy_id,
        strategy_title=d.strategy_hint,
        confidence=min(d.confidence, d.risk.max_sleeve_weight / 0.28 * 0.95),
        available_margin=available_margin,
    )
    if not plan or plan.get("skip"):
        return {"ok": False, "skipped": True, "reason": (plan or {}).get("reason"), "plan": plan}

    sizing = plan.get("sizing") or {}
    if float(sizing.get("sleeve_weight") or 0) > float(d.risk.max_sleeve_weight):
        cap_risk = float(d.risk.max_daily_loss) * float(d.risk.max_sleeve_weight)
        plan = dict(plan)
        plan["sizing"] = {
            **sizing,
            "planned_risk": min(float(sizing.get("planned_risk") or cap_risk), cap_risk),
        }

    dry = _dry_run_flag(dry_run)
    result = execute_hunt(plan, redis_client=client, dry_run=dry)
    result["directive_stance"] = d.stance
    result["directive_side"] = d.preferred_side
    return result


def _manage_snapshot(client: RedisClient, symbol: str) -> list[dict[str, Any]]:
    from services.intraday_hunt import load_day_risk, open_sleeves

    out: list[dict[str, Any]] = []
    for s in open_sleeves(load_day_risk(client)):
        ltp = _sleeve_ltp(client, s, symbol=symbol)
        entry = float(s.get("entry_ltp") or s.get("ltp") or 0)
        pct = None
        if ltp is not None and entry > 0:
            pct = round((float(ltp) - entry) / entry * 100.0, 2)
        out.append(
            {
                "security_id": s.get("security_id"),
                "option_type": s.get("option_type"),
                "strike": s.get("strike"),
                "qty": s.get("qty"),
                "entry": entry or None,
                "ltp": ltp,
                "stop": s.get("stop_price"),
                "target": s.get("target_price"),
                "peak": s.get("peak_ltp"),
                "trail_armed": bool(s.get("trail_armed")),
                "unrealized_pct": pct if pct is not None else s.get("unrealized_pct"),
            }
        )
    return out


def tactical_tick(
    *,
    symbol: str = "NIFTY",
    redis_client: RedisClient | None = None,
) -> dict[str, Any]:
    """One fast cycle: sync opens → manage exits → optional entry."""
    client = redis_client or get_redis_client()
    sync = sync_open_positions(redis_client=client, symbol=symbol)
    exits = evaluate_exits(redis_client=client, symbol=symbol)
    manage = _manage_snapshot(client, symbol)
    directive = load_directive(client)
    entry: dict[str, Any] = {"skipped": True, "reason": "not attempted"}
    if directive and directive.allows_entry():
        entry = maybe_enter(directive=directive, redis_client=client, symbol=symbol)
    elif directive and directive.stance == "FLAT" and directive.risk.kill:
        entry = {"skipped": True, "reason": "strategic FLAT/kill"}
    elif manage:
        entry = {"skipped": True, "reason": "managing open sleeves toward profit"}

    summary = {
        "asof": datetime.now(IST).isoformat(),
        "sync": sync,
        "exits": exits,
        "stops": exits,  # back-compat for older log readers
        "manage": manage,
        "entry": entry,
        "directive": directive.to_dict() if directive else None,
    }
    try:
        client.client.set(TACTICAL_STATE_KEY, json.dumps(summary, default=str), ex=300)
    except Exception:
        pass
    return summary


def run_forever(symbol: str = "NIFTY", *, poll_sec: float | None = None) -> None:
    poll = poll_sec if poll_sec is not None else tactical_poll_sec()
    logger.info(
        "Tactical executor started (symbol=%s, poll=%.2fs) — entries/stops/profit-booking",
        symbol,
        poll,
    )
    while True:
        started = time.perf_counter()
        try:
            out = tactical_tick(symbol=symbol)
            exits = out.get("exits") or []
            manage = out.get("manage") or []
            entry = out.get("entry") or {}
            if exits or manage or entry.get("ok") or (
                not entry.get("skipped") and entry.get("error")
            ):
                logger.info(
                    "Tactical · exits=%s open=%s entry_ok=%s reason=%s",
                    [
                        {
                            "action": e.get("action"),
                            "ok": e.get("ok"),
                            "pnl_pct": e.get("pnl_pct"),
                        }
                        for e in exits
                    ],
                    len(manage),
                    entry.get("ok"),
                    entry.get("reason") or entry.get("error"),
                )
        except Exception:
            logger.exception("Tactical tick failed")
        elapsed = time.perf_counter() - started
        time.sleep(max(0.05, poll - elapsed))
