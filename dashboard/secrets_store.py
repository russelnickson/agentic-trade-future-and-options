"""Local-only broker / infra secrets for the Streamlit settings UI.

Secrets are written to ``.secrets.env`` at the project root (gitignored, mode 0600).
Nothing here is pushed to git, Redis, or remote APIs except live broker validation
calls the operator explicitly triggers.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
SECRETS_PATH = _ROOT / ".secrets.env"

# Only these keys may be written via the settings UI.
ALLOWED_KEYS: tuple[str, ...] = (
    "TRADE_BROKER",
    "DHAN_CLIENT_ID",
    "DHAN_ACCESS_TOKEN",
    "ZERODHA_API_KEY",
    "ZERODHA_API_SECRET",
    "ZERODHA_USER_ID",
    "ZERODHA_PASSWORD",
    "ZERODHA_TOTP_SECRET",
    "ZERODHA_ACCESS_TOKEN",
    "REDIS_URL",
    "DATABASE_URL",
    "MAX_DAILY_LOSS",
    "TRADE_TOKENS",
)

# Keys that must never be echoed back in plaintext in UI defaults beyond password widgets.
SENSITIVE_KEYS = frozenset(
    {
        "DHAN_ACCESS_TOKEN",
        "ZERODHA_API_SECRET",
        "ZERODHA_PASSWORD",
        "ZERODHA_TOTP_SECRET",
        "ZERODHA_ACCESS_TOKEN",
    }
)


def secrets_path() -> Path:
    return SECRETS_PATH


def ensure_secrets_file() -> Path:
    path = secrets_path()
    if not path.exists():
        path.write_text(
            "# Local secrets for the F&O terminal — DO NOT COMMIT\n"
            "# Written by dashboard Settings. Mode 0600.\n",
            encoding="utf-8",
        )
    _harden_permissions(path)
    return path


def _harden_permissions(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        logger.warning("Could not chmod 0600 on %s", path)


def load_local_secrets() -> dict[str, str]:
    """Load merged view: process env ← .env ← .secrets.env (last wins for UI)."""
    ensure_secrets_file()
    # Base .env first (may already be loaded by settings), then override with local secrets.
    env_path = _ROOT / ".env"
    merged: dict[str, str] = {}
    if env_path.is_file():
        for k, v in dotenv_values(env_path).items():
            if k and v is not None:
                merged[k] = v
    for k, v in dotenv_values(secrets_path()).items():
        if k and v is not None:
            merged[k] = v
    # Live process env wins for keys already exported in the shell.
    for key in ALLOWED_KEYS:
        if os.getenv(key):
            merged[key] = os.environ[key]
    return {k: merged.get(k, "") for k in ALLOWED_KEYS}


def apply_secrets_to_environ() -> None:
    """Load gitignored secrets into os.environ and clear settings cache."""
    ensure_secrets_file()
    load_dotenv(_ROOT / ".env", override=False)
    load_dotenv(secrets_path(), override=True)
    try:
        from config.settings import get_settings

        get_settings.cache_clear()
    except Exception:
        logger.debug("get_settings cache clear skipped", exc_info=True)


def save_local_secrets(updates: dict[str, Any]) -> Path:
    """
    Persist allowed keys to ``.secrets.env`` (0600). Empty string clears a key.

    Sensitive values are never logged.
    """
    path = ensure_secrets_file()
    cleaned: dict[str, str] = {}
    for key, value in updates.items():
        if key not in ALLOWED_KEYS:
            continue
        text = "" if value is None else str(value).strip()
        cleaned[key] = text

    # Merge with existing file contents for keys not in this update.
    existing = {k: (v or "") for k, v in dotenv_values(path).items() if k}
    existing.update(cleaned)

    # Atomic rewrite so we never leave a half-written secrets file.
    header = (
        "# Local secrets for the F&O terminal — DO NOT COMMIT OR SYNC\n"
        "# Managed by dashboard → Settings. File mode 0600.\n"
        "# Prefer this over committing .env. Production hosts should use a vault.\n\n"
    )
    lines = [header]
    for key in ALLOWED_KEYS:
        val = existing.get(key, "")
        if val == "":
            continue
        # Escape newlines; dotenv set_key handles quoting — we write carefully.
        safe = val.replace("\n", "").replace("\r", "")
        lines.append(f"{key}={_quote_dotenv(safe)}\n")

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".secrets.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        _harden_permissions(tmp_path)
        tmp_path.replace(path)
        _harden_permissions(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    for key, val in cleaned.items():
        if val:
            os.environ[key] = val
        else:
            os.environ.pop(key, None)

    apply_secrets_to_environ()
    logger.info("Local secrets updated at %s (keys touched=%s)", path.name, sorted(cleaned))
    return path


def _quote_dotenv(value: str) -> str:
    if value == "":
        return '""'
    if any(ch in value for ch in (' ', '#', '"', "'", "=", "$")):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def mask_secret(value: str, *, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "•" * len(value)
    return "•" * (len(value) - visible) + value[-visible:]


def validate_dhan(client_id: str, access_token: str) -> tuple[bool, str]:
    client_id = (client_id or "").strip()
    access_token = (access_token or "").strip()
    if not client_id or not access_token:
        return False, "Client ID and Access Token are required."
    try:
        from dhanhq import DhanLogin

        profile = DhanLogin(client_id).user_profile(access_token)
        if not isinstance(profile, dict):
            return False, f"Unexpected response: {profile!r}"

        status = str(profile.get("status") or "").lower()
        if status == "failure":
            remarks = profile.get("remarks") or {}
            if isinstance(remarks, dict):
                msg = (
                    remarks.get("error_message")
                    or remarks.get("errorMessage")
                    or str(remarks)
                )
                code = remarks.get("error_code") or remarks.get("errorCode") or ""
            else:
                msg = str(remarks or "profile failed")
                code = ""
            detail = f"{msg}" if not code else f"{code}: {msg}"
            return False, f"Dhan REST rejected token — {detail}"

        data = profile.get("data") if isinstance(profile.get("data"), dict) else {}
        identity = (
            profile.get("dhanClientId")
            or profile.get("clientId")
            or profile.get("client_id")
            or data.get("dhanClientId")
        )
        if not identity:
            # Some SDK builds return sparse success payloads; require a non-failure status.
            if status and status not in {"success", "ok"}:
                return False, f"Dhan REST ambiguous profile status={status!r}"
            identity = client_id
        return True, f"Dhan REST OK — client={identity}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def kite_login_url(api_key: str) -> str:
    key = (api_key or "").strip()
    return f"https://kite.zerodha.com/connect/login?v=3&api_key={key}"


def extract_request_token(raw: str) -> str:
    """Accept a bare request_token or a full redirect URL containing it."""
    text = (raw or "").strip()
    if not text:
        return ""
    if "request_token=" in text:
        from urllib.parse import parse_qs, urlparse

        # Handle full URL or query-only fragment.
        if "://" not in text and text.startswith("?"):
            text = "https://local" + text
        elif "://" not in text and "request_token=" in text:
            text = "https://local/?" + text.split("?", 1)[-1]
        parsed = urlparse(text)
        values = parse_qs(parsed.query).get("request_token") or []
        if values:
            return values[0].strip()
    return text.split("&", 1)[0].strip()


def exchange_zerodha_request_token(
    api_key: str,
    api_secret: str,
    request_token: str,
) -> tuple[bool, str, str | None]:
    """
    Exchange a one-time request_token for a daily access_token and save it locally.

    Returns (ok, message, access_token_or_none).
    """
    api_key = (api_key or "").strip()
    api_secret = (api_secret or "").strip()
    request_token = extract_request_token(request_token)
    if not api_key or not api_secret:
        return False, "API Key and API Secret are required.", None
    if not request_token:
        return False, "Paste the request_token from the Zerodha redirect URL.", None
    try:
        from kiteconnect import KiteConnect

        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(request_token, api_secret=api_secret)
        access = str(data.get("access_token") or "")
        if not access:
            return False, f"No access_token in session response: {data!r}", None
        kite.set_access_token(access)
        profile = kite.profile()
        uid = (
            profile.get("user_id")
            if isinstance(profile, dict)
            else None
        ) or "?"
        save_local_secrets(
            {
                "ZERODHA_API_KEY": api_key,
                "ZERODHA_API_SECRET": api_secret,
                "ZERODHA_ACCESS_TOKEN": access,
            }
        )
        return True, f"Access token saved. Logged in as {uid}.", access
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", None


def validate_zerodha(
    api_key: str,
    *,
    access_token: str = "",
    api_secret: str = "",
    user_id: str = "",
    password: str = "",
    totp_secret: str = "",
) -> tuple[bool, str]:
    api_key = (api_key or "").strip()
    access_token = (access_token or "").strip()
    if not api_key:
        return False, "API Key is required."
    try:
        from kiteconnect import KiteConnect

        kite = KiteConnect(api_key=api_key)
        if access_token:
            kite.set_access_token(access_token)
            profile = kite.profile()
        elif all([api_secret, user_id, password, totp_secret]):
            # Temporary env for headless helper, then profile.
            os.environ["ZERODHA_API_KEY"] = api_key
            os.environ["ZERODHA_API_SECRET"] = api_secret
            os.environ["ZERODHA_USER_ID"] = user_id
            os.environ["ZERODHA_PASSWORD"] = password
            os.environ["ZERODHA_TOTP_SECRET"] = totp_secret
            from tests.test_auth import zerodha_headless_login

            kite, _ = zerodha_headless_login()
            profile = kite.profile()
            if kite.access_token:
                # Persist session token into local secrets so WS works without re-login.
                save_local_secrets({"ZERODHA_ACCESS_TOKEN": kite.access_token})
        else:
            return (
                False,
                "No access token yet. Use Settings → Get & save access token "
                "(browser login), or fill Advanced TOTP fields.",
            )

        if not isinstance(profile, dict):
            return False, f"Unexpected profile: {profile!r}"
        uid = profile.get("user_id") or profile.get("email") or "?"
        return True, f"Zerodha REST OK — user_id={uid}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def validate_redis(redis_url: str | None = None) -> tuple[bool, str]:
    try:
        if redis_url:
            os.environ["REDIS_URL"] = redis_url.strip()
            apply_secrets_to_environ()
        from database.redis_client import RedisClient

        client = RedisClient.from_settings()
        if not client.ping():
            return False, "PING failed"
        return True, "Redis PING OK"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
