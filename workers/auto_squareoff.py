"""PM2 auto_squareoff — daily-loss circuit breaker + emergency flatten/lock."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.circuit_breaker import start_circuit_breaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [auto_squareoff] %(message)s",
)


def main() -> int:
    start_circuit_breaker().run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
