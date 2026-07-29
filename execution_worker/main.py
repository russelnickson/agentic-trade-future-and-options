"""FastAPI execution worker — auth-gated order API for EC2 / PM2."""

from __future__ import annotations

import os
import signal
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# In-memory mock broker (swap for Dhan/Zerodha clients later)
# ---------------------------------------------------------------------------

Action = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT", "SL", "SL-M"]


class PlaceOrderRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=64)
    action: Action
    qty: int = Field(..., gt=0)
    order_type: OrderType = "LIMIT"

    @field_validator("symbol")
    @classmethod
    def _strip_symbol(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned:
            raise ValueError("symbol is required")
        return cleaned


class MockBroker:
    """Thread-hostile but fine for single-worker uvicorn under PM2."""

    def __init__(self) -> None:
        self._orders: list[dict[str, Any]] = []
        self._positions: dict[str, dict[str, Any]] = {}

    def place(self, payload: PlaceOrderRequest) -> dict[str, Any]:
        order_id = f"MOCK-{uuid.uuid4().hex[:12].upper()}"
        now = datetime.now(timezone.utc).isoformat()
        order = {
            "order_id": order_id,
            "symbol": payload.symbol,
            "action": payload.action,
            "qty": payload.qty,
            "order_type": payload.order_type,
            "status": "FILLED",
            "broker": "mock",
            "created_at": now,
        }
        self._orders.append(order)

        key = payload.symbol
        pos = self._positions.get(key)
        signed = payload.qty if payload.action == "BUY" else -payload.qty
        if pos is None:
            self._positions[key] = {
                "symbol": key,
                "qty": signed,
                "avg_price": 0.0,
                "updated_at": now,
            }
        else:
            new_qty = int(pos["qty"]) + signed
            if new_qty == 0:
                del self._positions[key]
            else:
                pos["qty"] = new_qty
                pos["updated_at"] = now

        return order

    def positions(self) -> list[dict[str, Any]]:
        return list(self._positions.values())


broker = MockBroker()
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
            # Not main thread / unsupported — lifespan still closes the pool.
            pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.shutting_down = False
    app.state.last_signal = None
    app.state.started_at = datetime.now(timezone.utc).isoformat()

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
    version="1.0.0",
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
    }


@app.post("/order/place", dependencies=[Depends(require_auth_token)])
async def place_order(body: PlaceOrderRequest) -> dict[str, Any]:
    order = broker.place(body)
    return {"ok": True, "order": order}


@app.get("/order/positions", dependencies=[Depends(require_auth_token)])
async def get_positions() -> dict[str, Any]:
    rows = broker.positions()
    return {"ok": True, "count": len(rows), "positions": rows}
