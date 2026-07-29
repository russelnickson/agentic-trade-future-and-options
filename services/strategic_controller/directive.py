"""Strategic directive published by LangGraph; consumed by deterministic tactical code."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from database.redis_client import RedisClient, get_redis_client

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

DIRECTIVE_KEY = "agent:strategy:directive"
DIRECTIVE_HISTORY_KEY = "agent:strategy:directive:history"

Regime = Literal["TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL", "UNKNOWN"]
Sentiment = Literal["BULLISH", "BEARISH", "NEUTRAL"]
Stance = Literal["HUNT", "HOLD", "REDUCE", "FLAT"]
Side = Literal["CE", "PE", "NONE"]


@dataclass
class RiskLimits:
    max_daily_loss: float
    allow_new_entries: bool
    max_sleeve_weight: float
    kill: bool = False
    reason: str = ""


@dataclass
class StrategyDirective:
    """Slow-path strategic output — never places orders itself."""

    asof: str
    symbol: str
    regime: Regime
    sentiment: Sentiment
    sentiment_score: float
    stance: Stance
    preferred_side: Side
    strategy_hint: str
    confidence: float
    risk: RiskLimits
    ttl_sec: int = 180
    source: str = "langgraph_strategic"
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyDirective:
        risk_raw = data.get("risk") or {}
        risk = RiskLimits(
            max_daily_loss=float(risk_raw.get("max_daily_loss") or 5000),
            allow_new_entries=bool(risk_raw.get("allow_new_entries", False)),
            max_sleeve_weight=float(risk_raw.get("max_sleeve_weight") or 0.28),
            kill=bool(risk_raw.get("kill", False)),
            reason=str(risk_raw.get("reason") or ""),
        )
        return cls(
            asof=str(data.get("asof") or ""),
            symbol=str(data.get("symbol") or "NIFTY"),
            regime=data.get("regime") or "UNKNOWN",  # type: ignore[arg-type]
            sentiment=data.get("sentiment") or "NEUTRAL",  # type: ignore[arg-type]
            sentiment_score=float(data.get("sentiment_score") or 0.0),
            stance=data.get("stance") or "HOLD",  # type: ignore[arg-type]
            preferred_side=data.get("preferred_side") or "NONE",  # type: ignore[arg-type]
            strategy_hint=str(data.get("strategy_hint") or ""),
            confidence=float(data.get("confidence") or 0.0),
            risk=risk,
            ttl_sec=int(data.get("ttl_sec") or 180),
            source=str(data.get("source") or "langgraph_strategic"),
            meta=dict(data.get("meta") or {}),
        )

    def is_fresh(self, *, now: datetime | None = None) -> bool:
        if not self.asof:
            return False
        try:
            asof = datetime.fromisoformat(self.asof)
            if asof.tzinfo is None:
                asof = asof.replace(tzinfo=IST)
        except ValueError:
            return False
        now = now or datetime.now(IST)
        return (now - asof).total_seconds() <= float(self.ttl_sec) + 15.0

    def allows_entry(self) -> bool:
        return (
            self.is_fresh()
            and not self.risk.kill
            and self.risk.allow_new_entries
            and self.stance == "HUNT"
            and self.preferred_side in {"CE", "PE"}
        )


def publish_directive(
    directive: StrategyDirective,
    *,
    redis_client: RedisClient | None = None,
) -> None:
    client = redis_client or get_redis_client()
    payload = json.dumps(directive.to_dict(), default=str)
    client.client.set(DIRECTIVE_KEY, payload, ex=max(60, int(directive.ttl_sec) * 3))
    try:
        client.client.lpush(DIRECTIVE_HISTORY_KEY, payload)
        client.client.ltrim(DIRECTIVE_HISTORY_KEY, 0, 99)
    except Exception:
        logger.debug("directive history push failed", exc_info=True)


def load_directive(redis_client: RedisClient | None = None) -> StrategyDirective | None:
    client = redis_client or get_redis_client()
    try:
        raw = client.client.get(DIRECTIVE_KEY)
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not raw:
            return None
        return StrategyDirective.from_dict(json.loads(raw))
    except Exception:
        logger.debug("load_directive failed", exc_info=True)
        return None
