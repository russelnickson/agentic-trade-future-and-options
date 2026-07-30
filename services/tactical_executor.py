"""Deterministic tactical executor — orders & stop-loss only (no LangGraph).

Reads the latest StrategyDirective from Redis and acts instantly:
- Stop-loss: exit when LTP breaches planned stop from day-risk sleeves / positions
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


def tactical_poll_sec() -> float:
    raw = (os.getenv("TACTICAL_POLL_SEC") or "1.0").strip()
    try:
        return max(0.25, float(raw))
    except ValueError:
        return 1.0


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


def evaluate_stops(
    *,
    redis_client: RedisClient | None = None,
    dry_run: bool | None = None,
) -> list[dict[str, Any]]:
    """Fire SELL LIMIT when LTP <= stop for long sleeves recorded in day_risk."""
    from services.intraday_hunt import load_day_risk
    from services.order_guard import OrderGuardError, place_protected_limit_order
    from services.intraday_hunt import auto_execute_enabled

    client = redis_client or get_redis_client()
    if dry_run is None:
        dry_run = not auto_execute_enabled()
    try:
        from config.runtime_mode import is_local_paper_desk, paper_trading_enabled

        if is_local_paper_desk() or paper_trading_enabled():
            dry_run = True
    except Exception:
        pass

    state = load_day_risk(client)
    actions: list[dict[str, Any]] = []
    for sleeve in list(state.get("sleeves") or []):
        if not isinstance(sleeve, dict):
            continue
        if sleeve.get("stopped"):
            continue
        stop = sleeve.get("stop_price")
        sec = sleeve.get("security_id")
        opt = sleeve.get("option_type") or "CE"
        strike = sleeve.get("strike")
        qty = int(sleeve.get("qty") or 0)
        if stop is None or sec is None or qty <= 0:
            continue
        ltp = _tick_ltp(client, sec)
        if ltp is None and strike is not None:
            ltp, _ = _chain_side_ltp(client, "NIFTY", float(strike), str(opt))
        if ltp is None:
            continue
        # Long option stop: exit when premium falls to/below stop
        if float(ltp) > float(stop):
            continue

        cd_key = f"{STOP_COOLDOWN_KEY}:{sec}"
        try:
            if client.client.get(cd_key):
                continue
        except Exception:
            pass

        trading_symbol = str(
            sleeve.get("trading_symbol")
            or f"NIFTY{strike}{opt}"
        )
        try:
            result = place_protected_limit_order(
                trading_symbol,
                "SELL",
                qty,
                float(ltp),
                security_id=str(sec),
                product="INTRADAY",
                tag="TACT_STOP",
                redis_client=client,
                dry_run=bool(dry_run),
            )
            sleeve["stopped"] = True
            sleeve["stop_fill_ltp"] = float(ltp)
            sleeve["stop_order_id"] = result.order_id
            sleeve["stop_asof"] = datetime.now(IST).isoformat()
            actions.append(
                {
                    "action": "STOP_SELL",
                    "ok": result.success,
                    "security_id": sec,
                    "ltp": ltp,
                    "stop": stop,
                    "order_id": result.order_id,
                    "dry_run": dry_run,
                }
            )
            client.client.set(cd_key, "1", ex=120)
        except OrderGuardError as exc:
            actions.append(
                {"action": "STOP_SELL", "ok": False, "error": str(exc), "security_id": sec}
            )
            client.client.set(cd_key, "1", ex=30)
        except Exception as exc:
            logger.exception("stop evaluation failed")
            actions.append(
                {"action": "STOP_SELL", "ok": False, "error": str(exc), "security_id": sec}
            )

    if actions:
        from services.intraday_hunt import save_day_risk

        save_day_risk(client, state)
    return actions


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
        auto_execute_enabled,
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

    # Force preferred side from directive into strategy_id hint
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

    # Cap sleeve weight from directive
    sizing = plan.get("sizing") or {}
    if float(sizing.get("sleeve_weight") or 0) > float(d.risk.max_sleeve_weight):
        # Rebuild planned risk to directive cap
        cap_risk = float(d.risk.max_daily_loss) * float(d.risk.max_sleeve_weight)
        plan = dict(plan)
        plan["sizing"] = {**sizing, "planned_risk": min(float(sizing.get("planned_risk") or cap_risk), cap_risk)}

    if dry_run is None:
        dry_run = not auto_execute_enabled()

    result = execute_hunt(plan, redis_client=client, dry_run=dry_run)
    result["directive_stance"] = d.stance
    result["directive_side"] = d.preferred_side
    return result


def tactical_tick(
    *,
    symbol: str = "NIFTY",
    redis_client: RedisClient | None = None,
) -> dict[str, Any]:
    """One fast cycle: stops first, then optional entry."""
    client = redis_client or get_redis_client()
    directive = load_directive(client)
    stops = evaluate_stops(redis_client=client)
    entry: dict[str, Any] = {"skipped": True, "reason": "not attempted"}
    if directive and directive.allows_entry():
        entry = maybe_enter(directive=directive, redis_client=client, symbol=symbol)
    elif directive and directive.stance == "FLAT" and directive.risk.kill:
        entry = {"skipped": True, "reason": "strategic FLAT/kill"}

    summary = {
        "asof": datetime.now(IST).isoformat(),
        "stops": stops,
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
        "Tactical executor started (symbol=%s, poll=%.2fs) — orders/stops only",
        symbol,
        poll,
    )
    while True:
        started = time.perf_counter()
        try:
            out = tactical_tick(symbol=symbol)
            n_stops = len(out.get("stops") or [])
            entry = out.get("entry") or {}
            if n_stops or entry.get("ok") or (not entry.get("skipped") and entry.get("error")):
                logger.info(
                    "Tactical · stops=%s entry_ok=%s reason=%s",
                    n_stops,
                    entry.get("ok"),
                    entry.get("reason") or entry.get("error"),
                )
        except Exception:
            logger.exception("Tactical tick failed")
        elapsed = time.perf_counter() - started
        time.sleep(max(0.05, poll - elapsed))
