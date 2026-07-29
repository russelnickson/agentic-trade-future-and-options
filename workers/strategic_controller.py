"""PM2 strategic_controller — LangGraph slow path (regime / sentiment / risk)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.strategic_controller.runner import run_forever

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [strategic] %(message)s",
)


def main() -> int:
    symbol = (os.getenv("TRADE_SYMBOL") or "NIFTY").strip().upper()
    run_forever(symbol)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
