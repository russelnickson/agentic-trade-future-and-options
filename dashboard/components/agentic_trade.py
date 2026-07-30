"""Agentic Trade — unified desk conversation + operator replies.

Pulls Scout / Voices / Research / Thesis / Risk / Trade / Tactical outputs into
one feed. Operator messages are answered with factual agent turns (no invented
market claims) so the desk can talk in one place.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from dashboard.components.agent_journal import (
    append_conversation,
    append_decision,
    build_desk_timeline,
)
from database.redis_client import RedisClient

logger = logging.getLogger(__name__)

TACTICAL_STATE_KEY = "agent:tactical:state"
DIRECTIVE_KEY = "agent:strategy:directive"

AGENT_AVATARS: dict[str, str] = {
    "user": "🧑‍💼",
    "orchestrator": "◈",
    "scout": "🔭",
    "voices": "📡",
    "researcher": "🔬",
    "thesis": "📜",
    "risk": "🛡",
    "execution": "⚡",
    "tactical": "🎯",
    "strategic": "🧠",
    "system": "◇",
}


def agent_avatar(agent: str) -> str:
    return AGENT_AVATARS.get(str(agent).lower(), "·")


def _json_get(client: RedisClient | None, key: str) -> dict[str, Any] | None:
    if client is None:
        return None
    try:
        raw = client.client.get(key)
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        logger.debug("agentic_trade read %s failed", key, exc_info=True)
        return None


def load_live_desk_context(
    client: RedisClient | None,
    *,
    symbol: str = "NIFTY",
) -> dict[str, Any]:
    """Snapshot used for agent replies and the live strip."""
    tactical = _json_get(client, TACTICAL_STATE_KEY) or {}
    directive = _json_get(client, DIRECTIVE_KEY) or {}
    manage = list(tactical.get("manage") or [])
    exits = list(tactical.get("exits") or [])
    entry = tactical.get("entry") or {}
    bits = []
    for m in manage[:3]:
        strike = m.get("strike")
        opt = m.get("option_type") or "?"
        pct = m.get("unrealized_pct")
        tgt = m.get("target")
        stop = m.get("stop")
        trail = "trail armed" if m.get("trail_armed") else "trail idle"
        bits.append(
            f"{strike}{opt} "
            f"{'' if pct is None else f'{float(pct):+.1f}%'} "
            f"ltp {m.get('ltp')} tp {tgt} stop {stop} ({trail})"
        )
    return {
        "symbol": symbol.upper(),
        "tactical": tactical,
        "directive": directive,
        "manage": manage,
        "exits": exits,
        "entry": entry,
        "manage_bits": bits,
        "stance": directive.get("stance") or "—",
        "regime": directive.get("regime") or "—",
        "sentiment": directive.get("sentiment") or "—",
        "strategy_hint": directive.get("strategy_hint") or "",
        "allow_new": bool((directive.get("risk") or {}).get("allow_new_entries", False)),
    }


def load_agentic_feed(
    client: RedisClient | None,
    *,
    limit: int = 120,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    return build_desk_timeline(client, limit=limit)


def _compose_agent_replies(
    message: str,
    *,
    ctx: dict[str, Any],
) -> list[tuple[str, str, str]]:
    """Deterministic multi-agent replies from live desk state + user text."""
    text = (message or "").strip()
    low = text.lower()
    symbol = ctx.get("symbol") or "NIFTY"
    manage = ctx.get("manage") or []
    bits = ctx.get("manage_bits") or []
    stance = ctx.get("stance")
    regime = ctx.get("regime")
    sentiment = ctx.get("sentiment")
    hint = ctx.get("strategy_hint") or ""
    entry = ctx.get("entry") or {}
    allow_new = bool(ctx.get("allow_new"))

    wants_exit = any(
        w in low
        for w in ("exit", "flatten", "square", "book", "take profit", "tp now", "sell")
    )
    wants_why = any(w in low for w in ("why", "status", "what", "manage", "proposed", "stuck"))
    wants_enter = any(w in low for w in ("enter", "hunt", "buy", "entry", "open"))

    replies: list[tuple[str, str, str]] = [
        (
            "orchestrator",
            "system",
            f"Heard — routing to the desk on {symbol}. "
            f"Strategic={stance} ({regime}/{sentiment}); "
            f"open sleeves={len(manage)}; new entries "
            f"{'allowed' if allow_new else 'blocked'}.",
        )
    ]

    if manage:
        replies.append(
            (
                "tactical",
                "execution",
                "Managing live: "
                + (" · ".join(bits) if bits else f"{len(manage)} sleeve(s)")
                + ". Exits fire only on hard TP (~+28%), trail giveback, or stop — "
                "not on console MANAGE rows.",
            )
        )
        replies.append(
            (
                "execution",
                "execution",
                "Console MANAGE/PROPOSED is stance telemetry. "
                "I do not place SELL from that journal line — "
                f"tactical_executor owns fills. Entry skip reason: "
                f"{entry.get('reason') or 'n/a'}.",
            )
        )
    else:
        replies.append(
            (
                "tactical",
                "execution",
                f"Flat book — no open sleeves. Entry: {entry.get('reason') or 'idle'}.",
            )
        )

    replies.append(
        (
            "strategic",
            "system",
            f"Directive {stance} · {regime}/{sentiment}"
            + (f" — {hint}" if hint else "")
            + ("" if allow_new else " · allow_new_entries=false"),
        )
    )
    replies.append(
        (
            "risk",
            "risk",
            "Day-loss / util caps still bind new risk. "
            "Open premium is trailed; we do not invent early exits from chat.",
        )
    )

    if wants_why and manage:
        m0 = manage[0]
        pct = m0.get("unrealized_pct")
        tgt = m0.get("target")
        ltp = m0.get("ltp")
        replies.append(
            (
                "orchestrator",
                "system",
                f"Why no exit yet: LTP {ltp} vs TP {tgt} "
                f"({'' if pct is None else f'{float(pct):+.1f}%'} unrealized). "
                "Trail is armed above ~+15%; hard book waits for TP or stop hit.",
            )
        )
    if wants_exit:
        replies.append(
            (
                "risk",
                "risk",
                "Operator asked to exit — use Console **Square off** / kill for "
                "immediate flatten. Chat cannot bypass order_guard.",
            )
        )
    if wants_enter and not allow_new:
        replies.append(
            (
                "strategic",
                "system",
                "ENTRY blocked by directive/util while sleeves or deployed risk "
                "consume the day budget. Flatten or wait for tactical to free risk.",
            )
        )

    return replies


def publish_operator_turn(
    message: str,
    *,
    redis_client: RedisClient | None,
    symbol: str = "NIFTY",
    session_id: str = "",
) -> list[dict[str, Any]]:
    """Write operator message + factual agent replies into the conversation stream."""
    text = (message or "").strip()
    if not text:
        return []

    ctx = load_live_desk_context(redis_client, symbol=symbol)
    session = session_id or f"agentic-{symbol.lower()}"
    published: list[dict[str, Any]] = []

    user_turn = append_conversation(
        {
            "agent": "user",
            "role": "user",
            "message": text,
            "session_id": session,
            "tags": ["agentic_trade", "operator"],
        },
        redis_client=redis_client,
    )
    published.append(user_turn.to_dict())

    for agent, role, msg in _compose_agent_replies(text, ctx=ctx):
        turn = append_conversation(
            {
                "agent": agent,
                "role": role,
                "message": msg,
                "session_id": session,
                "tags": ["agentic_trade", "reply"],
            },
            redis_client=redis_client,
        )
        published.append(turn.to_dict())

    # Surface a decision row when managing so the table stays coherent
    manage = ctx.get("manage") or []
    if manage:
        bits = ctx.get("manage_bits") or []
        append_decision(
            {
                "agent": "execution",
                "kind": "MANAGE",
                "symbol": symbol,
                "summary": (
                    f"MANAGE — operator chat · {len(manage)} open · "
                    + " · ".join(bits)[:160]
                )[:240],
                "rationale": "Operator joined Agentic Trade conversation; tactical still owns exits.",
                "confidence": 0.8,
                "status": "ACTIVE",
                "meta": {"source": "agentic_trade", "manage": manage[:3]},
            },
            redis_client=redis_client,
        )

    return published


def announce_tactical_exit(
    action: dict[str, Any],
    *,
    redis_client: RedisClient | None,
    symbol: str = "NIFTY",
) -> None:
    """Publish EXIT fills so Agentic Trade / Agents see real execution."""
    if not action or action.get("skipped"):
        return
    reason = str(action.get("action") or "EXIT")
    ok = bool(action.get("ok"))
    pct = action.get("pnl_pct")
    status = "EXECUTED" if ok else "FAILED"
    summary = (
        f"{reason} · {'ok' if ok else 'fail'} · "
        f"sec {action.get('security_id')} · ltp {action.get('ltp')}"
        f"{'' if pct is None else f' · {float(pct):+.1f}%'}"
    )[:240]
    try:
        append_conversation(
            {
                "agent": "tactical",
                "role": "execution",
                "message": f"Tactical {summary}"
                + (f" · order {action.get('order_id')}" if action.get("order_id") else "")
                + (f" · err {action.get('error')}" if action.get("error") else ""),
                "session_id": f"tactical-{symbol.lower()}",
                "tags": ["tactical", "exit", reason.lower()],
            },
            redis_client=redis_client,
        )
        append_decision(
            {
                "agent": "tactical",
                "kind": "EXIT",
                "symbol": symbol,
                "summary": summary,
                "rationale": f"Deterministic {reason} via tactical_executor / profit_guard",
                "confidence": 0.95 if ok else 0.4,
                "status": status,
                "action": "SELL",
                "meta": dict(action),
            },
            redis_client=redis_client,
        )
    except Exception:
        logger.debug("announce_tactical_exit failed", exc_info=True)
