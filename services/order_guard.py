"""Protected LIMIT order placement with slippage buffer (never plain MARKET)."""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from config.settings import get_settings
from dashboard.components.orders import append_order_audit
from dashboard.components.risk_controls import is_trading_disabled, load_terminal_controls
from database.redis_client import RedisClient, get_redis_client

logger = logging.getLogger(__name__)

BrokerName = Literal["dhan", "zerodha"]
Action = Literal["BUY", "SELL"]

DEFAULT_SLIPPAGE = 0.50  # INR buffer on option LTP
DEFAULT_TICK_SIZE = 0.05  # common NIFTY option tick; override per contract when known


class OrderGuardError(RuntimeError):
    """Raised when a protected order is blocked or fails validation."""


@dataclass(frozen=True)
class ProtectedOrderRequest:
    symbol: str
    action: Action
    qty: int
    ltp: float
    limit_price: float
    slippage: float
    tick_size: float
    order_type: str = "LIMIT"
    product: str = "INTRADAY"
    broker: BrokerName = "dhan"
    security_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProtectedOrderResult:
    success: bool
    order_id: str | None
    limit_price: float
    request: ProtectedOrderRequest
    broker_response: Any = None
    error: str | None = None
    latency_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["request"] = self.request.to_dict()
        return data


def _normalize_action(action: str) -> Action:
    text = str(action).strip().upper()
    if text in {"BUY", "B", "LONG"}:
        return "BUY"
    if text in {"SELL", "S", "SHORT"}:
        return "SELL"
    raise OrderGuardError(f"Unsupported action: {action!r} (expected BUY/SELL)")


def round_to_tick(price: float, tick_size: float, *, action: Action) -> float:
    """
    Snap limit price to exchange tick size.

    BUY pads up (harder to miss), SELL pads down — never improves the fill vs buffer.
    """
    if tick_size <= 0:
        raise OrderGuardError(f"tick_size must be positive, got {tick_size}")
    if action == "BUY":
        ticks = math.ceil(price / tick_size - 1e-12)
    else:
        ticks = math.floor(price / tick_size + 1e-12)
    rounded = round(ticks * tick_size, 10)
    # Avoid floating dust (e.g. 100.1000000001).
    decimals = max(0, len(str(tick_size).rstrip("0").split(".")[-1]) if "." in str(tick_size) else 0)
    return float(f"{rounded:.{max(decimals, 2)}f}")


def compute_protected_limit_price(
    ltp: float,
    action: str,
    *,
    slippage: float = DEFAULT_SLIPPAGE,
    tick_size: float = DEFAULT_TICK_SIZE,
) -> float:
    """
    Dynamic LIMIT price with slippage buffer.

    - BUY  → LTP + slippage (e.g. 100.00 + 0.50 = 100.50)
    - SELL → LTP - slippage (e.g. 100.00 - 0.50 =  99.50)
    """
    side = _normalize_action(action)
    if ltp <= 0:
        raise OrderGuardError(f"ltp must be positive, got {ltp}")
    if slippage < 0:
        raise OrderGuardError(f"slippage must be >= 0, got {slippage}")

    raw = ltp + slippage if side == "BUY" else ltp - slippage
    if raw <= 0:
        raise OrderGuardError(
            f"Protected SELL limit would be non-positive (ltp={ltp}, slippage={slippage})"
        )
    return round_to_tick(raw, tick_size, action=side)


def assert_order_placement_allowed(redis_client: RedisClient | None = None) -> None:
    """Block placement when kill-switch / circuit breaker has locked trading."""
    client = redis_client or get_redis_client()
    controls = load_terminal_controls(client)
    if is_trading_disabled(controls, client=client):
        raise OrderGuardError(
            "Order placement locked: kill-switch / emergency disable is active "
            f"(until {controls.get('trading_disabled_until')})"
        )
    if controls.get("order_placement_locked"):
        raise OrderGuardError(
            "Order placement locked by circuit breaker: "
            f"{controls.get('circuit_breaker_reason') or 'order_placement_locked'}"
        )


def place_protected_limit_order(
    symbol: str,
    action: str,
    qty: int,
    ltp: float,
    *,
    slippage: float = DEFAULT_SLIPPAGE,
    tick_size: float = DEFAULT_TICK_SIZE,
    security_id: str | int | None = None,
    product: str | None = None,
    broker: BrokerName | None = None,
    exchange: str | None = None,
    tag: str | None = None,
    redis_client: RedisClient | None = None,
    dry_run: bool = False,
) -> ProtectedOrderResult:
    """
    Place an options order as a **LIMIT** (never MARKET) with slippage buffer.

    Example: ``place_protected_limit_order("NIFTY28JUL24500CE", "BUY", 65, 120.0)``
    sends a LIMIT buy at ``120.0 + 0.50`` (tick-rounded), reducing adverse MARKET fills.
    """
    side = _normalize_action(action)
    quantity = int(qty)
    if quantity <= 0:
        raise OrderGuardError(f"qty must be positive, got {qty}")

    if broker is not None:
        broker_name = broker.lower()  # type: ignore[assignment]
    elif dry_run:
        broker_name = "dhan"
    else:
        settings = get_settings()
        broker_name = (settings.trade_broker or "dhan").lower()  # type: ignore[assignment]
    if broker_name not in {"dhan", "zerodha"}:
        raise OrderGuardError(f"Unsupported broker: {broker_name!r}")

    redis = redis_client
    if not dry_run:
        try:
            from config.runtime_mode import is_local_paper_desk, paper_trading_enabled

            if is_local_paper_desk() or paper_trading_enabled():
                dry_run = True
        except Exception:
            pass

    if not dry_run:
        assert_order_placement_allowed(redis)

    limit_price = compute_protected_limit_price(
        float(ltp), side, slippage=float(slippage), tick_size=float(tick_size)
    )
    product_type = product or ("INTRADAY" if broker_name == "dhan" else "MIS")

    request = ProtectedOrderRequest(
        symbol=str(symbol),
        action=side,
        qty=quantity,
        ltp=float(ltp),
        limit_price=limit_price,
        slippage=float(slippage),
        tick_size=float(tick_size),
        order_type="LIMIT",
        product=product_type,
        broker=broker_name,
        security_id=str(security_id) if security_id is not None else None,
    )

    logger.info(
        "Protected LIMIT %s %s qty=%s ltp=%.2f → limit=%.2f (slippage=%.2f, tick=%.2f)",
        side,
        symbol,
        quantity,
        float(ltp),
        limit_price,
        float(slippage),
        float(tick_size),
    )

    if dry_run:
        return ProtectedOrderResult(
            success=True,
            order_id="DRY-RUN",
            limit_price=limit_price,
            request=request,
            broker_response={"dry_run": True},
            latency_ms=0.0,
        )

    started = time.perf_counter()
    try:
        if broker_name == "dhan":
            order_id, response = _place_dhan_limit(request, exchange=exchange, tag=tag)
        else:
            order_id, response = _place_zerodha_limit(request, exchange=exchange, tag=tag)
        latency_ms = (time.perf_counter() - started) * 1000.0
        result = ProtectedOrderResult(
            success=True,
            order_id=str(order_id) if order_id is not None else None,
            limit_price=limit_price,
            request=request,
            broker_response=response,
            latency_ms=latency_ms,
        )
        _audit(result, redis_client=redis)
        return result
    except OrderGuardError:
        raise
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        logger.exception("Protected limit order failed")
        result = ProtectedOrderResult(
            success=False,
            order_id=None,
            limit_price=limit_price,
            request=request,
            broker_response=None,
            error=str(exc),
            latency_ms=latency_ms,
        )
        _audit(result, redis_client=redis)
        raise OrderGuardError(str(exc)) from exc


def _place_dhan_limit(
    request: ProtectedOrderRequest,
    *,
    exchange: str | None,
    tag: str | None,
) -> tuple[str | None, Any]:
    from dhanhq import DhanContext, Order

    settings = get_settings()
    security_id = request.security_id or request.symbol
    # If symbol looks non-numeric, require explicit security_id.
    if not str(security_id).isdigit() and request.security_id is None:
        raise OrderGuardError(
            "Dhan orders require numeric security_id=… "
            f"(got symbol={request.symbol!r})"
        )

    ctx = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
    response = Order(ctx).place_order(
        security_id=str(security_id),
        exchange_segment=(exchange or "NSE_FNO"),
        transaction_type=request.action,
        quantity=request.qty,
        order_type="LIMIT",
        product_type=request.product,
        price=request.limit_price,
        trigger_price=0,
        tag=tag,
    )
    order_id = None
    if isinstance(response, dict):
        data = response.get("data") if isinstance(response.get("data"), dict) else response
        order_id = (
            (data or {}).get("orderId")
            or (data or {}).get("order_id")
            or response.get("orderId")
        )
        status = str(response.get("status") or "").lower()
        if status == "failure":
            raise OrderGuardError(f"Dhan order rejected: {response}")
    return (str(order_id) if order_id else None), response


def _place_zerodha_limit(
    request: ProtectedOrderRequest,
    *,
    exchange: str | None,
    tag: str | None,
) -> tuple[str | None, Any]:
    from kiteconnect import KiteConnect

    settings = get_settings()
    token = settings.zerodha_access_token or os.getenv("ZERODHA_ACCESS_TOKEN", "")
    if not token:
        raise OrderGuardError("ZERODHA_ACCESS_TOKEN missing")

    kite = KiteConnect(api_key=settings.zerodha_api_key)
    kite.set_access_token(token)
    # Zerodha product: MIS (intraday) / NRML (overnight F&O)
    product = request.product.upper()
    if product == "INTRADAY":
        product = "MIS"

    order_id = kite.place_order(
        variety="regular",
        exchange=(exchange or "NFO"),
        tradingsymbol=request.symbol,
        transaction_type=request.action,
        quantity=request.qty,
        product=product,
        order_type="LIMIT",
        price=request.limit_price,
        validity="DAY",
        tag=tag,
    )
    return str(order_id), {"order_id": order_id}


def _audit(result: ProtectedOrderResult, *, redis_client: RedisClient | None) -> None:
    try:
        client = redis_client
        if client is None:
            try:
                client = get_redis_client()
            except Exception:
                client = None
        append_order_audit(
            {
                "order_id": result.order_id or f"FAILED-{int(time.time())}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "strategy_name": "ORDER_GUARD",
                "strike": result.request.symbol,
                "action": result.request.action,
                "quantity": result.request.qty,
                "status": "COMPLETE" if result.success else "REJECTED",
                "execution_latency_ms": result.latency_ms,
                "limit_price": result.limit_price,
                "ltp": result.request.ltp,
                "order_type": "LIMIT",
                "note": result.error,
            },
            redis_client=client,
        )
    except Exception:
        logger.debug("Failed to audit protected order", exc_info=True)
