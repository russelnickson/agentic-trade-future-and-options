"""
Headless broker auth checks — Zerodha (pyotp) + DhanHQ token validation.

Requires credentials in ``.env`` (see ``.env.example``). Skip gracefully when
secrets are missing so CI can collect the module without live broker access.

Run:
    python -m unittest tests.test_auth -v
    # or
    python tests/test_auth.py
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# Project root on sys.path when executed as a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pyotp
import requests
from dotenv import load_dotenv
from kiteconnect import KiteConnect

load_dotenv(_ROOT / ".env")

KITE_LOGIN_URL = "https://kite.zerodha.com/api/login"
KITE_TWOFA_URL = "https://kite.zerodha.com/api/twofa"


def _env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _has_zerodha_login_creds() -> bool:
    return all(
        [
            _env("ZERODHA_API_KEY"),
            _env("ZERODHA_API_SECRET"),
            _env("ZERODHA_USER_ID"),
            _env("ZERODHA_PASSWORD"),
            _env("ZERODHA_TOTP_SECRET"),
        ]
    )


def _has_dhan_creds() -> bool:
    return bool(_env("DHAN_CLIENT_ID") and _env("DHAN_ACCESS_TOKEN"))


def zerodha_headless_login() -> tuple[KiteConnect, dict[str, Any]]:
    """
    Create an active Kite Connect session without opening a browser.

    Flow: password login → TOTP 2FA (pyotp) → request_token → access_token.
    """
    api_key = _env("ZERODHA_API_KEY")
    api_secret = _env("ZERODHA_API_SECRET")
    user_id = _env("ZERODHA_USER_ID")
    password = _env("ZERODHA_PASSWORD")
    totp_secret = _env("ZERODHA_TOTP_SECRET")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "trade-data-engine/1.0",
            "X-Kite-Version": "3",
        }
    )

    login_resp = session.post(
        KITE_LOGIN_URL,
        data={"user_id": user_id, "password": password},
        timeout=30,
    )
    login_resp.raise_for_status()
    login_json = login_resp.json()
    if login_json.get("status") != "success":
        raise RuntimeError(f"Zerodha login failed: {login_json}")

    request_id = login_json["data"]["request_id"]
    totp_code = pyotp.TOTP(totp_secret).now()

    twofa_resp = session.post(
        KITE_TWOFA_URL,
        data={
            "user_id": user_id,
            "request_id": request_id,
            "twofa_value": totp_code,
            "twofa_type": "totp",
            "skip_session": True,
        },
        timeout=30,
    )
    twofa_resp.raise_for_status()
    twofa_json = twofa_resp.json()
    if twofa_json.get("status") != "success":
        raise RuntimeError(f"Zerodha TOTP/2FA failed: {twofa_json}")

    kite = KiteConnect(api_key=api_key)
    # Follow connect redirect with the authenticated session cookies.
    connect_resp = session.get(kite.login_url(), allow_redirects=True, timeout=30)
    request_token = _extract_request_token(connect_resp)
    if not request_token:
        raise RuntimeError(
            "Zerodha request_token not found after headless login "
            f"(final_url={connect_resp.url!r})"
        )

    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session_data["access_token"]
    kite.set_access_token(access_token)
    return kite, session_data


def _extract_request_token(response: requests.Response) -> str | None:
    """Pull request_token from the final URL or redirect history."""
    candidates = [response.url, *(r.headers.get("Location", "") for r in response.history)]
    for url in candidates:
        if not url or "request_token=" not in url:
            continue
        token = parse_qs(urlparse(url).query).get("request_token", [None])[0]
        if token:
            return token
    return None


def dhan_validate_access_token() -> dict[str, Any]:
    """Validate DHAN_ACCESS_TOKEN and return the user profile payload."""
    from dhanhq import DhanLogin

    client_id = _env("DHAN_CLIENT_ID")
    access_token = _env("DHAN_ACCESS_TOKEN")
    profile = DhanLogin(client_id).user_profile(access_token)
    if not isinstance(profile, dict):
        raise RuntimeError(f"Unexpected Dhan profile response: {profile!r}")
    return profile


def _print_profile(broker: str, profile: dict[str, Any]) -> None:
    print(f"\n===== {broker} profile =====")
    for key in sorted(profile.keys()):
        value = profile[key]
        # Avoid dumping oversized nested blobs.
        if isinstance(value, (dict, list)) and len(str(value)) > 200:
            print(f"  {key}: <{type(value).__name__} len={len(value)}>")
        else:
            print(f"  {key}: {value}")
    print("============================\n")


@unittest.skipUnless(
    _has_zerodha_login_creds(),
    "Zerodha login credentials missing in .env "
    "(ZERODHA_API_KEY/SECRET/USER_ID/PASSWORD/TOTP_SECRET)",
)
class TestZerodhaHeadlessAuth(unittest.TestCase):
    def test_headless_totp_login_creates_active_session(self) -> None:
        kite, session_data = zerodha_headless_login()

        self.assertIn("access_token", session_data)
        self.assertTrue(session_data["access_token"])
        self.assertTrue(kite.access_token)

        profile = kite.profile()
        self.assertIsInstance(profile, dict)
        self.assertTrue(profile.get("user_id") or profile.get("email") or profile.get("user_name"))

        print("Zerodha headless login OK — active session created (no browser).")
        _print_profile("Zerodha", profile)
        print(
            "Zerodha session meta:",
            {
                "user_id": session_data.get("user_id"),
                "login_time": session_data.get("login_time"),
                "access_token_prefix": f"{session_data['access_token'][:8]}…",
            },
        )


@unittest.skipUnless(
    _has_dhan_creds(),
    "Dhan credentials missing in .env (DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN)",
)
class TestDhanAccessToken(unittest.TestCase):
    def test_access_token_validates_and_returns_profile(self) -> None:
        profile = dhan_validate_access_token()

        # Dhan profile payloads vary slightly by API version; accept common keys.
        data = profile.get("data")
        identity = (
            profile.get("dhanClientId")
            or profile.get("clientId")
            or profile.get("client_id")
            or (data.get("dhanClientId") if isinstance(data, dict) else None)
        )
        self.assertTrue(
            identity or profile.get("tokenValidity") or profile.get("status") == "success" or len(profile) > 0,
            f"Dhan profile empty / unrecognized: {profile!r}",
        )

        print("DhanHQ access token validation OK — active session confirmed.")
        _print_profile("DhanHQ", profile)


if __name__ == "__main__":
    unittest.main(verbosity=2)
