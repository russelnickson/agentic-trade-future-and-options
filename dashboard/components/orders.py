"""Order audit log for the Streamlit terminal (Redis stream or execution log file)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

import pandas as pd
import streamlit as st

from database.redis_client import RedisClient
from dashboard.timefmt import format_ist

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "execution_orders.jsonl"

# Redis keys used by the execution engine.
ORDERS_STREAM_KEY = "orders:audit"  # Redis Stream
ORDERS_LIST_KEY = "orders:audit:list"  # optional LPUSH fallback

Action = Literal["BUY", "SELL"]
Status = Literal["COMPLETE", "REJECTED", "PENDING", "CANCELLED"]


@dataclass
class OrderAuditRow:
    order_id: str
    timestamp: str
    strategy_name: str
    strike: float | str
    action: str
    quantity: int
    status: str
    execution_latency_ms: float | None

    def to_display_dict(self) -> dict[str, Any]:
        return {
            "Order ID": self.order_id,
            "Timestamp": format_ist(self.timestamp),
            "Strategy Name": self.strategy_name,
            "Strike": self.strike,
            "Action": self.action,
            "Quantity": self.quantity,
            "Status": self.status,
            "Execution Latency (ms)": self.execution_latency_ms
            if self.execution_latency_ms is not None
            else "—",
        }


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_action(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"BUY", "B", "LONG"}:
        return "BUY"
    if text in {"SELL", "S", "SHORT"}:
        return "SELL"
    return text or "—"


def _normalize_status(value: Any) -> str:
    text = str(value or "").strip().upper()
    aliases = {
        "COMPLETE": "COMPLETE",
        "COMPLETED": "COMPLETE",
        "FILLED": "COMPLETE",
        "SUCCESS": "COMPLETE",
        "REJECTED": "REJECTED",
        "REJECT": "REJECTED",
        "FAILED": "REJECTED",
        "PENDING": "PENDING",
        "OPEN": "PENDING",
        "CANCELLED": "CANCELLED",
        "CANCELED": "CANCELLED",
    }
    return aliases.get(text, text or "—")


def _normalize_timestamp(value: Any) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    text = str(value).strip()
    return text or datetime.now(timezone.utc).isoformat()


def parse_order_event(payload: dict[str, Any]) -> OrderAuditRow | None:
    """Normalize heterogeneous execution-engine payloads into an audit row."""
    if not isinstance(payload, dict):
        return None

    order_id = (
        payload.get("order_id")
        or payload.get("orderId")
        or payload.get("id")
        or payload.get("broker_order_id")
    )
    if not order_id:
        return None

    strike_raw = payload.get("strike", payload.get("Strike"))
    strike: float | str
    parsed_strike = _as_float(strike_raw)
    strike = parsed_strike if parsed_strike is not None else (strike_raw or "—")

    latency = _as_float(
        payload.get(
            "execution_latency_ms",
            payload.get("latency_ms", payload.get("exec_latency_ms")),
        )
    )

    return OrderAuditRow(
        order_id=str(order_id),
        timestamp=_normalize_timestamp(
            payload.get("timestamp", payload.get("ts", payload.get("created_at")))
        ),
        strategy_name=str(
            payload.get("strategy_name")
            or payload.get("strategy")
            or payload.get("algo")
            or "—"
        ),
        strike=strike,
        action=_normalize_action(payload.get("action", payload.get("side", payload.get("transaction_type")))),
        quantity=_as_int(payload.get("quantity", payload.get("qty"))),
        status=_normalize_status(payload.get("status", payload.get("order_status"))),
        execution_latency_ms=latency,
    )


def _parse_stream_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Redis stream entries may store a single JSON blob or flat field map."""
    if "json" in fields:
        try:
            return json.loads(fields["json"])
        except (TypeError, json.JSONDecodeError):
            return dict(fields)
    if "data" in fields and len(fields) <= 2:
        try:
            return json.loads(fields["data"])
        except (TypeError, json.JSONDecodeError):
            return dict(fields)
    # Flat map already looks like an order event.
    if any(k in fields for k in ("order_id", "orderId", "action", "status")):
        return dict(fields)
    return dict(fields)


def read_orders_from_redis(
    redis_client: RedisClient,
    *,
    limit: int = 200,
) -> list[OrderAuditRow]:
    """Read newest-first audit rows from Redis Stream and/or list."""
    client = redis_client.client
    rows: list[OrderAuditRow] = []
    seen: set[str] = set()

    # Prefer Redis Streams (XREVRANGE).
    try:
        entries = client.xrevrange(ORDERS_STREAM_KEY, max="+", min="-", count=limit)
        for _id, fields in entries or []:
            payload = _parse_stream_fields(fields)
            row = parse_order_event(payload)
            if row and row.order_id not in seen:
                seen.add(row.order_id)
                rows.append(row)
    except Exception:
        logger.debug("Redis stream %s unavailable", ORDERS_STREAM_KEY, exc_info=True)

    # Fallback: LIST of JSON strings (LPUSH / LRANGE).
    if len(rows) < limit:
        try:
            remaining = limit - len(rows)
            blobs = client.lrange(ORDERS_LIST_KEY, 0, remaining - 1)
            for blob in blobs or []:
                try:
                    payload = json.loads(blob)
                except (TypeError, json.JSONDecodeError):
                    continue
                row = parse_order_event(payload)
                if row and row.order_id not in seen:
                    seen.add(row.order_id)
                    rows.append(row)
        except Exception:
            logger.debug("Redis list %s unavailable", ORDERS_LIST_KEY, exc_info=True)

    return rows[:limit]


def read_orders_from_log(
    log_path: Path | None = None,
    *,
    limit: int = 200,
) -> list[OrderAuditRow]:
    """Read JSONL execution-engine log (newest last in file → reverse for display)."""
    path = log_path or DEFAULT_LOG_PATH
    if not path.exists():
        return []

    rows: list[OrderAuditRow] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        logger.exception("Failed reading order log %s", path)
        return []

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        row = parse_order_event(payload)
        if row:
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def load_order_audit(
    redis_client: RedisClient | None = None,
    *,
    log_path: Path | None = None,
    limit: int = 200,
) -> tuple[list[OrderAuditRow], str]:
    """
    Load audit rows: Redis stream/list first, then execution JSONL log.

    Returns (rows, source_label).
    """
    if redis_client is not None:
        try:
            rows = read_orders_from_redis(redis_client, limit=limit)
            if rows:
                return rows, f"redis:{ORDERS_STREAM_KEY}"
        except Exception:
            logger.exception("Redis order audit read failed")

    rows = read_orders_from_log(log_path, limit=limit)
    if rows:
        return rows, f"file:{log_path or DEFAULT_LOG_PATH}"
    return [], "none"


def append_order_audit(
    event: dict[str, Any],
    *,
    redis_client: RedisClient | None = None,
    log_path: Path | None = None,
) -> None:
    """
    Helper for the execution engine: persist one audit event to Redis + JSONL.

    Stream field layout: ``{"json": "<serialized event>"}``.
    """
    row = parse_order_event(event)
    if row is None:
        raise ValueError("order event missing order_id")

    payload = {
        "order_id": row.order_id,
        "timestamp": row.timestamp,
        "strategy_name": row.strategy_name,
        "strike": row.strike,
        "action": row.action,
        "quantity": row.quantity,
        "status": row.status,
        "execution_latency_ms": row.execution_latency_ms,
    }
    blob = json.dumps(payload, default=str)

    path = log_path or DEFAULT_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(blob + "\n")

    if redis_client is not None:
        try:
            redis_client.client.xadd(
                ORDERS_STREAM_KEY,
                {"json": blob},
                maxlen=5_000,
                approximate=True,
            )
            redis_client.client.lpush(ORDERS_LIST_KEY, blob)
            redis_client.client.ltrim(ORDERS_LIST_KEY, 0, 4_999)
        except Exception:
            logger.exception("Failed to write order audit to Redis")


def _style_orders(df: pd.DataFrame) -> Any:
    def _status_color(val: Any) -> str:
        text = str(val).upper()
        if text == "COMPLETE":
            return "color: #0a7a32; font-weight: 600"
        if text == "REJECTED":
            return "color: #c62828; font-weight: 600"
        if text == "PENDING":
            return "color: #ef6c00; font-weight: 600"
        return ""

    def _action_color(val: Any) -> str:
        text = str(val).upper()
        if text == "BUY":
            return "color: #1565c0; font-weight: 600"
        if text == "SELL":
            return "color: #6a1b9a; font-weight: 600"
        return ""

    styler = df.style.map(_status_color, subset=["Status"]).map(
        _action_color, subset=["Action"]
    )

    def _fmt_latency(val: Any) -> str:
        if val == "—" or val is None:
            return "—"
        try:
            return f"{float(val):.1f}"
        except (TypeError, ValueError):
            return str(val)

    def _fmt_strike(val: Any) -> str:
        if val == "—" or val is None:
            return "—"
        try:
            num = float(val)
            return f"{num:.0f}" if num == int(num) else f"{num:.2f}"
        except (TypeError, ValueError):
            return str(val)

    return styler.format(
        {
            "Execution Latency (ms)": _fmt_latency,
            "Strike": _fmt_strike,
        }
    )


def orders_dataframe(rows: Iterable[OrderAuditRow]) -> pd.DataFrame:
    records = [r.to_display_dict() for r in rows]
    if not records:
        return pd.DataFrame(
            columns=[
                "Order ID",
                "Timestamp",
                "Strategy Name",
                "Strike",
                "Action",
                "Quantity",
                "Status",
                "Execution Latency (ms)",
            ]
        )
    return pd.DataFrame(records)


def render_orders_table(
    redis_client: RedisClient | None = None,
    *,
    log_path: Path | None = None,
    limit: int = 200,
) -> list[OrderAuditRow]:
    """Render the execution audit log table in Streamlit."""
    st.subheader("Order Audit Log")
    rows, source = load_order_audit(
        redis_client, log_path=log_path, limit=limit
    )
    st.caption(f"Source: `{source}` · showing up to {limit} newest events")

    df = orders_dataframe(rows)
    if df.empty:
        st.info(
            "No order audit events yet. The execution engine should write to "
            f"Redis stream `{ORDERS_STREAM_KEY}` or `{DEFAULT_LOG_PATH}`."
        )
        return rows

    complete = sum(1 for r in rows if r.status == "COMPLETE")
    rejected = sum(1 for r in rows if r.status == "REJECTED")
    latencies = [r.execution_latency_ms for r in rows if r.execution_latency_ms is not None]
    c1, c2, c3 = st.columns(3)
    c1.metric("COMPLETE", complete)
    c2.metric("REJECTED", rejected)
    c3.metric(
        "Avg latency (ms)",
        f"{sum(latencies) / len(latencies):.1f}" if latencies else "—",
    )

    try:
        st.dataframe(_style_orders(df), use_container_width=True, hide_index=True)
    except Exception:
        st.dataframe(df, use_container_width=True, hide_index=True)

    return rows
