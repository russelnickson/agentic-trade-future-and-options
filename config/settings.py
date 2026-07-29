from functools import lru_cache
from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    App settings from ``.env`` + ``.secrets.env`` + process env.

    Broker fields are optional so the dashboard can boot while only one
    broker (or neither) is configured yet. Call sites that need live API
    access must check the relevant credentials themselves.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", ".secrets.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Optional until the operator configures the active broker in Settings.
    dhan_client_id: str = ""
    dhan_access_token: str = ""

    zerodha_api_key: str = ""
    zerodha_api_secret: str = ""
    zerodha_user_id: str = ""
    zerodha_password: str = ""
    zerodha_totp_secret: str = ""
    # Optional: set after browser login / daily session refresh.
    zerodha_access_token: str = ""

    redis_url: str | None = None
    redis_host: str = "localhost"
    redis_port: int = 6379
    # Matches docker-compose.yml defaults.
    database_url: str = "postgresql://trade:trade@localhost:5432/trade"

    # Circuit breaker: trip when daily P&L <= -max_daily_loss (INR).
    max_daily_loss: float = 5000.0
    circuit_breaker_poll_sec: float = 5.0
    trade_broker: str = "dhan"

    @field_validator("redis_url", mode="before")
    @classmethod
    def _empty_redis_url_as_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator(
        "dhan_client_id",
        "dhan_access_token",
        "zerodha_api_key",
        "zerodha_api_secret",
        "zerodha_user_id",
        "zerodha_password",
        "zerodha_totp_secret",
        "zerodha_access_token",
        "database_url",
        mode="before",
    )
    @classmethod
    def _none_as_empty_str(cls, value: object) -> object:
        if value is None:
            return ""
        return value

    def resolved_redis_host_port(self) -> tuple[str, int]:
        """Prefer REDIS_URL when set; otherwise REDIS_HOST / REDIS_PORT."""
        if self.redis_url:
            parsed = urlparse(self.redis_url)
            host = parsed.hostname or "localhost"
            port = parsed.port or 6379
            return host, port
        return self.redis_host, self.redis_port


@lru_cache
def get_settings() -> Settings:
    return Settings()
