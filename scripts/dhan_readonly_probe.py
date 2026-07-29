#!/usr/bin/env python3
"""Read-only Dhan API preflight — profile, funds, positions, holdings, history.

Does **not** place, modify, or cancel orders.

Usage (from repo root, with venv):
  python scripts/dhan_readonly_probe.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")
load_dotenv(_ROOT / ".secrets.env", override=True)


def _print(status: str, name: str, detail: str) -> None:
    print(f"{status:4}  {name}: {detail}")


def main() -> int:
    from dhanhq import DhanContext, DhanLogin, Funds, Portfolio

    from config.settings import get_settings
    from dashboard.components.capital import fetch_dhan_capital
    from services.history_downloader import DHAN_HIST_URL, DHAN_INDEX_UNIVERSE, _headers

    import requests

    # Clear settings cache so fresh .secrets.env is picked up
    get_settings.cache_clear()
    settings = get_settings()
    cid = (settings.dhan_client_id or "").strip()
    tok = (settings.dhan_access_token or "").strip()
    if not cid or not tok:
        _print("FAIL", "creds", "DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN missing")
        return 1

    _print("INFO", "creds", f"client_id={cid} token_len={len(tok)}")
    fails = 0

    # Profile
    try:
        profile = DhanLogin(cid).user_profile(tok)
        if isinstance(profile, dict) and str(profile.get("status", "")).lower() == "failure":
            remarks = profile.get("remarks") or {}
            msg = remarks.get("error_message") if isinstance(remarks, dict) else remarks
            _print("FAIL", "profile", str(msg))
            fails += 1
        else:
            data = profile.get("data") if isinstance(profile, dict) else None
            identity = None
            if isinstance(profile, dict):
                identity = profile.get("dhanClientId") or profile.get("clientId")
            if isinstance(data, dict):
                identity = identity or data.get("dhanClientId")
            _print("PASS", "profile", f"id={identity} keys={list(profile)[:8] if isinstance(profile, dict) else type(profile)}")
    except Exception as exc:
        _print("FAIL", "profile", repr(exc))
        fails += 1

    # Funds via capital helper (UI path)
    snap = fetch_dhan_capital()
    if snap.error:
        _print("FAIL", "fund_limits", snap.error)
        fails += 1
    else:
        _print(
            "PASS",
            "fund_limits",
            f"available={snap.available_margin:.2f} utilized={snap.utilized_margin:.2f} "
            f"cash={snap.cash_balance:.2f} collateral={snap.collateral_value:.2f}",
        )

    ctx = DhanContext(cid, tok)

    for label, fn in (
        ("positions", lambda: Portfolio(ctx).get_positions()),
        ("holdings", lambda: Portfolio(ctx).get_holdings()),
    ):
        try:
            payload = fn()
            status = str(payload.get("status", "")).lower() if isinstance(payload, dict) else ""
            if status == "failure":
                remarks = (payload or {}).get("remarks") or {}
                msg = remarks.get("error_message") if isinstance(remarks, dict) else remarks
                _print("FAIL", label, str(msg))
                fails += 1
            else:
                data = payload.get("data", payload) if isinstance(payload, dict) else payload
                n = len(data) if isinstance(data, list) else ("obj" if data else 0)
                _print("PASS", label, f"n={n}")
        except Exception as exc:
            _print("FAIL", label, repr(exc))
            fails += 1

    # Historical (NIFTY last ~10d)
    try:
        meta = DHAN_INDEX_UNIVERSE["NIFTY"]
        to_d = date.today() + timedelta(days=1)
        fr_d = date.today() - timedelta(days=10)
        payload = {
            "securityId": meta["security_id"],
            "exchangeSegment": meta["segment"],
            "instrument": meta["instrument"],
            "expiryCode": 0,
            "oi": False,
            "fromDate": fr_d.isoformat(),
            "toDate": to_d.isoformat(),
        }
        resp = requests.post(DHAN_HIST_URL, headers=_headers(), json=payload, timeout=30)
        body = resp.json() if "json" in (resp.headers.get("content-type") or "") else {}
        if resp.status_code >= 400 or (
            isinstance(body, dict) and str(body.get("status", "")).lower() == "failure"
        ):
            _print("FAIL", "historical", f"http={resp.status_code} body={json.dumps(body)[:240]}")
            fails += 1
        else:
            # Dhan returns parallel arrays open/high/... or nested data
            bars = 0
            if isinstance(body, dict):
                for key in ("close", "c", "timestamp", "open"):
                    if isinstance(body.get(key), list):
                        bars = len(body[key])
                        break
                data = body.get("data")
                if bars == 0 and isinstance(data, dict):
                    for key in ("close", "c", "timestamp", "open"):
                        if isinstance(data.get(key), list):
                            bars = len(data[key])
                            break
            _print("PASS", "historical", f"http={resp.status_code} bars≈{bars}")
    except Exception as exc:
        _print("FAIL", "historical", repr(exc))
        fails += 1

    # Raw fundlimit echo for field mapping debug
    try:
        raw = Funds(ctx).get_fund_limits()
        data = raw.get("data") if isinstance(raw, dict) else None
        if isinstance(data, dict):
            interesting = {
                k: data.get(k)
                for k in (
                    "availabelBalance",
                    "availableBalance",
                    "sodLimit",
                    "withdrawableBalance",
                    "utilizedAmount",
                    "collateralAmount",
                )
                if k in data
            }
            _print("INFO", "fund_fields", json.dumps(interesting, default=str))
    except Exception as exc:
        _print("INFO", "fund_fields", repr(exc))

    print()
    if fails:
        print(f"RESULT: {fails} check(s) failed — fix token/credentials before live trading.")
        return 1
    print("RESULT: all read-only Dhan checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
