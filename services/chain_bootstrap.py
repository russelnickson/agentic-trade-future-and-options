"""Bootstrap Redis option_chain skeletons from TRADE_TOKENS + master CSV.

Without this, ticks land in ``tick:{id}`` but never assemble into
``option_chain:NIFTY`` — Trade Console then reports SKIP (no live chain).
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from database.chain_cache import ChainCache
from database.redis_client import RedisClient
from services.strike_selector import ActiveStrikeTokens

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"


def _parse_tokens(raw: str | None = None) -> list[int]:
    text = (raw if raw is not None else os.getenv("TRADE_TOKENS", "")).strip()
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return sorted(set(out))


def _load_master(broker: str | None = None) -> pd.DataFrame:
    broker = (broker or os.getenv("TRADE_BROKER", "dhan")).lower()
    if broker == "dhan":
        path = DATA_DIR / "dhan_nse_fno.csv"
        if not path.is_file():
            raise FileNotFoundError(f"Missing {path} — run master_downloader")
        df = pd.read_csv(path, low_memory=False)
        return df.rename(
            columns={
                "SEM_SMST_SECURITY_ID": "token",
                "SEM_STRIKE_PRICE": "strike",
                "SEM_OPTION_TYPE": "option_type",
                "SEM_EXPIRY_DATE": "expiry",
                "SEM_TRADING_SYMBOL": "trading_symbol",
                "SEM_INSTRUMENT_NAME": "instrument",
            }
        )
    path = DATA_DIR / "zerodha_nse_fno.csv"
    if not path.is_file():
        raise FileNotFoundError(f"Missing {path}")
    df = pd.read_csv(path, low_memory=False)
    # Zerodha columns vary; normalize common names
    colmap = {}
    for a, b in (
        ("instrument_token", "token"),
        ("strike", "strike"),
        ("instrument_type", "option_type"),
        ("expiry", "expiry"),
        ("tradingsymbol", "trading_symbol"),
        ("name", "name"),
    ):
        if a in df.columns:
            colmap[a] = b
    return df.rename(columns=colmap)


def _underlying_from_row(row: pd.Series) -> str:
    ts = str(row.get("trading_symbol") or "")
    if ts.upper().startswith("NIFTY"):
        # BANKNIFTY / FINNIFTY check first
        if ts.upper().startswith("BANKNIFTY"):
            return "BANKNIFTY"
        if ts.upper().startswith("FINNIFTY"):
            return "FINNIFTY"
        return "NIFTY"
    name = str(row.get("name") or row.get("SM_SYMBOL_NAME") or "").upper()
    if "BANK" in name:
        return "BANKNIFTY"
    if "NIFTY" in name:
        return "NIFTY"
    return "NIFTY"


def build_active_from_tokens(
    tokens: list[int],
    *,
    broker: str | None = None,
) -> dict[str, tuple[ActiveStrikeTokens, str]]:
    """Return {underlying: (ActiveStrikeTokens, expiry_iso)} for subscribed tokens."""
    if not tokens:
        return {}
    master = _load_master(broker)
    if "token" not in master.columns:
        raise ValueError("Master CSV missing token column")
    master["token"] = pd.to_numeric(master["token"], errors="coerce")
    matched = master[master["token"].isin(tokens)].copy()
    if matched.empty:
        raise RuntimeError("None of TRADE_TOKENS found in instrument master")

    matched["option_type"] = matched["option_type"].astype(str).str.upper().str.strip()
    matched["strike"] = pd.to_numeric(matched["strike"], errors="coerce")
    matched["expiry_d"] = pd.to_datetime(matched["expiry"], errors="coerce").dt.date
    matched["underlying"] = matched.apply(_underlying_from_row, axis=1)

    out: dict[str, tuple[ActiveStrikeTokens, str]] = {}
    for underlying, grp in matched.groupby("underlying"):
        if underlying not in {"NIFTY", "BANKNIFTY"}:
            continue
        # Prefer the nearest upcoming expiry present in the subscription
        today = date.today()
        expiries = sorted({d for d in grp["expiry_d"].dropna().tolist() if d >= today})
        if not expiries:
            expiries = sorted({d for d in grp["expiry_d"].dropna().tolist()})
        if not expiries:
            continue
        exp = expiries[0]
        g = grp[grp["expiry_d"] == exp]
        strikes = sorted({float(s) for s in g["strike"].dropna().tolist()})
        if not strikes:
            continue
        # ATM ≈ median subscribed strike
        mid = strikes[len(strikes) // 2]
        call_tokens: dict[float, int] = {}
        put_tokens: dict[float, int] = {}
        for _, row in g.iterrows():
            strike = float(row["strike"])
            tok = int(row["token"])
            ot = str(row["option_type"])
            if ot in {"CE", "C", "CALL"}:
                call_tokens[strike] = tok
            elif ot in {"PE", "P", "PUT"}:
                put_tokens[strike] = tok
        active = ActiveStrikeTokens(
            atm_strike=float(mid),
            strikes=tuple(strikes),
            call_tokens=call_tokens,
            put_tokens=put_tokens,
        )
        out[str(underlying)] = (active, exp.isoformat())
    return out


def _spot_hint(symbol: str, atm: float) -> float:
    hist = PROJECT_ROOT / "data" / "history" / f"{symbol.lower()}_daily.parquet"
    if hist.is_file():
        try:
            df = pd.read_parquet(hist)
            if "close" in df.columns and df["close"].notna().any():
                return float(df["close"].iloc[-1])
        except Exception:
            pass
    return float(atm)


def bootstrap_option_chains(
    *,
    redis_client: RedisClient | None = None,
    tokens: list[int] | None = None,
    broker: str | None = None,
) -> dict[str, Any]:
    """Build and persist option_chain:{SYMBOL} skeletons; return summary."""
    toks = tokens if tokens is not None else _parse_tokens()
    if not toks:
        raise RuntimeError("TRADE_TOKENS empty — cannot bootstrap option chain")

    redis = redis_client or RedisClient.from_settings()
    cache = ChainCache(redis)
    built = build_active_from_tokens(toks, broker=broker)
    summary: dict[str, Any] = {"tokens": len(toks), "underlyings": {}}

    for symbol, (active, expiry) in built.items():
        spot = _spot_hint(symbol, active.atm_strike)
        chain = cache.bootstrap(
            symbol,
            active,
            expiry=expiry,
            underlying_ltp=spot,
        )
        # Replay any already-cached ticks into the chain
        replayed = 0
        for tok in cache.indexed_tokens():
            raw = redis.get_latest_tick(tok)
            if raw:
                if cache.on_tick(tok, raw):
                    replayed += 1
        summary["underlyings"][symbol] = {
            "atm": active.atm_strike,
            "expiry": expiry,
            "strikes": len(active.strikes),
            "ce": len(active.call_tokens),
            "pe": len(active.put_tokens),
            "spot_hint": spot,
            "ticks_replayed": replayed,
            "updated_at": chain.get("updated_at"),
        }
        logger.info(
            "Bootstrapped option_chain:%s atm=%s expiry=%s strikes=%d replayed=%d",
            symbol,
            active.atm_strike,
            expiry,
            len(active.strikes),
            replayed,
        )
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        from dashboard.secrets_store import apply_secrets_to_environ

        apply_secrets_to_environ()
    except Exception:
        pass
    print(bootstrap_option_chains())
