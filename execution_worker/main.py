"""FastAPI execution worker — auth-gated order API for EC2 / PM2."""

from __future__ import annotations

import os
import signal
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from execution_worker import broker_adapter

Action = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT", "SL", "SL-M"]


class PlaceOrderRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=64)
    action: Action
    qty: int = Field(..., gt=0)
    order_type: OrderType = "LIMIT"
    exchange: str = Field(default="NFO", min_length=1, max_length=32)
    price: float = Field(default=0.0, ge=0.0)

    @field_validator("symbol")
    @classmethod
    def _strip_symbol(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned:
            raise ValueError("symbol is required")
        return cleaned

    @field_validator("exchange")
    @classmethod
    def _strip_exchange(cls, value: str) -> str:
        return value.strip().upper() or "NFO"


http_client: httpx.AsyncClient | None = None


def _expected_token() -> str:
    token = (os.getenv("INTERNAL_AUTH_SECRET") or "").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="INTERNAL_AUTH_SECRET is not configured",
        )
    return token


async def require_auth_token(
    x_auth_token: str | None = Header(default=None, alias="X-Auth-Token"),
) -> None:
    expected = _expected_token()
    if not x_auth_token or x_auth_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "X-Auth-Token"},
        )


def _install_signal_handlers(app: FastAPI) -> None:
    """PM2 / systemd send SIGINT/SIGTERM — mark app shutting down."""

    def _handle(signum: int, _frame: Any) -> None:
        app.state.shutting_down = True
        app.state.last_signal = signal.Signals(signum).name

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.shutting_down = False
    app.state.last_signal = None
    app.state.started_at = datetime.now(timezone.utc).isoformat()
    app.state.broker = broker_adapter.adapter_status()

    global http_client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        headers={"User-Agent": "fno-execution-worker/1.0"},
    )
    app.state.http = http_client
    _install_signal_handlers(app)

    try:
        yield
    finally:
        app.state.shutting_down = True
        client = http_client
        http_client = None
        app.state.http = None
        if client is not None:
            await client.aclose()


app = FastAPI(
    title="F&O Execution Worker",
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Liveness — no auth (ALB / PM2 / uptime checks)."""
    host = request.headers.get("host")
    if not host and request.client is not None:
        host = request.client.host
    static_ip = (os.getenv("EC2_STATIC_IP") or os.getenv("EC2_HOST") or "").strip() or None
    return {
        "status": "shutting_down" if getattr(request.app.state, "shutting_down", False) else "ok",
        "process": "execution_worker",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "started_at": getattr(request.app.state, "started_at", None),
        "static_ip": static_ip,
        "host_header": host,
        "last_signal": getattr(request.app.state, "last_signal", None),
        "http_pool_ready": http_client is not None and not http_client.is_closed,
        "broker": broker_adapter.adapter_status(),
    }


@app.post("/order/place", dependencies=[Depends(require_auth_token)])
async def place_order(body: PlaceOrderRequest) -> dict[str, Any]:
    try:
        order = broker_adapter.place_order(
            symbol=body.symbol,
            exchange=body.exchange,
            qty=body.qty,
            transaction_type=body.action,
            order_type=body.order_type,
            price=body.price,
        )
    except broker_adapter.BrokerAdapterError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"ok": True, "order": order}


@app.get("/order/positions", dependencies=[Depends(require_auth_token)])
async def get_positions() -> dict[str, Any]:
    try:
        rows = broker_adapter.get_positions()
    except broker_adapter.BrokerAdapterError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"ok": True, "count": len(rows), "positions": rows}


@app.post("/order/cancel", dependencies=[Depends(require_auth_token)])
async def cancel_order(payload: dict[str, Any]) -> dict[str, Any]:
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id is required")
    try:
        result = broker_adapter.cancel_order(order_id)
    except broker_adapter.BrokerAdapterError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return result


@app.post("/order/square_off_all", dependencies=[Depends(require_auth_token)])
async def square_off_all() -> dict[str, Any]:
    """Emergency flatten — close all open positions immediately."""
    try:
        return broker_adapter.square_off_all()
    except broker_adapter.BrokerAdapterError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
