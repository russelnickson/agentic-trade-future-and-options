#!/usr/bin/env python3
"""
Preflight readiness check — five launch pillars with GREEN/RED console output.

Pillars:
  1. Broker REST API connected
  2. WebSocket tick feed active (short smoke connect)
  3. Redis hot memory reachable
  4. TimescaleDB writable
  5. Clock NTP drift < 50ms

Exit codes:
  0 — all GREEN
  1 — one or more RED
  2 — configuration / bootstrap failure
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")
load_dotenv(_ROOT / ".env.production", override=False)

GREEN = "\033[92m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"
DIM = "\033[2m"

MAX_CLOCK_DRIFT_MS = float(os.getenv("TRADE_MAX_CLOCK_DRIFT_MS", "50"))
WS_SMOKE_SEC = float(os.getenv("TRADE_PREFLIGHT_WS_SEC", "8"))


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _broker_name() -> str:
    return os.getenv("TRADE_BROKER", os.getenv("trade_broker", "dhan")).strip().lower()


def _env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def check_broker_rest() -> CheckResult:
    name = "1. Broker REST API connected"
    broker = _broker_name()
    try:
        if broker == "zerodha":
            from kiteconnect import KiteConnect

            api_key = _env("ZERODHA_API_KEY")
            access = _env("ZERODHA_ACCESS_TOKEN")
            if not api_key:
                return CheckResult(name, False, "ZERODHA_API_KEY missing")
            kite = KiteConnect(api_key=api_key)
            if access:
                kite.set_access_token(access)
            else:
                # Headless login when only password/TOTP are configured.
                from tests.test_auth import _has_zerodha_login_creds, zerodha_headless_login

                if not _has_zerodha_login_creds():
                    return CheckResult(
                        name,
                        False,
                        "Need ZERODHA_ACCESS_TOKEN or full login + TOTP secrets",
                    )
                kite, _ = zerodha_headless_login()
            profile = kite.profile()
            uid = profile.get("user_id") or profile.get("email") or "?"
            return CheckResult(name, True, f"Zerodha profile OK (user_id={uid})")

        # Default: Dhan
        from dhanhq import DhanLogin

        client_id = _env("DHAN_CLIENT_ID")
        token = _env("DHAN_ACCESS_TOKEN")
        if not client_id or not token:
            return CheckResult(name, False, "DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN missing")
        profile = DhanLogin(client_id).user_profile(token)
        if not isinstance(profile, dict):
            return CheckResult(name, False, f"Unexpected profile: {profile!r}")
        data = profile.get("data") if isinstance(profile.get("data"), dict) else {}
        identity = (
            profile.get("dhanClientId")
            or profile.get("clientId")
            or profile.get("client_id")
            or data.get("dhanClientId")
        )
        return CheckResult(
            name,
            True,
            f"Dhan profile OK (client={identity or 'ok'}, keys={len(profile)})",
        )
    except Exception as exc:
        return CheckResult(name, False, f"{type(exc).__name__}: {exc}")


def check_websocket_feed() -> CheckResult:
    name = "2. WebSocket tick feed active"
    broker = _broker_name()
    raw = _env("TRADE_TOKENS", "TRADE_PREFLIGHT_TOKENS")
    if not raw:
        return CheckResult(
            name,
            False,
            "Set TRADE_TOKENS or TRADE_PREFLIGHT_TOKENS for WS smoke test",
        )
    try:
        tokens = [int(t.strip()) for t in raw.split(",") if t.strip()]
        if not tokens:
            return CheckResult(name, False, "Token list empty")

        from ingestion.tick_listener import TickListener

        listener = TickListener(tokens[:5], broker=broker, mode="ltp")  # type: ignore[arg-type]
        got_tick = {"ok": False}
        original_enqueue = listener._enqueue_tick

        def _mark(tick: dict) -> None:
            got_tick["ok"] = True
            original_enqueue(tick)

        listener._enqueue_tick = _mark  # type: ignore[method-assign]
        started = time.monotonic()
        listener.start()
        deadline = started + WS_SMOKE_SEC
        feed_stable = False
        try:
            while time.monotonic() < deadline:
                if got_tick["ok"]:
                    break
                if (time.monotonic() - started) < 2.0:
                    time.sleep(0.2)
                    continue
                pub = listener._publisher_thread
                pub_ok = pub is not None and pub.is_alive()
                if broker == "dhan":
                    feed = listener._feed_thread
                    if pub_ok and feed is not None and feed.is_alive():
                        feed_stable = True
                        break
                else:
                    # Zerodha uses KiteTicker threaded connect (no _feed_thread).
                    kite = listener._kite
                    if pub_ok and kite is not None:
                        connected = getattr(kite, "is_connected", None)
                        if callable(connected):
                            try:
                                if connected():
                                    feed_stable = True
                                    break
                            except Exception:
                                pass
                        else:
                            feed_stable = True
                            break
                time.sleep(0.2)
        finally:
            listener.stop()

        if got_tick["ok"] or feed_stable:
            how = "tick received" if got_tick["ok"] else "session/feed stable"
            detail = (
                f"{broker} WS smoke OK ({how}; {len(tokens[:5])} token(s); "
                f"window={WS_SMOKE_SEC:.0f}s)"
            )
            return CheckResult(name, True, detail)
        return CheckResult(
            name,
            False,
            f"No WS activity within {WS_SMOKE_SEC:.0f}s — check tokens/session",
        )
    except Exception as exc:
        return CheckResult(name, False, f"{type(exc).__name__}: {exc}")


def check_redis() -> CheckResult:
    name = "3. Redis hot memory reachable"
    try:
        from database.redis_client import RedisClient

        from config.settings import get_settings

        client = RedisClient.from_settings()
        if not client.ping():
            return CheckResult(name, False, "PING failed")
        # Round-trip probe key (short TTL).
        probe = f"preflight:probe:{int(time.time())}"
        client.client.set(probe, "1", ex=30)
        val = client.client.get(probe)
        client.client.delete(probe)
        if val is None:
            return CheckResult(name, False, "SET/GET probe failed")
        host, port = get_settings().resolved_redis_host_port()
        return CheckResult(name, True, f"PING/PONG + write OK ({host}:{port})")
    except Exception as exc:
        return CheckResult(name, False, f"{type(exc).__name__}: {exc}")


def check_timescaledb() -> CheckResult:
    name = "4. TimescaleDB writable"
    try:
        import psycopg2

        from config.settings import get_settings

        settings = get_settings()
        conn = psycopg2.connect(settings.database_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS preflight_write_probe (
                        id BIGSERIAL PRIMARY KEY,
                        checked_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cur.execute(
                    "INSERT INTO preflight_write_probe (checked_at) VALUES (%s) RETURNING id",
                    (datetime.now(timezone.utc),),
                )
                row_id = cur.fetchone()[0]
                cur.execute("DELETE FROM preflight_write_probe WHERE id = %s", (row_id,))
            return CheckResult(name, True, f"INSERT/DELETE OK (id={row_id})")
        finally:
            conn.close()
    except Exception as exc:
        return CheckResult(name, False, f"{type(exc).__name__}: {exc}")


def check_clock_ntp() -> CheckResult:
    name = f"5. Clock NTP drift < {MAX_CLOCK_DRIFT_MS:.0f}ms"
    try:
        from services.clock_sync import measure_clock_drift

        result = measure_clock_drift(max_drift_ms=MAX_CLOCK_DRIFT_MS)
        abs_drift = abs(result.drift_ms)
        detail = (
            f"drift={result.drift_ms:+.3f}ms vs {result.ntp_host} "
            f"(limit ±{MAX_CLOCK_DRIFT_MS:.0f}ms, IST {result.system_ist.strftime('%H:%M:%S')})"
        )
        return CheckResult(name, result.within_tolerance, detail if result.within_tolerance else f"FAIL {detail}")
    except Exception as exc:
        return CheckResult(name, False, f"{type(exc).__name__}: {exc}")


def _print_checklist(results: list[CheckResult]) -> None:
    width = 72
    print()
    print(f"{BOLD}{'═' * width}{RESET}")
    print(f"{BOLD}  F&O ENGINE — PREFLIGHT READINESS CHECKLIST{RESET}")
    print(f"{DIM}  {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}{RESET}")
    print(f"{BOLD}{'═' * width}{RESET}")
    for r in results:
        badge = f"{GREEN}GREEN{RESET}" if r.ok else f"{RED}RED{RESET}"
        print(f"  [{badge}]  {r.name}")
        print(f"          {DIM}{r.detail}{RESET}")
    print(f"{BOLD}{'─' * width}{RESET}")
    failed = sum(1 for r in results if not r.ok)
    if failed == 0:
        print(f"  {GREEN}{BOLD}READY{RESET}{GREEN} — all 5 pillars GREEN. Safe to launch.{RESET}")
    else:
        print(
            f"  {RED}{BOLD}NOT READY{RESET}{RED} — {failed} pillar(s) RED. "
            f"Do not start the trading cluster.{RESET}"
        )
    print(f"{BOLD}{'═' * width}{RESET}")
    print()


def run_preflight() -> int:
    checks: list[tuple[str, Callable[[], CheckResult]]] = [
        ("broker_rest", check_broker_rest),
        ("websocket", check_websocket_feed),
        ("redis", check_redis),
        ("timescaledb", check_timescaledb),
        ("clock_ntp", check_clock_ntp),
    ]
    results: list[CheckResult] = []
    for _, fn in checks:
        results.append(fn())
    _print_checklist(results)
    return 0 if all(r.ok for r in results) else 1


def main() -> int:
    try:
        return run_preflight()
    except Exception as exc:
        print(f"{RED}Preflight bootstrap failed: {exc}{RESET}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
