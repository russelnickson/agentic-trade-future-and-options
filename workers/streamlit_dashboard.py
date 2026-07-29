"""PM2 streamlit_dashboard entry — Trade Console live desk."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    app = _ROOT / "dashboard" / "Trade_Console.py"
    port = os.getenv("STREAMLIT_SERVER_PORT", "8501")
    address = os.getenv("STREAMLIT_SERVER_ADDRESS", "0.0.0.0")
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
        "--server.port",
        str(port),
        "--server.address",
        address,
        "--browser.gatherUsageStats",
        "false",
    ]
    return subprocess.call(cmd, cwd=str(_ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
