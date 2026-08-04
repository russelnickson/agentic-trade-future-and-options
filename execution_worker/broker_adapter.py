"""Broker adapter — Dhan / Zerodha (live) or paper trading via env config.

Env
---
``BROKER_NAME``          ``dhan`` | ``zerodha`` (fallback: ``TRADE_BROKER``)
``BROKER_API_KEY``       API key / client id (fallback: broker-specific vars)
``BROKER_ACCESS_TOKEN``  Session access token
``PAPER_TRADING``        ``true`` → simulate fills with slippage logging (no live calls)
``PAPER_SLIPPAGE_BPS``   Slippage in basis points (default 5 = 0.05%)
"""

from __future__ import annotations

import logging
import os
import random
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger(__name__)

BrokerName = Literal["dhan", "zerodha"]


class BrokerAdapterError(RuntimeError):
    """Raised when broker config or API call fails."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def paper_trading_enabled() -> bool:
    try:
        from config.runtime_mode import paper_trading_enabled as _mode_paper

        return bool(_mode_paper())
    except Exception:
        return _env_bool("PAPER_TRADING", default=False)


def resolve_broker_name() -> BrokerName:
    raw = (
        os.getenv("BROKER_NAME")
        or os.getenv("TRADE_BROKER")
        or "dhan"
    ).strip().lower()
    if raw not in {"dhan", "zerodha"}:
        raise BrokerAdapterError(
            f"Unsupported BROKER_NAME={raw!r}; expected 'dhan' or 'zerodha'"
        )
    return raw  # type: ignore[return-value]


def _credentials(broker: BrokerName) -> tuple[str, str]:
    """Return (api_key, access_token) from env."""
    api_key = (os.getenv("BROKER_API_KEY") or "").strip()
    access = (os.getenv("BROKER_ACCESS_TOKEN") or "").strip()

    if broker == "dhan":
        api_key = api_key or (os.getenv("DHAN_CLIENT_ID") or "").strip()
        access = access or (os.getenv("DHAN_ACCESS_TOKEN") or "").strip()
    else:
        api_key = api_key or (os.getenv("ZERODHA_API_KEY") or "").strip()
        access = access or (os.getenv("ZERODHA_ACCESS_TOKEN") or "").strip()

    if not api_key or not access:
        raise BrokerAdapterError(
            f"Missing credentials for {broker}: set BROKER_API_KEY / "
            "BROKER_ACCESS_TOKEN (or broker-specific DHAN_* / ZERODHA_* vars)"
        )
    return api_key, access


def _normalize_txn(transaction_type: str) -> str:
    text = str(transaction_type).strip().upper()
    if text in {"BUY", "B", "LONG"}:
        return "BUY"
    if text in {"SELL", "S", "SHORT"}:
        return "SELL"
    raise BrokerAdapterError(f"Unsupported transaction_type: {transaction_type!r}")


def _normalize_order_type(order_type: str) -> str:
    text = str(order_type).strip().upper()
    mapping = {
        "MARKET": "MARKET",
        "MKT": "MARKET",
        "LIMIT": "LIMIT",
        "L": "LIMIT",
        "SL": "SL",
        "SL-M": "SL-M",
        "SLM": "SL-M",
    }
    out = mapping.get(text)
    if not out:
        raise BrokerAdapterError(f"Unsupported order_type: {order_type!r}")
    return out


def _slippage_bps() -> float:
    try:
        return max(0.0, float(os.getenv("PAPER_SLIPPAGE_BPS") or "5"))
    except ValueError:
        return 5.0


def _apply_slippage(price: float, transaction_type: str, order_type: str) -> tuple[float, float]:
    """Return (fill_price, slippage_abs) with realistic adverse slip."""
    base = float(price) if price and price > 0 else 100.0
    bps = _slippage_bps()
    # Small random jitter ±20% of configured bps
    jitter = random.uniform(0.8, 1.2)
    slip_frac = (bps * jitter) / 10_000.0
    side = _normalize_txn(transaction_type)
    if order_type.upper() == "MARKET" or base <= 0:
        # Adverse: BUY pays more, SELL receives less
        fill = base * (1.0 + slip_frac) if side == "BUY" else base * (1.0 - slip_frac)
    else:
        # LIMIT: rare partial adverse vs limit (simulate 30% of bps)
        fill = base * (1.0 + slip_frac * 0.3) if side == "BUY" else base * (1.0 - slip_frac * 0.3)
    fill = round(max(0.05, fill), 2)
    return fill, round(abs(fill - base), 4)


# ---------------------------------------------------------------------------
# Paper book
# ---------------------------------------------------------------------------

_paper_orders: list[dict[str, Any]] = []
_paper_positions: dict[str, dict[str, Any]] = {}


def _paper_place(
    *,
    symbol: str,
    exchange: str,
    qty: int,
    transaction_type: str,
    order_type: str,
    price: float,
) -> dict[str, Any]:
    side = _normalize_txn(transaction_type)
    otype = _normalize_order_type(order_type)
    fill_price, slip = _apply_slippage(price, side, otype)
    order_id = f"PAPER-{uuid.uuid4().hex[:12].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    order = {
        "order_id": order_id,
        "symbol": symbol.strip().upper(),
        "exchange": exchange,
        "qty": int(qty),
        "transaction_type": side,
        "order_type": otype,
        "price": float(price),
        "fill_price": fill_price,
        "slippage": slip,
        "status": "FILLED",
        "broker": "paper",
        "paper_trading": True,
        "created_at": now,
    }
    logger.info(
        "PAPER FILL %s %s qty=%s limit=%.2f fill=%.2f slip=%.4f (%s bps cfg)",
        side,
        symbol,
        qty,
        price,
        fill_price,
        slip,
        _slippage_bps(),
    )
    _paper_orders.append(order)

    key = f"{exchange}:{symbol.strip().upper()}"
    signed = qty if side == "BUY" else -qty
    pos = _paper_positions.get(key)
    if pos is None:
        _paper_positions[key] = {
            "symbol": symbol.strip().upper(),
            "exchange": exchange,
            "qty": signed,
            "avg_price": fill_price,
            "updated_at": now,
        }
    else:
        old_qty = int(pos["qty"])
        new_qty = old_qty + signed
        if new_qty == 0:
            del _paper_positions[key]
        else:
            # Running average on same-direction adds
            if (old_qty > 0 and signed > 0) or (old_qty < 0 and signed < 0):
                prev_avg = float(pos["avg_price"])
                pos["avg_price"] = round(
                    (abs(old_qty) * prev_avg + abs(signed) * fill_price) / abs(new_qty),
                    4,
                )
            pos["qty"] = new_qty
            pos["updated_at"] = now
    return order


def _paper_positions_list() -> list[dict[str, Any]]:
    return list(_paper_positions.values())


def _paper_cancel(order_id: str) -> dict[str, Any]:
    for order in _paper_orders:
        if order.get("order_id") == order_id:
            if order.get("status") == "CANCELLED":
                return {"ok": True, "order_id": order_id, "status": "CANCELLED", "broker": "paper"}
            order["status"] = "CANCELLED"
            order["cancelled_at"] = datetime.now(timezone.utc).isoformat()
            logger.info("PAPER CANCEL order_id=%s", order_id)
            return {"ok": True, "order_id": order_id, "status": "CANCELLED", "broker": "paper"}
    return {"ok": False, "order_id": order_id, "status": "NOT_FOUND", "broker": "paper"}


# ---------------------------------------------------------------------------
# Live brokers
# ---------------------------------------------------------------------------

def _place_dhan(
    *,
    symbol: str,
    exchange: str,
    qty: int,
    transaction_type: str,
    order_type: str,
    price: float,
) -> dict[str, Any]:
    from dhanhq import DhanContext, Order

    api_key, access = _credentials("dhan")
    side = _normalize_txn(transaction_type)
    otype = _normalize_order_type(order_type)
    # Dhan expects numeric security_id; allow numeric symbol
    security_id = str(symbol).strip()
    if not security_id.isdigit():
        raise BrokerAdapterError(
            "Dhan place_order requires numeric security_id as symbol "
            f"(got {symbol!r})"
        )
    segment = exchange.strip().upper() or "NSE_FNO"
    if segment in {"NFO", "NSE"}:
        segment = "NSE_FNO"

    dhan_order_type = {
        "MARKET": "MARKET",
        "LIMIT": "LIMIT",
        "SL": "STOP_LOSS",
        "SL-M": "STOP_LOSS_MARKET",
    }.get(otype, "LIMIT")

    ctx = DhanContext(api_key, access)
    response = Order(ctx).place_order(
        security_id=security_id,
        exchange_segment=segment,
        transaction_type=side,
        quantity=int(qty),
        order_type=dhan_order_type,
        product_type="INTRADAY",
        price=float(price) if otype != "MARKET" else 0,
        trigger_price=0,
        tag="EXEC_WORKER",
    )
    order_id = None
    if isinstance(response, dict):
        data = response.get("data") if isinstance(response.get("data"), dict) else response
        order_id = (
            (data or {}).get("orderId")
            or (data or {}).get("order_id")
            or response.get("orderId")
        )
        if str(response.get("status") or "").lower() == "failure":
            raise BrokerAdapterError(f"Dhan order rejected: {response}")
    return {
        "order_id": str(order_id) if order_id else None,
        "symbol": security_id,
        "exchange": segment,
        "qty": int(qty),
        "transaction_type": side,
        "order_type": otype,
        "price": float(price),
        "status": "SUBMITTED",
        "broker": "dhan",
        "paper_trading": False,
        "broker_response": response,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _place_zerodha(
    *,
    symbol: str,
    exchange: str,
    qty: int,
    transaction_type: str,
    order_type: str,
    price: float,
) -> dict[str, Any]:
    from kiteconnect import KiteConnect

    api_key, access = _credentials("zerodha")
    side = _normalize_txn(transaction_type)
    otype = _normalize_order_type(order_type)
    exch = exchange.strip().upper() or "NFO"
    if exch in {"NSE_FNO", "NSE-FNO"}:
        exch = "NFO"

    kite_order_type = {
        "MARKET": "MARKET",
        "LIMIT": "LIMIT",
        "SL": "SL",
        "SL-M": "SL-M",
    }.get(otype, "LIMIT")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access)
    order_id = kite.place_order(
        variety="regular",
        exchange=exch,
        tradingsymbol=str(symbol).strip().upper(),
        transaction_type=side,
        quantity=int(qty),
        product="MIS",
        order_type=kite_order_type,
        price=float(price) if otype not in {"MARKET", "SL-M"} else 0.0,
        validity="DAY",
        tag="EXECW",
    )
    return {
        "order_id": str(order_id),
        "symbol": str(symbol).strip().upper(),
        "exchange": exch,
        "qty": int(qty),
        "transaction_type": side,
        "order_type": otype,
        "price": float(price),
        "status": "SUBMITTED",
        "broker": "zerodha",
        "paper_trading": False,
        "broker_response": {"order_id": order_id},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _positions_dhan() -> list[dict[str, Any]]:
    from dhanhq import DhanContext, Portfolio

    api_key, access = _credentials("dhan")
    ctx = DhanContext(api_key, access)
    payload = Portfolio(ctx).get_positions()
    rows: list[dict[str, Any]] = []
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        # Dhan sometimes nests open/closed
        candidates = []
        for key in ("open", "closed", "positions"):
            if isinstance(data.get(key), list):
                candidates.extend(data[key])
        if not candidates and isinstance(data, list):
            candidates = data
        items = candidates or ([data] if data else [])
    elif isinstance(data, list):
        items = data
    else:
        items = []

    for pos in items:
        if not isinstance(pos, dict):
            continue
        qty = int(pos.get("netQty") or 0)
        if qty == 0:
            continue
        rows.append(
            {
                "symbol": str(pos.get("tradingSymbol") or pos.get("securityId") or ""),
                "exchange": str(pos.get("exchangeSegment") or ""),
                "qty": qty,
                "avg_price": float(pos.get("costPrice") or pos.get("buyAvg") or 0),
                "pnl": float(pos.get("unrealizedProfit") or 0),
                "broker": "dhan",
                "raw": pos,
            }
        )
    return rows


def _positions_zerodha() -> list[dict[str, Any]]:
    from kiteconnect import KiteConnect

    api_key, access = _credentials("zerodha")
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access)
    payload = kite.positions()
    net = payload.get("net") if isinstance(payload, dict) else []
    rows: list[dict[str, Any]] = []
    for pos in net or []:
        qty = int(pos.get("quantity") or 0)
        if qty == 0:
            continue
        rows.append(
            {
                "symbol": str(pos.get("tradingsymbol") or ""),
                "exchange": str(pos.get("exchange") or ""),
                "qty": qty,
                "avg_price": float(pos.get("average_price") or 0),
                "pnl": float(pos.get("pnl") or 0),
                "broker": "zerodha",
                "raw": pos,
            }
        )
    return rows


def _cancel_dhan(order_id: str) -> dict[str, Any]:
    from dhanhq import DhanContext, Order

    api_key, access = _credentials("dhan")
    ctx = DhanContext(api_key, access)
    response = Order(ctx).cancel_order(order_id=str(order_id))
    return {
        "ok": True,
        "order_id": str(order_id),
        "status": "CANCELLED",
        "broker": "dhan",
        "broker_response": response,
    }


def _cancel_zerodha(order_id: str) -> dict[str, Any]:
    from kiteconnect import KiteConnect

    api_key, access = _credentials("zerodha")
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access)
    response = kite.cancel_order(variety="regular", order_id=str(order_id))
    return {
        "ok": True,
        "order_id": str(order_id),
        "status": "CANCELLED",
        "broker": "zerodha",
        "broker_response": response,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def place_order(
    symbol: str,
    exchange: str,
    qty: int,
    transaction_type: str,
    order_type: str,
    price: float = 0.0,
) -> dict[str, Any]:
    """Place an order via paper book or configured live broker."""
    if int(qty) <= 0:
        raise BrokerAdapterError(f"qty must be positive, got {qty}")

    if paper_trading_enabled():
        return _paper_place(
            symbol=symbol,
            exchange=exchange or "NFO",
            qty=int(qty),
            transaction_type=transaction_type,
            order_type=order_type,
            price=float(price),
        )

    broker = resolve_broker_name()
    if broker == "dhan":
        return _place_dhan(
            symbol=symbol,
            exchange=exchange,
            qty=int(qty),
            transaction_type=transaction_type,
            order_type=order_type,
            price=float(price),
        )
    return _place_zerodha(
        symbol=symbol,
        exchange=exchange,
        qty=int(qty),
        transaction_type=transaction_type,
        order_type=order_type,
        price=float(price),
    )


def get_positions() -> list[dict[str, Any]]:
    """Return open positions (paper book or live broker)."""
    if paper_trading_enabled():
        return _paper_positions_list()
    broker = resolve_broker_name()
    if broker == "dhan":
        return _positions_dhan()
    return _positions_zerodha()


def cancel_order(order_id: str) -> dict[str, Any]:
    """Cancel an order by id."""
    if not str(order_id).strip():
        raise BrokerAdapterError("order_id is required")
    if paper_trading_enabled():
        return _paper_cancel(str(order_id).strip())
    broker = resolve_broker_name()
    if broker == "dhan":
        return _cancel_dhan(str(order_id).strip())
    return _cancel_zerodha(str(order_id).strip())


def square_off_all() -> dict[str, Any]:
    """Close all open positions immediately (paper or live)."""
    positions = get_positions()
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    for pos in positions:
        symbol = str(pos.get("symbol") or "").strip()
        exchange = str(pos.get("exchange") or "NFO")
        qty = int(pos.get("qty") or 0)
        if not symbol or qty == 0:
            continue
        # Flatten: opposite side of net qty
        side = "SELL" if qty > 0 else "BUY"
        abs_qty = abs(qty)
        try:
            order = place_order(
                symbol=symbol,
                exchange=exchange or "NFO",
                qty=abs_qty,
                transaction_type=side,
                order_type="MARKET",
                price=0.0,
            )
            results.append({"symbol": symbol, "side": side, "qty": abs_qty, "order": order})
        except BrokerAdapterError as exc:
            errors.append(f"{symbol}: {exc}")
            logger.exception("square_off_all failed for %s", symbol)

    return {
        "ok": len(errors) == 0,
        "closed": len(results),
        "results": results,
        "errors": errors,
        "paper_trading": paper_trading_enabled(),
        "asof": datetime.now(timezone.utc).isoformat(),
    }


def adapter_status() -> dict[str, Any]:
    """Lightweight status for /health enrichment."""
    try:
        name = resolve_broker_name()
    except BrokerAdapterError:
        name = "unknown"
    return {
        "broker_name": name,
        "paper_trading": paper_trading_enabled(),
    }
