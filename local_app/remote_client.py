"""Remote client — dispatch order signals to the AWS EC2 execution worker.

Uses a persistent ``httpx.Client`` with connection pooling, header-based auth,
hard 3 s timeout, and exponential-backoff retry (max 3) on 5xx / network errors.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()  # reads local .env / .secrets.env

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BACKOFF_BASE = 0.4       # seconds; 0.4 → 0.8 → 1.6
_TIMEOUT = httpx.Timeout(3.0, connect=3.0)
_RETRYABLE_STATUS = frozenset(range(500, 600))


def _base_url() -> str:
    ip = (os.getenv("EC2_ELASTIC_IP") or os.getenv("EC2_HOST") or "").strip()
    if not ip:
        raise RuntimeError(
            "EC2_ELASTIC_IP (or EC2_HOST) not set — cannot reach execution worker"
        )
    port = (os.getenv("EC2_WORKER_PORT") or "8000").strip()
    return f"http://{ip}:{port}"


def _auth_token() -> str:
    token = (os.getenv("INTERNAL_AUTH_SECRET") or "").strip()
    if not token:
        raise RuntimeError("INTERNAL_AUTH_SECRET not set in local .env")
    return token


class RemoteClient:
    """Persistent-connection client to the EC2 FastAPI execution worker."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        auth_token: str | None = None,
        max_retries: int = _MAX_RETRIES,
        timeout: httpx.Timeout = _TIMEOUT,
    ) -> None:
        self._base_url = (base_url or _base_url()).rstrip("/")
        self._token = auth_token or _auth_token()
        self._max_retries = max_retries
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            headers={
                "X-Auth-Token": self._token,
                "User-Agent": "fno-local-client/1.0",
            },
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=5,
            ),
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RemoteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = self._http.request(method, path, json=json)
                if resp.status_code not in _RETRYABLE_STATUS:
                    return resp
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout,
                    httpx.PoolTimeout, httpx.ConnectTimeout, httpx.NetworkError) as exc:
                last_exc = exc

            if attempt < self._max_retries:
                delay = _BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "Retry %d/%d after %.2fs — %s %s: %s",
                    attempt, self._max_retries, delay, method, path, last_exc,
                )
                time.sleep(delay)

        raise httpx.TransportError(
            f"Failed after {self._max_retries} retries: {last_exc}"
        ) from last_exc

    def check_health(self) -> bool:
        """Return True if the EC2 worker is reachable and healthy."""
        try:
            resp = self._http.get("/health", timeout=_TIMEOUT)
            return resp.status_code == 200 and resp.json().get("status") == "ok"
        except Exception:
            return False

    def send_order(
        self,
        symbol: str,
        action: str,
        qty: int,
        order_type: str = "LIMIT",
    ) -> dict[str, Any]:
        """Place an order on the remote execution worker. Returns the JSON response."""
        resp = self._request(
            "POST",
            "/order/place",
            json={
                "symbol": symbol,
                "action": action,
                "qty": qty,
                "order_type": order_type,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def get_positions(self) -> dict[str, Any]:
        """Fetch current active positions from the remote worker."""
        resp = self._request("GET", "/order/positions")
        resp.raise_for_status()
        return resp.json()


_default_client: RemoteClient | None = None


def get_client() -> RemoteClient:
    """Module-level singleton (lazy init)."""
    global _default_client
    if _default_client is None:
        _default_client = RemoteClient()
    return _default_client


def send_order(
    symbol: str,
    action: str,
    qty: int,
    order_type: str = "LIMIT",
) -> dict[str, Any]:
    """Convenience — send via the default client."""
    return get_client().send_order(symbol, action, qty, order_type)


def check_health() -> bool:
    """Convenience — health check via the default client."""
    return get_client().check_health()
