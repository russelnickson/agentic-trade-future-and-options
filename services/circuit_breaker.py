"""Background daily P&L circuit breaker.

Monitors cumulative day P&L. When loss reaches ``MAX_DAILY_LOSS`` (default ₹5,000),
cancels pending orders, squares off positions, and locks further order placement.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from config.settings import get_settings
from dashboard.components.risk_controls import (
    is_trading_disabled,
    load_terminal_controls,
    save_terminal_controls,
    trigger_emergency_square_off,
)
from database.redis_client import RedisClient, get_redis_client

logger = logging.getLogger(__name__)

BrokerName = Literal["dhan", "zerodha"]

PNL_STATE_KEY = "risk:circuit_breaker:pnl"
TRIP_STATE_KEY = "risk:circuit_breaker:tripped"

_PENDING_DHAN = frozenset(
    {"PENDING", "TRANSIT", "OPEN", "TRIGGERED", "PART_TRADED", "PENDING_CONFIRMATION"}
)
_PENDING_ZERODHA = frozenset(
    {
        "OPEN",
        "TRIGGER PENDING",
        "AMO REQ RECEIVED",
        "MODIFY PENDING",
        "CANCEL PENDING",
        "PUT ORDER REQ RECEIVED",
        "VALIDATION PENDING",
        "OPEN PENDING",
    }
)


@dataclass
class DailyPnLSnapshot:
    broker: BrokerName
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    as_of: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _unwrap_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, list):
            return [p for p in data if isinstance(p, dict)]
        if isinstance(data, dict):
            for key in ("net", "orders", "positions"):
                inner = data.get(key)
                if isinstance(inner, list):
                    return [p for p in inner if isinstance(p, dict)]
    return []


def fetch_daily_pnl(broker: BrokerName = "dhan") -> DailyPnLSnapshot:
    """Sum realized + unrealized P&L for the trading day from broker positions."""
    as_of = datetime.now(timezone.utc).isoformat()
    try:
        if broker == "dhan":
            return _dhan_daily_pnl(as_of)
        if broker == "zerodha":
            return _zerodha_daily_pnl(as_of)
        raise ValueError(f"Unsupported broker: {broker!r}")
    except Exception as exc:
        logger.exception("Daily P&L fetch failed (%s)", broker)
        return DailyPnLSnapshot(
            broker=broker,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_pnl=0.0,
            as_of=as_of,
            error=str(exc),
        )


def _dhan_daily_pnl(as_of: str) -> DailyPnLSnapshot:
    from dhanhq import DhanContext, Portfolio

    settings = get_settings()
    ctx = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
    payload = Portfolio(ctx).get_positions()
    realized = 0.0
    unrealized = 0.0
    for pos in _unwrap_list(payload):
        realized += _as_float(pos.get("realizedProfit"))
        unrealized += _as_float(pos.get("unrealizedProfit"))
    total = realized + unrealized
    return DailyPnLSnapshot(
        broker="dhan",
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        total_pnl=total,
        as_of=as_of,
    )


def _zerodha_daily_pnl(as_of: str) -> DailyPnLSnapshot:
    from kiteconnect import KiteConnect

    settings = get_settings()
    token = settings.zerodha_access_token or os.getenv("ZERODHA_ACCESS_TOKEN", "")
    if not token:
        return DailyPnLSnapshot(
            broker="zerodha",
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_pnl=0.0,
            as_of=as_of,
            error="ZERODHA_ACCESS_TOKEN missing",
        )

    kite = KiteConnect(api_key=settings.zerodha_api_key)
    kite.set_access_token(token)
    payload = kite.positions() or {}
    net = payload.get("net") if isinstance(payload, dict) else payload

    realized = 0.0
    unrealized = 0.0
    for pos in _unwrap_list(net):
        if pos.get("realised") is not None or pos.get("unrealised") is not None:
            realized += _as_float(pos.get("realised"))
            unrealized += _as_float(pos.get("unrealised"))
        else:
            unrealized += _as_float(pos.get("pnl"))

    return DailyPnLSnapshot(
        broker="zerodha",
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        total_pnl=realized + unrealized,
        as_of=as_of,
    )


def cancel_pending_orders(broker: BrokerName = "dhan") -> dict[str, Any]:
    """Cancel all open/pending orders at the broker."""
    if broker == "dhan":
        return _cancel_pending_dhan()
    if broker == "zerodha":
        return _cancel_pending_zerodha()
    raise ValueError(f"Unsupported broker: {broker!r}")


def _cancel_pending_dhan() -> dict[str, Any]:
    from dhanhq import DhanContext, Order

    settings = get_settings()
    ctx = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
    order_api = Order(ctx)
    payload = order_api.get_order_list()
    cancelled: list[str] = []
    errors: list[dict[str, Any]] = []

    for order in _unwrap_list(payload):
        status = str(order.get("orderStatus") or order.get("status") or "").upper()
        order_id = str(order.get("orderId") or order.get("order_id") or "")
        if not order_id or status not in _PENDING_DHAN:
            continue
        try:
            resp = order_api.cancel_order(order_id)
            cancelled.append(order_id)
            logger.warning("Cancelled Dhan pending order %s → %s", order_id, resp)
        except Exception as exc:
            errors.append({"order_id": order_id, "error": str(exc)})
            logger.exception("Failed cancelling Dhan order %s", order_id)

    return {"cancelled": cancelled, "errors": errors, "broker": "dhan"}


def _cancel_pending_zerodha() -> dict[str, Any]:
    from kiteconnect import KiteConnect

    settings = get_settings()
    token = settings.zerodha_access_token or os.getenv("ZERODHA_ACCESS_TOKEN", "")
    if not token:
        return {
            "cancelled": [],
            "errors": [{"error": "ZERODHA_ACCESS_TOKEN missing"}],
            "broker": "zerodha",
        }

    kite = KiteConnect(api_key=settings.zerodha_api_key)
    kite.set_access_token(token)
    cancelled: list[str] = []
    errors: list[dict[str, Any]] = []

    for order in kite.orders() or []:
        status = str(order.get("status") or "").upper()
        order_id = str(order.get("order_id") or "")
        variety = order.get("variety") or "regular"
        if not order_id or status not in _PENDING_ZERODHA:
            continue
        try:
            kite.cancel_order(variety=variety, order_id=order_id)
            cancelled.append(order_id)
            logger.warning("Cancelled Zerodha pending order %s (%s)", order_id, variety)
        except Exception as exc:
            errors.append({"order_id": order_id, "error": str(exc)})
            logger.exception("Failed cancelling Zerodha order %s", order_id)

    return {"cancelled": cancelled, "errors": errors, "broker": "zerodha"}


def lock_order_placement(client: RedisClient, *, reason: str) -> dict[str, Any]:
    """Persist hard lock so agents cannot place new orders for the day."""
    controls = load_terminal_controls(client)
    controls.update(
        {
            "kill_switch": True,
            "trading_disabled": True,
            "order_placement_locked": True,
            "circuit_breaker_reason": reason,
        }
    )
    return save_terminal_controls(client, controls)


def trip_circuit_breaker(
    client: RedisClient,
    snapshot: DailyPnLSnapshot,
    *,
    max_daily_loss: float,
    broker: BrokerName = "dhan",
) -> dict[str, Any]:
    """
    Cancel pending orders, square off / broadcast liquidation, lock placements.
    """
    reason = (
        f"MAX_DAILY_LOSS breached: total_pnl={snapshot.total_pnl:.2f} "
        f"<= -{max_daily_loss:.2f}"
    )
    logger.error("CIRCUIT BREAKER TRIPPED — %s", reason)

    cancel_result = cancel_pending_orders(broker)
    emergency = trigger_emergency_square_off(
        client,
        broker=broker,
        call_broker_apis=True,
        source="services.circuit_breaker",
        reason=reason,
    )
    controls = lock_order_placement(client, reason=reason)

    trip_payload = {
        "tripped_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "pnl": snapshot.to_dict(),
        "max_daily_loss": max_daily_loss,
        "cancel_orders": cancel_result,
        "emergency": emergency.to_dict(),
        "controls": {
            "kill_switch": controls.get("kill_switch"),
            "trading_disabled": controls.get("trading_disabled"),
            "order_placement_locked": controls.get("order_placement_locked"),
            "trading_disabled_until": controls.get("trading_disabled_until"),
        },
    }
    try:
        client.client.set(TRIP_STATE_KEY, json.dumps(trip_payload, default=str))
    except Exception:
        logger.exception("Failed to persist circuit breaker trip state")

    return trip_payload


class CircuitBreaker:
    """Background monitor loop for daily loss limits."""

    def __init__(
        self,
        *,
        max_daily_loss: float | None = None,
        poll_sec: float | None = None,
        broker: BrokerName | None = None,
        redis_client: RedisClient | None = None,
    ) -> None:
        settings = get_settings()
        self.max_daily_loss = float(
            max_daily_loss
            if max_daily_loss is not None
            else settings.max_daily_loss
        )
        self.poll_sec = float(
            poll_sec if poll_sec is not None else settings.circuit_breaker_poll_sec
        )
        raw_broker = (broker or settings.trade_broker or "dhan").lower()
        if raw_broker not in {"dhan", "zerodha"}:
            raise ValueError(f"Unsupported broker: {raw_broker!r}")
        self.broker: BrokerName = raw_broker  # type: ignore[assignment]
        self._redis = redis_client or get_redis_client()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tripped = False
        self.last_snapshot: DailyPnLSnapshot | None = None

        if self.max_daily_loss <= 0:
            raise ValueError("max_daily_loss must be positive (INR absolute loss limit)")

    @property
    def loss_threshold(self) -> float:
        """P&L level that trips the breaker (negative number)."""
        return -abs(self.max_daily_loss)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="circuit-breaker",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "CircuitBreaker started (broker=%s, MAX_DAILY_LOSS=₹%.2f, poll=%.1fs)",
            self.broker,
            self.max_daily_loss,
            self.poll_sec,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("CircuitBreaker stopped")

    def check_once(self) -> DailyPnLSnapshot:
        """Fetch P&L once and trip if the loss limit is breached."""
        if is_trading_disabled(client=self._redis) and self._already_tripped():
            snapshot = fetch_daily_pnl(self.broker)
            self.last_snapshot = snapshot
            self._persist_pnl(snapshot)
            return snapshot

        snapshot = fetch_daily_pnl(self.broker)
        self.last_snapshot = snapshot
        self._persist_pnl(snapshot)

        if snapshot.error:
            logger.warning("CircuitBreaker P&L error: %s", snapshot.error)
            return snapshot

        logger.info(
            "Daily P&L realized=%.2f unrealized=%.2f total=%.2f (limit=%.2f)",
            snapshot.realized_pnl,
            snapshot.unrealized_pnl,
            snapshot.total_pnl,
            self.loss_threshold,
        )

        if snapshot.total_pnl <= self.loss_threshold and not self._already_tripped():
            trip_circuit_breaker(
                self._redis,
                snapshot,
                max_daily_loss=self.max_daily_loss,
                broker=self.broker,
            )
            self._tripped = True

        return snapshot

    def _already_tripped(self) -> bool:
        if self._tripped:
            return True
        try:
            return bool(self._redis.client.get(TRIP_STATE_KEY))
        except Exception:
            return False

    def _persist_pnl(self, snapshot: DailyPnLSnapshot) -> None:
        try:
            self._redis.client.set(
                PNL_STATE_KEY,
                json.dumps(snapshot.to_dict(), default=str),
            )
        except Exception:
            logger.debug("Failed to persist P&L snapshot", exc_info=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:
                logger.exception("CircuitBreaker loop error")
            self._stop.wait(self.poll_sec)

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            logger.info("CircuitBreaker interrupted")
        finally:
            self.stop()


def start_circuit_breaker(
    *,
    max_daily_loss: float | None = None,
    poll_sec: float | None = None,
    broker: BrokerName | None = None,
) -> CircuitBreaker:
    breaker = CircuitBreaker(
        max_daily_loss=max_daily_loss,
        poll_sec=poll_sec,
        broker=broker,
    )
    breaker.start()
    return breaker


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    start_circuit_breaker().run_forever()
