"""PM2 greeks_engine worker — ZMQ tick SUB → IV/greeks enrich → Redis hot tick."""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from database.redis_client import RedisClient
from ingestion.zmq_pub import DEFAULT_ENDPOINT
from ingestion.zmq_sub import iter_ticks
from services.greeks_engine import compute_greeks, years_to_expiry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [greeks_engine] %(message)s",
)
logger = logging.getLogger("greeks_engine")


def _extract_ltp(tick: dict) -> float | None:
    for key in ("ltp", "last_price", "last_traded_price", "LTP"):
        if tick.get(key) is not None:
            try:
                return float(tick[key])
            except (TypeError, ValueError):
                return None
    return None


def _extract_token(tick: dict) -> int | None:
    for key in ("instrument_token", "token", "security_id"):
        if tick.get(key) is not None:
            try:
                return int(tick[key])
            except (TypeError, ValueError):
                return None
    return None


def enrich_tick(tick: dict, *, spot: float | None, strike: float | None, opt: str | None, expiry: str | None) -> dict:
    """Best-effort greeks when contract meta is present on the tick or env defaults."""
    out = dict(tick)
    ltp = _extract_ltp(tick)
    if ltp is None or ltp <= 0 or spot is None or strike is None or not opt:
        return out
    try:
        if expiry:
            exp = date.fromisoformat(str(expiry)[:10])
            days = max((exp - date.today()).days, 1)
        else:
            days = 7
        result = compute_greeks(
            spot=spot,
            strike=strike,
            tte=years_to_expiry(days=days),
            option_ltp=ltp,
            option_type=opt,
        )
        if result.iv is not None:
            out["iv"] = result.iv
        if result.delta is not None:
            out["delta"] = result.delta
        if result.theta is not None:
            out["theta"] = result.theta
        if result.gamma is not None:
            out["gamma"] = result.gamma
        if result.vega is not None:
            out["vega"] = result.vega
    except Exception:
        logger.debug("greeks enrich failed", exc_info=True)
    return out


def main() -> int:
    endpoint = os.getenv("TRADE_ZMQ_ENDPOINT", DEFAULT_ENDPOINT)
    # Optional contract context for demos; production ticks should carry meta.
    spot = float(os.getenv("GREEKS_SPOT", "0") or 0) or None
    strike = float(os.getenv("GREEKS_STRIKE", "0") or 0) or None
    opt = os.getenv("GREEKS_OPTION_TYPE", "").strip() or None
    expiry = os.getenv("GREEKS_EXPIRY", "").strip() or None

    redis = RedisClient.from_settings()
    logger.info("Greeks engine listening on %s", endpoint)

    for tick in iter_ticks(endpoint):
        token = _extract_token(tick)
        enriched = enrich_tick(
            tick,
            spot=spot or tick.get("underlying_ltp") or tick.get("spot"),
            strike=strike or tick.get("strike"),
            opt=opt or tick.get("option_type") or tick.get("instrument_type"),
            expiry=expiry or tick.get("expiry"),
        )
        if token is not None:
            try:
                redis.set_latest_tick(token, enriched)
            except Exception:
                logger.exception("Failed writing enriched tick for %s", token)
        # Soft yield for CPU fairness under burst.
        time.sleep(0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
