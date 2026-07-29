"""Broker speculation desk notes — clearly labeled non-primary inputs.

These are NOT exchange/regulator facts. Agents must treat them as external
broker desk speculation with lower weight than Global Outlook / Live Market /
exchange filings.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dashboard.components.agent_journal import append_conversation, append_decision
from dashboard.timefmt import format_ist
from database.redis_client import RedisClient

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = PROJECT_ROOT / "data" / "desk" / "broker_speculation.json"
REDIS_KEY = "agent:broker_speculation"

CREDIBILITY = 0.55  # below direct-source tiers; labeled speculation only


@dataclass
class ZoneRange:
    label: str
    low: float
    high: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fmt(self) -> str:
        return f"{self.low:.0f}–{self.high:.0f}"


@dataclass
class BrokerSpeculation:
    asof: str
    source_label: str
    credibility: float
    signals: dict[str, str]
    nifty: dict[str, Any]
    banknifty: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_from_sheet_2026_07_29() -> BrokerSpeculation:
    """Snapshot transcribed from the broker desk sheet (29 Jul 2026)."""
    return BrokerSpeculation(
        asof=datetime.now(IST).isoformat(),
        source_label="Broker desk speculation sheet (operator paste)",
        credibility=CREDIBILITY,
        signals={
            "Global": "POSITIVE",
            "FII": "NEUTRAL",
            "DII": "POSITIVE",
            "F&O": "NEUTRAL",
            "Sentiment": "POSITIVE",
            "Trend": "POSITIVE",
        },
        nifty={
            "Support Zone": {"low": 23800, "high": 23900},
            "Strong Shopping Zone": {"low": 23650, "high": 23765},
            "Upper Zone": {"low": 24050, "high": 24175},
            "Profit booking Zone": {"low": 24200, "high": 24335},
        },
        banknifty={
            "Support Zone": {"low": 56400, "high": 56675},
            "Strong Shopping Zone": {"low": 56025, "high": 56300},
            "Upper Zone": {"low": 57050, "high": 57325},
            "Profit booking Zone": {"low": 57400, "high": 57550},
        },
        notes=[
            "Labeled SPECULATION — not a primary exchange/regulator source.",
            "Use only as situational context alongside Scout / Voices / Insights.",
        ],
    )


def save_speculation(
    spec: BrokerSpeculation,
    *,
    redis_client: RedisClient | None = None,
) -> BrokerSpeculation:
    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = spec.to_dict()
    SPEC_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if redis_client is not None:
        try:
            redis_client.client.set(REDIS_KEY, json.dumps(payload, default=str))
        except Exception:
            logger.debug("Failed writing %s", REDIS_KEY, exc_info=True)
    return spec


def load_speculation(redis_client: RedisClient | None = None) -> BrokerSpeculation | None:
    if redis_client is not None:
        try:
            raw = redis_client.client.get(REDIS_KEY)
            if raw:
                data = json.loads(raw)
                return BrokerSpeculation(**data)
        except Exception:
            logger.debug("Redis speculation read failed", exc_info=True)
    if SPEC_PATH.is_file():
        try:
            data = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
            return BrokerSpeculation(**data)
        except Exception:
            logger.debug("File speculation read failed", exc_info=True)
    return None


def _zone_line(zones: dict[str, Any]) -> str:
    parts = []
    for name, z in zones.items():
        if isinstance(z, dict) and "low" in z and "high" in z:
            parts.append(f"{name} {z['low']:.0f}–{z['high']:.0f}")
    return "; ".join(parts)


def speculation_summary(spec: BrokerSpeculation) -> str:
    sig = ", ".join(f"{k}={v}" for k, v in spec.signals.items())
    return (
        f"[SPECULATION cred {spec.credibility:.2f}] {spec.source_label} · "
        f"Signals: {sig}. "
        f"NIFTY: {_zone_line(spec.nifty)}. "
        f"BANKNIFTY: {_zone_line(spec.banknifty)}."
    )


def inject_broker_speculation(
    spec: BrokerSpeculation | None = None,
    *,
    redis_client: RedisClient | None = None,
    symbol: str = "NIFTY",
) -> BrokerSpeculation:
    """Persist sheet + publish agent discussion turns + a labeled Trade OBSERVE."""
    spec = spec or default_from_sheet_2026_07_29()
    save_speculation(spec, redis_client=redis_client)
    session = f"console-{datetime.now(IST).strftime('%Y%m%d')}"
    when = format_ist(spec.asof)

    turns = [
        (
            "orchestrator",
            "system",
            f"Operator ingested broker desk SPECULATION ({when}). "
            f"Credibility {spec.credibility:.2f} — not a primary source. "
            "Scout/Voices/Insights remain authoritative; Trade may only use this as soft context.",
        ),
        (
            "scout",
            "system",
            "Scout (on speculation): sheet Global=POSITIVE, FII=NEUTRAL, DII=POSITIVE — "
            "aligns directionally with our BULLISH open bias but FII NEUTRAL is softer than a strong FII bid.",
        ),
        (
            "voices",
            "system",
            "Voices: no change to direct-source feed. Broker sheet is external commentary — "
            "do not mix into credibility-scored Live Market rows.",
        ),
        (
            "researcher",
            "researcher",
            "Research (speculation levels): "
            f"NIFTY support {spec.nifty['Support Zone']['low']:.0f}–{spec.nifty['Support Zone']['high']:.0f}, "
            f"strong buy {spec.nifty['Strong Shopping Zone']['low']:.0f}–{spec.nifty['Strong Shopping Zone']['high']:.0f}, "
            f"upper {spec.nifty['Upper Zone']['low']:.0f}–{spec.nifty['Upper Zone']['high']:.0f}, "
            f"book {spec.nifty['Profit booking Zone']['low']:.0f}–{spec.nifty['Profit booking Zone']['high']:.0f}. "
            f"BANKNIFTY support {spec.banknifty['Support Zone']['low']:.0f}–{spec.banknifty['Support Zone']['high']:.0f}, "
            f"strong buy {spec.banknifty['Strong Shopping Zone']['low']:.0f}–{spec.banknifty['Strong Shopping Zone']['high']:.0f}, "
            f"upper {spec.banknifty['Upper Zone']['low']:.0f}–{spec.banknifty['Upper Zone']['high']:.0f}, "
            f"book {spec.banknifty['Profit booking Zone']['low']:.0f}–{spec.banknifty['Profit booking Zone']['high']:.0f}.",
        ),
        (
            "risk",
            "risk",
            "Risk: speculation zones are not hard stops. Prefer defined-risk only; "
            "invalidate if spot gaps through Strong Shopping without reclaim.",
        ),
        (
            "execution",
            "execution",
            "Trade: logged broker speculation as soft map. "
            "No auto-entry from sheet alone — wait live chain + Risk clearance. "
            f"Near-term watch NIFTY support {spec.nifty['Support Zone']['low']:.0f}–{spec.nifty['Support Zone']['high']:.0f} "
            f"vs upper {spec.nifty['Upper Zone']['low']:.0f}–{spec.nifty['Upper Zone']['high']:.0f}.",
        ),
    ]
    for agent, role, message in turns:
        append_conversation(
            {
                "agent": agent,
                "role": role,
                "message": message,
                "session_id": session,
                "tags": ["broker_speculation", "soft_input"],
            },
            redis_client=redis_client,
        )

    append_decision(
        {
            "agent": "execution",
            "kind": "OBSERVE",
            "symbol": symbol,
            "summary": "OBSERVE — broker speculation map ingested (soft context only)",
            "rationale": speculation_summary(spec),
            "confidence": spec.credibility,
            "status": "PROPOSED",
            "meta": {
                "source": "broker_speculation",
                "credibility": spec.credibility,
                "signals": spec.signals,
                "nifty": spec.nifty,
                "banknifty": spec.banknifty,
            },
        },
        redis_client=redis_client,
    )
    return spec
