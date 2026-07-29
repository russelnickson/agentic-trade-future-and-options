"""Redis connection pool for hot tick and option-chain state."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import redis

from config.settings import get_settings

TICK_KEY = "tick:{token}"
OPTION_CHAIN_KEY = "option_chain:{symbol}"


class RedisClient:
    """Thin wrapper over a redis-py connection pool for F&O hot state."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        *,
        password: str | None = None,
        max_connections: int = 50,
    ) -> None:
        self._pool = redis.ConnectionPool(
            host=host,
            port=port,
            db=db,
            password=password,
            max_connections=max_connections,
            decode_responses=True,
        )
        self._client = redis.Redis(connection_pool=self._pool)

    @classmethod
    def from_settings(cls) -> RedisClient:
        settings = get_settings()
        host, port = settings.resolved_redis_host_port()
        return cls(host=host, port=port)

    @property
    def client(self) -> redis.Redis:
        return self._client

    def set_latest_tick(self, token: int | str, tick_dict: dict[str, Any]) -> None:
        """Overwrite the latest tick payload for a security token."""
        key = TICK_KEY.format(token=token)
        self._client.set(key, json.dumps(tick_dict, default=str))

    def get_latest_tick(self, token: int | str) -> dict[str, Any] | None:
        """Return the latest tick for a token, or None if missing."""
        raw = self._client.get(TICK_KEY.format(token=token))
        if raw is None:
            return None
        return json.loads(raw)

    def update_option_chain_state(self, symbol: str, data: dict[str, Any]) -> None:
        """Replace the hot option-chain snapshot for an underlying symbol."""
        key = OPTION_CHAIN_KEY.format(symbol=symbol.strip().upper())
        self._client.set(key, json.dumps(data, default=str))

    def get_option_chain_state(self, symbol: str) -> dict[str, Any] | None:
        """Return the stored option-chain snapshot, or None if missing."""
        raw = self._client.get(OPTION_CHAIN_KEY.format(symbol=symbol.strip().upper()))
        if raw is None:
            return None
        return json.loads(raw)

    def ping(self) -> bool:
        return bool(self._client.ping())

    def close(self) -> None:
        self._client.close()
        self._pool.disconnect()


@lru_cache
def get_redis_client() -> RedisClient:
    return RedisClient.from_settings()
