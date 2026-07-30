"""Runtime desk mode — local paper vs EC2 live Dhan.

Local (developer laptop / localhost Streamlit):
  - PAPER_TRADING forced on (no live order API calls from this host)
  - TRADE_AUTO_EXECUTE defaults off unless explicitly overridden for paper drills

EC2 (ap-south-1 worker / portal):
  - Live Dhan when PAPER_TRADING=false and DHAN_* credentials are set
  - TRADE_AUTO_EXECUTE can stay on for tactical execution
"""

from __future__ import annotations

import os
import socket
from functools import lru_cache
from typing import Literal

DeskRole = Literal["local_paper", "ec2_live"]


def _env_bool(name: str, default: bool | None = None) -> bool | None:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _looks_like_ec2() -> bool:
    """Best-effort EC2 detection without network calls."""
    forced = (os.getenv("DESK_ROLE") or os.getenv("TRADE_RUNTIME") or "").strip().lower()
    if forced in {"ec2", "ec2_live", "live", "aws"}:
        return True
    if forced in {"local", "local_paper", "laptop", "dev"}:
        return False

    # Explicit marker file used on the instance
    if os.path.isfile("/home/ubuntu/app/.ec2_desk"):
        return True

    host = socket.gethostname().lower()
    if host.startswith("ip-") and "compute.internal" in (
        socket.getfqdn().lower() if hasattr(socket, "getfqdn") else ""
    ):
        return True
    # Ubuntu EC2 default user home layout
    if os.path.isdir("/home/ubuntu/app") and os.path.isfile("/sys/hypervisor/uuid"):
        try:
            uuid = open("/sys/hypervisor/uuid", encoding="utf-8").read().strip().lower()
            if uuid.startswith("ec2"):
                return True
        except OSError:
            pass
    return False


@lru_cache(maxsize=1)
def desk_role() -> DeskRole:
    return "ec2_live" if _looks_like_ec2() else "local_paper"


def is_local_paper_desk() -> bool:
    return desk_role() == "local_paper"


def is_ec2_live_desk() -> bool:
    return desk_role() == "ec2_live"


def paper_trading_enabled() -> bool:
    """
    Local always papers unless DESK_ROLE=ec2 (shouldn't happen on laptop).

    On EC2, honour PAPER_TRADING env (default **false** → live Dhan).
    """
    explicit = _env_bool("PAPER_TRADING", default=None)
    if is_local_paper_desk():
        # Localhost / laptop: never live-route orders from this process
        if explicit is False and (os.getenv("ALLOW_LOCAL_LIVE") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            return False
        return True
    # EC2
    if explicit is None:
        return False
    return explicit


def live_dhan_execution() -> bool:
    """True when this host may send real Dhan orders."""
    return is_ec2_live_desk() and not paper_trading_enabled()


def auto_execute_default() -> bool:
    """Tactical auto-execute: on for EC2 live, off for local paper."""
    explicit = _env_bool("TRADE_AUTO_EXECUTE", default=None)
    if explicit is not None:
        return explicit
    return live_dhan_execution()


def mode_banner() -> str:
    if live_dhan_execution():
        return "EC2 LIVE · Dhan API orders"
    if is_ec2_live_desk() and paper_trading_enabled():
        return "EC2 PAPER · simulated fills (PAPER_TRADING=true)"
    return "LOCAL PAPER · no live orders from this host"


def clear_runtime_cache() -> None:
    desk_role.cache_clear()
