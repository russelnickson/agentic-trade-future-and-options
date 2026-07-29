#!/usr/bin/env python3
"""Live E2E ping — local dev → EC2 execution worker (port 8000).

Requires in local ``.env`` / ``.secrets.env``:
  EC2_ELASTIC_IP (or EC2_HOST)
  INTERNAL_AUTH_SECRET

Usage:
  python scripts/e2e_ping.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")
load_dotenv(_ROOT / ".secrets.env")

from local_app.remote_client import RemoteClient  # noqa: E402


def main() -> int:
    ip = (os.getenv("EC2_ELASTIC_IP") or os.getenv("EC2_HOST") or "").strip()
    if not ip:
        print("FAIL: set EC2_ELASTIC_IP (or EC2_HOST) in .env")
        return 1
    if not (os.getenv("INTERNAL_AUTH_SECRET") or "").strip():
        print("FAIL: set INTERNAL_AUTH_SECRET in .env")
        return 1

    print(f"Target: http://{ip}:8000")
    with RemoteClient() as client:
        healthy = client.check_health()
        print(f"Health: {'OK' if healthy else 'FAIL'}")
        if not healthy:
            return 2

        order = client.send_order(
            symbol="E2E_PING_CE",
            action="BUY",
            qty=1,
            order_type="LIMIT",
        )
        print(f"Order:  {order}")

        positions = client.get_positions()
        print(f"Positions count: {positions.get('count')}")
        print("E2E ping succeeded")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
