"""Download multi-year OHLC history from DhanHQ (lowest-latency broker path).

Stores daily candles under ``data/history/<symbol>_daily.parquet``.
Requires an active Dhan Data API plan (``dataPlan: Active`` on the profile).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.master_downloader import DATA_DIR

logger = logging.getLogger(__name__)

HISTORY_DIR = DATA_DIR / "history"
DHAN_HIST_URL = "https://api.dhan.co/v2/charts/historical"

# Official Dhan index underlyings (IDX_I / INDEX).
DHAN_INDEX_UNIVERSE: dict[str, dict[str, str]] = {
    "NIFTY": {"security_id": "13", "segment": "IDX_I", "instrument": "INDEX"},
    "BANKNIFTY": {"security_id": "25", "segment": "IDX_I", "instrument": "INDEX"},
    "FINNIFTY": {"security_id": "27", "segment": "IDX_I", "instrument": "INDEX"},
    "INDIA_VIX": {"security_id": "21", "segment": "IDX_I", "instrument": "INDEX"},
    "MIDCPNIFTY": {"security_id": "442", "segment": "IDX_I", "instrument": "INDEX"},
}


@dataclass(frozen=True)
class HistoryJob:
    symbol: str
    security_id: str
    segment: str
    instrument: str
    years: int = 5


def _headers() -> dict[str, str]:
    load_dotenv(_ROOT / ".secrets.env", override=True)
    load_dotenv(_ROOT / ".env", override=False)
    token = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
    client_id = os.getenv("DHAN_CLIENT_ID", "").strip()
    if not token or not client_id:
        raise RuntimeError("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN required in .secrets.env")
    return {
        "access-token": token,
        "client-id": client_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _chunk_ranges(start: date, end: date, *, months: int = 6) -> list[tuple[date, date]]:
    """Half-open [from, to) chunks — Dhan ``toDate`` is non-inclusive."""
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=30 * months), end)
        chunks.append((cur, nxt))
        cur = nxt
    return chunks


def _candles_to_frame(payload: dict[str, Any], *, symbol: str) -> pd.DataFrame:
    opens = payload.get("open") or []
    highs = payload.get("high") or []
    lows = payload.get("low") or []
    closes = payload.get("close") or []
    volumes = payload.get("volume") or [0] * len(closes)
    timestamps = payload.get("timestamp") or []
    oi = payload.get("open_interest") or [None] * len(closes)
    n = min(len(opens), len(highs), len(lows), len(closes), len(timestamps))
    if n == 0:
        return pd.DataFrame(
            columns=["time", "symbol", "open", "high", "low", "close", "volume", "oi"]
        )
    rows = []
    for i in range(n):
        ts = float(timestamps[i])
        # Dhan returns epoch seconds (exchange local often IST midnight as UTC-5:30 offset).
        t = datetime.fromtimestamp(ts, tz=timezone.utc)
        rows.append(
            {
                "time": t,
                "symbol": symbol,
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i]),
                "volume": int(volumes[i] or 0),
                "oi": (int(oi[i]) if oi[i] not in (None, "") else None),
            }
        )
    return pd.DataFrame(rows)


def fetch_daily_chunk(
    *,
    security_id: str,
    segment: str,
    instrument: str,
    from_date: date,
    to_date: date,
    oi: bool = False,
    timeout: float = 60.0,
    max_retries: int = 6,
) -> dict[str, Any]:
    import time

    payload = {
        "securityId": str(security_id),
        "exchangeSegment": segment,
        "instrument": instrument,
        "expiryCode": 0,
        "oi": bool(oi),
        "fromDate": from_date.isoformat(),
        "toDate": to_date.isoformat(),
    }
    last_err: Exception | None = None
    for attempt in range(max_retries):
        resp = requests.post(DHAN_HIST_URL, headers=_headers(), json=payload, timeout=timeout)
        if resp.status_code == 429:
            wait = min(2 ** attempt, 30)
            logger.warning("Rate limited (DH-904); sleeping %ss", wait)
            time.sleep(wait)
            last_err = RuntimeError(f"Dhan historical HTTP 429: {resp.text[:300]}")
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Dhan historical HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, dict) or "close" not in data:
            if isinstance(data, dict) and data.get("status") == "failure":
                raise RuntimeError(f"Dhan historical failure: {data.get('remarks')}")
            raise RuntimeError(f"Unexpected Dhan historical payload: {str(data)[:300]}")
        # Gentle pacing between successful calls.
        time.sleep(0.35)
        return data
    assert last_err is not None
    raise last_err


def download_symbol_history(
    symbol: str,
    *,
    years: int = 5,
    end: date | None = None,
) -> Path:
    meta = DHAN_INDEX_UNIVERSE[symbol.upper()]
    end = end or date.today()
    start = date(end.year - years, end.month, end.day)
    frames: list[pd.DataFrame] = []
    for frm, to in _chunk_ranges(start, end + timedelta(days=1), months=6):
        logger.info("%s chunk %s → %s", symbol, frm, to)
        try:
            raw = fetch_daily_chunk(
                security_id=meta["security_id"],
                segment=meta["segment"],
                instrument=meta["instrument"],
                from_date=frm,
                to_date=to,
            )
        except Exception:
            logger.exception("%s failed chunk %s-%s", symbol, frm, to)
            raise
        frame = _candles_to_frame(raw, symbol=symbol.upper())
        if not frame.empty:
            frames.append(frame)
            logger.info("%s +%d bars", symbol, len(frame))

    if not frames:
        raise RuntimeError(f"No history returned for {symbol}")

    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{symbol.lower()}_daily.parquet"
    out.to_parquet(path, compression="zstd", index=False)
    logger.info("Wrote %s rows=%d range=%s→%s", path, len(out), out["time"].iloc[0], out["time"].iloc[-1])
    return path


def download_universe(
    symbols: Iterable[str] | None = None,
    *,
    years: int = 5,
) -> dict[str, Path]:
    symbols = list(symbols or ("NIFTY", "BANKNIFTY", "FINNIFTY", "INDIA_VIX"))
    results: dict[str, Path] = {}
    for symbol in symbols:
        key = symbol.upper()
        if key not in DHAN_INDEX_UNIVERSE:
            raise KeyError(f"Unsupported symbol {symbol!r}; known={sorted(DHAN_INDEX_UNIVERSE)}")
        results[key] = download_symbol_history(key, years=years)
    return results


def latest_close(symbol: str) -> float | None:
    path = HISTORY_DIR / f"{symbol.lower()}_daily.parquet"
    if not path.is_file():
        return None
    df = pd.read_parquet(path, columns=["close"])
    if df.empty:
        return None
    return float(df["close"].iloc[-1])


def summarize(path: Path) -> dict[str, Any]:
    df = pd.read_parquet(path)
    return {
        "path": str(path.relative_to(_ROOT)),
        "rows": int(len(df)),
        "start": str(df["time"].iloc[0]),
        "end": str(df["time"].iloc[-1]),
        "last_close": float(df["close"].iloc[-1]),
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [history_downloader] %(message)s",
    )
    parser = argparse.ArgumentParser(description="Download Dhan index daily history")
    parser.add_argument(
        "--symbols",
        default="NIFTY,BANKNIFTY,FINNIFTY,INDIA_VIX",
        help="Comma-separated index symbols",
    )
    parser.add_argument("--years", type=int, default=5)
    args = parser.parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    paths = download_universe(symbols, years=args.years)
    for sym, path in paths.items():
        print(f"{sym}: {summarize(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
