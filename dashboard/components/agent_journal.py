"""Agent journal — conversations, decisions, and next-day insights.

Storage mirrors the order-audit pattern:
  - Redis streams (hot / live)
  - JSONL files under ``logs/`` (durable fallback, gitignored)
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from database.redis_client import RedisClient

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
IST = ZoneInfo("Asia/Kolkata")

CONVERSATIONS_STREAM = "agent:conversations"
DECISIONS_STREAM = "agent:decisions"
INSIGHTS_STREAM = "agent:insights"
STRATEGY_TODAY_KEY = "agent:strategy:today"

CONVERSATIONS_LOG = PROJECT_ROOT / "logs" / "agent_conversations.jsonl"
DECISIONS_LOG = PROJECT_ROOT / "logs" / "agent_decisions.jsonl"
INSIGHTS_LOG = PROJECT_ROOT / "logs" / "agent_insights.jsonl"

Role = Literal["system", "researcher", "risk", "execution", "user", "orchestrator"]
DecisionKind = Literal[
    "ENTRY",
    "EXIT",
    "HEDGE",
    "SKIP",
    "SQUARE_OFF",
    "ADJUST",
    "OBSERVE",
]


@dataclass
class ConversationTurn:
    turn_id: str
    timestamp: str
    agent: str
    role: str
    message: str
    session_id: str = ""
    related_decision_id: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecisionEvent:
    decision_id: str
    timestamp: str
    agent: str
    kind: str
    symbol: str
    summary: str
    rationale: str
    confidence: float | None = None
    strike: float | str | None = None
    action: str = ""
    status: str = "PROPOSED"
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InsightNote:
    insight_id: str
    timestamp: str
    trade_date: str
    symbol: str
    title: str
    outlook: str
    strategy_for_tomorrow: str
    why: str
    supporting_metrics: dict[str, Any] = field(default_factory=dict)
    agent: str = "researcher"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    _ensure_parent(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, default=str) + "\n")


def _xadd(client: RedisClient | None, stream: str, payload: dict[str, Any]) -> None:
    if client is None:
        return
    try:
        # Redis streams want flat string fields.
        flat = {k: json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v) for k, v in payload.items()}
        client.client.xadd(stream, flat, maxlen=5_000, approximate=True)
    except Exception:
        logger.debug("Redis XADD failed for %s", stream, exc_info=True)


def _read_stream(client: RedisClient | None, stream: str, *, count: int = 200) -> list[dict[str, Any]]:
    if client is None:
        return []
    try:
        rows = client.client.xrevrange(stream, count=count)
    except Exception:
        logger.debug("Redis XREVRANGE failed for %s", stream, exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for _id, fields in rows:
        item: dict[str, Any] = {"_stream_id": _id}
        for k, v in fields.items():
            key = k.decode() if isinstance(k, bytes) else str(k)
            val = v.decode() if isinstance(v, bytes) else v
            if isinstance(val, str) and val[:1] in "[{":
                try:
                    val = json.loads(val)
                except json.JSONDecodeError:
                    pass
            item[key] = val
        out.append(item)
    return out


def _read_jsonl(path: Path, *, limit: int = 200) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines[-limit * 2 :]):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(rows) >= limit:
                break
    except OSError:
        logger.debug("Failed reading %s", path, exc_info=True)
    return rows


def append_conversation(
    turn: dict[str, Any] | ConversationTurn,
    *,
    redis_client: RedisClient | None = None,
) -> ConversationTurn:
    if isinstance(turn, ConversationTurn):
        payload = turn.to_dict()
    else:
        payload = {
            "turn_id": turn.get("turn_id") or f"turn-{uuid.uuid4().hex[:10]}",
            "timestamp": turn.get("timestamp") or _utc_now_iso(),
            "agent": str(turn.get("agent") or "orchestrator"),
            "role": str(turn.get("role") or "system"),
            "message": str(turn.get("message") or ""),
            "session_id": str(turn.get("session_id") or ""),
            "related_decision_id": str(turn.get("related_decision_id") or ""),
            "tags": list(turn.get("tags") or []),
        }
    _xadd(redis_client, CONVERSATIONS_STREAM, payload)
    _append_jsonl(CONVERSATIONS_LOG, payload)
    return ConversationTurn(**{k: payload[k] for k in ConversationTurn.__dataclass_fields__})


def append_decision(
    event: dict[str, Any] | DecisionEvent,
    *,
    redis_client: RedisClient | None = None,
) -> DecisionEvent:
    if isinstance(event, DecisionEvent):
        payload = event.to_dict()
    else:
        payload = {
            "decision_id": event.get("decision_id") or f"dec-{uuid.uuid4().hex[:10]}",
            "timestamp": event.get("timestamp") or _utc_now_iso(),
            "agent": str(event.get("agent") or "orchestrator"),
            "kind": str(event.get("kind") or "OBSERVE").upper(),
            "symbol": str(event.get("symbol") or "").upper(),
            "summary": str(event.get("summary") or ""),
            "rationale": str(event.get("rationale") or ""),
            "confidence": event.get("confidence"),
            "strike": event.get("strike"),
            "action": str(event.get("action") or ""),
            "status": str(event.get("status") or "PROPOSED"),
            "meta": dict(event.get("meta") or {}),
        }
    _xadd(redis_client, DECISIONS_STREAM, payload)
    _append_jsonl(DECISIONS_LOG, payload)
    return DecisionEvent(**{k: payload[k] for k in DecisionEvent.__dataclass_fields__})


def append_insight(
    note: dict[str, Any] | InsightNote,
    *,
    redis_client: RedisClient | None = None,
) -> InsightNote:
    if isinstance(note, InsightNote):
        payload = note.to_dict()
    else:
        payload = {
            "insight_id": note.get("insight_id") or f"ins-{uuid.uuid4().hex[:10]}",
            "timestamp": note.get("timestamp") or _utc_now_iso(),
            "trade_date": str(note.get("trade_date") or date.today().isoformat()),
            "symbol": str(note.get("symbol") or "NIFTY").upper(),
            "title": str(note.get("title") or ""),
            "outlook": str(note.get("outlook") or ""),
            "strategy_for_tomorrow": str(note.get("strategy_for_tomorrow") or ""),
            "why": str(note.get("why") or ""),
            "supporting_metrics": dict(note.get("supporting_metrics") or {}),
            "agent": str(note.get("agent") or "researcher"),
        }
    _xadd(redis_client, INSIGHTS_STREAM, payload)
    _append_jsonl(INSIGHTS_LOG, payload)
    try:
        if redis_client is not None:
            redis_client.client.set(STRATEGY_TODAY_KEY, json.dumps(payload, default=str))
    except Exception:
        logger.debug("Failed writing %s", STRATEGY_TODAY_KEY, exc_info=True)
    return InsightNote(**{k: payload[k] for k in InsightNote.__dataclass_fields__})


def load_conversations(
    redis_client: RedisClient | None = None,
    *,
    limit: int = 150,
) -> tuple[list[dict[str, Any]], str]:
    rows = _read_stream(redis_client, CONVERSATIONS_STREAM, count=limit)
    src = "redis:agent:conversations"
    if not rows:
        rows = _read_jsonl(CONVERSATIONS_LOG, limit=limit)
        src = f"file:{CONVERSATIONS_LOG.name}"
    return rows, src


def load_decisions(
    redis_client: RedisClient | None = None,
    *,
    limit: int = 150,
) -> tuple[list[dict[str, Any]], str]:
    rows = _read_stream(redis_client, DECISIONS_STREAM, count=limit)
    src = "redis:agent:decisions"
    if not rows:
        rows = _read_jsonl(DECISIONS_LOG, limit=limit)
        src = f"file:{DECISIONS_LOG.name}"
    return rows, src


def load_insights(
    redis_client: RedisClient | None = None,
    *,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], str]:
    rows = _read_stream(redis_client, INSIGHTS_STREAM, count=limit)
    src = "redis:agent:insights"
    if not rows:
        rows = _read_jsonl(INSIGHTS_LOG, limit=limit)
        src = f"file:{INSIGHTS_LOG.name}"
    return rows, src


def load_strategy_snapshot(redis_client: RedisClient | None = None) -> dict[str, Any] | None:
    if redis_client is not None:
        try:
            raw = redis_client.client.get(STRATEGY_TODAY_KEY)
            if raw:
                return json.loads(raw)
        except Exception:
            logger.debug("Failed reading strategy snapshot", exc_info=True)
    insights, _ = load_insights(redis_client, limit=1)
    return insights[0] if insights else None


def seed_sample_session(redis_client: RedisClient | None = None) -> None:
    """Write a short demo session so Commentary / Insights are not empty."""
    session = f"sess-{datetime.now(IST).strftime('%Y%m%d')}"
    now = datetime.now(timezone.utc)

    turns = [
        ("orchestrator", "system", "Market open checklist complete. Watching NIFTY ATM ±2 for premium decay."),
        ("researcher", "researcher", "PCR at 1.12 with put OI building near ATM. Bias: mild put-side support."),
        ("risk", "risk", "Daily loss budget ₹5,000 unused. Kill-switch disarmed. Max 1 iron-fly risk unit."),
        ("execution", "execution", "No fill yet — waiting for IV crush confirmation after 10:30 IST."),
        ("researcher", "researcher", "Short buildup on 24500 CE; long buildup on 24400 PE. Favor defined-risk credit."),
        ("orchestrator", "system", "Decision: propose short straddle hedge via iron fly if spot stays in 24450–24550."),
    ]
    for i, (agent, role, msg) in enumerate(turns):
        append_conversation(
            {
                "timestamp": (now - timedelta(minutes=30 - i * 4)).isoformat(),
                "agent": agent,
                "role": role,
                "message": msg,
                "session_id": session,
                "tags": ["nifty", "demo"],
            },
            redis_client=redis_client,
        )

    append_decision(
        {
            "timestamp": (now - timedelta(minutes=8)).isoformat(),
            "agent": "orchestrator",
            "kind": "ENTRY",
            "symbol": "NIFTY",
            "summary": "Propose NIFTY iron fly around ATM 24500",
            "rationale": "Range-bound open + elevated IV; PCR > 1 suggests put support under ATM.",
            "confidence": 0.68,
            "strike": 24500,
            "action": "SELL",
            "status": "PROPOSED",
            "meta": {"structure": "iron_fly", "width": 100},
        },
        redis_client=redis_client,
    )
    append_decision(
        {
            "timestamp": (now - timedelta(minutes=3)).isoformat(),
            "agent": "risk",
            "kind": "SKIP",
            "symbol": "NIFTY",
            "summary": "Defer entry until post-10:30 IV settle",
            "rationale": "Opening auction noise; latency SLA green but edge not confirmed.",
            "confidence": 0.55,
            "status": "ACCEPTED",
        },
        redis_client=redis_client,
    )

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    append_insight(
        {
            "trade_date": date.today().isoformat(),
            "symbol": "NIFTY",
            "title": "Range day → tomorrow favor credit spreads",
            "outlook": "Expect continued mean-reversion unless overnight gap > 0.6%.",
            "strategy_for_tomorrow": (
                f"For {tomorrow}: sell ATM iron fly or tight iron condor on NIFTY "
                "if IV rank remains elevated and PCR stays between 0.9–1.3."
            ),
            "why": (
                "Today’s tape showed balanced CE/PE OI with no persistent directional "
                "buildup. Premium decay rewarded defined-risk short vol. A gap open "
                "invalidates and flips to debit verticals."
            ),
            "supporting_metrics": {
                "pcr": 1.12,
                "iv_rank_est": "elevated",
                "bias": "neutral-range",
                "invalidation": "gap > 0.6% or PCR break < 0.8 / > 1.5",
            },
            "agent": "researcher",
        },
        redis_client=redis_client,
    )


def build_insight_from_market(
    symbol: str = "NIFTY",
    *,
    redis_client: RedisClient | None = None,
) -> InsightNote:
    """
    Best-effort insight from live Redis chain + optional parquet history.
    Falls back to a structured neutral note when data is thin.
    """
    symbol_u = symbol.strip().upper()
    metrics: dict[str, Any] = {"symbol": symbol_u}
    outlook = "Insufficient live chain data — keep risk light until PCR/IV confirm."
    strategy = "Observe only tomorrow morning; wait for first 30 minutes before short vol."
    why = "No reliable option-chain snapshot in Redis hot store yet."

    chain = None
    if redis_client is not None:
        try:
            chain = redis_client.get_option_chain_state(symbol_u)
        except Exception:
            logger.debug("chain read failed", exc_info=True)

    if chain:
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
        metrics.update(
            {
                "underlying_ltp": chain.get("underlying_ltp"),
                "atm": chain.get("atm"),
                "expiry": chain.get("expiry"),
                "call_oi": call_oi,
                "put_oi": put_oi,
                "pcr": pcr,
            }
        )
        if pcr is not None:
            if pcr >= 1.2:
                outlook = "Put-heavy OI (PCR elevated) — mild support bias under ATM."
                strategy = (
                    "Tomorrow: prefer bull put spreads / iron fly skewed slightly above ATM "
                    "if spot holds above support; skip naked shorts."
                )
                why = f"Live PCR={pcr:.2f} with put OI {put_oi:,} vs call OI {call_oi:,}."
            elif pcr <= 0.85:
                outlook = "Call-heavy OI — upside pressure / short-covering risk."
                strategy = (
                    "Tomorrow: favor bear call spreads or wait; avoid naked call sells into strength."
                )
                why = f"Live PCR={pcr:.2f} with call OI dominating put OI."
            else:
                outlook = "Balanced PCR — range / premium-decay regime."
                strategy = (
                    "Tomorrow: ATM iron fly / tight iron condor if IV stays rich; "
                    "invalidate on gap > 0.6%."
                )
                why = f"Live PCR={pcr:.2f} near neutral with ATM={chain.get('atm')}."

    # Prefer multi-year Dhan history under data/history/, then tick partitions.
    try:
        import pandas as pd

        history_path = PROJECT_ROOT / "data" / "history" / f"{symbol_u.lower()}_daily.parquet"
        path: Path | None = history_path if history_path.is_file() else None
        if path is None:
            from services.parquet_exporter import partition_path

            yesterday = date.today() - timedelta(days=1)
            candidate = partition_path(yesterday, symbol_u)
            if candidate.is_file():
                path = candidate
            else:
                data_root = PROJECT_ROOT / "data"
                candidates = sorted(
                    data_root.glob(f"*/{symbol_u.lower()}.parquet"),
                    reverse=True,
                )
                path = candidates[0] if candidates else None

        if path is not None and path.is_file():
            df = pd.read_parquet(path)
            metrics["history_file"] = str(path.relative_to(PROJECT_ROOT))
            metrics["history_rows"] = int(len(df))
            if "close" in df.columns and df["close"].notna().any():
                metrics["hist_last_close"] = float(df["close"].iloc[-1])
                metrics["hist_close_median"] = float(df["close"].median(skipna=True))
                rets = df["close"].pct_change().dropna()
                if len(rets) > 20:
                    metrics["hist_vol_20d"] = float(rets.tail(20).std() * (252 ** 0.5))
                    metrics["hist_ret_20d"] = float(
                        df["close"].iloc[-1] / df["close"].iloc[-21] - 1
                    )
            if "iv" in df.columns and df["iv"].notna().any():
                metrics["hist_iv_median"] = float(df["iv"].median(skipna=True))
            if "oi" in df.columns and df["oi"].notna().any():
                metrics["hist_oi_max"] = int(df["oi"].max(skipna=True))
            last = metrics.get("hist_last_close")
            vol = metrics.get("hist_vol_20d")
            why = (
                f"Dhan history {path.name}: {metrics['history_rows']} daily bars; "
                f"last_close={last}; realized_vol_20d={vol}."
            )
            if metrics.get("pcr") is None:
                outlook = "Historic regime available — confirm with live PCR at open."
                strategy = (
                    "Tomorrow: size short-vol only if open auction keeps spot near "
                    f"{last} ±0.4% and IV does not spike vs 20d realized."
                )
            else:
                strategy = (
                    f"{strategy} Anchor sizing to last close {last} and 20d vol {vol}."
                )
    except Exception:
        logger.debug("history insight enrichment skipped", exc_info=True)

    note = append_insight(
        {
            "trade_date": date.today().isoformat(),
            "symbol": symbol_u,
            "title": f"{symbol_u} forward outlook",
            "outlook": outlook,
            "strategy_for_tomorrow": strategy,
            "why": why,
            "supporting_metrics": metrics,
            "agent": "researcher",
        },
        redis_client=redis_client,
    )
    return note
