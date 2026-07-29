"""Export daily TimescaleDB ticks to compressed date-partitioned Parquet files."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import pyarrow as pa
import pyarrow.parquet as pq

from config.settings import get_settings
from services.master_downloader import DATA_DIR

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_COMPRESSION = "zstd"

_TICK_COLUMNS = ("time", "token", "last_price", "volume", "oi", "iv", "delta")

_FETCH_SQL = """
    SELECT time, token, last_price, volume, oi, iv, delta
    FROM fno_ticks
    WHERE time >= %s
      AND time < %s
    ORDER BY time ASC, token ASC
"""

_FETCH_SQL_TOKENS = """
    SELECT time, token, last_price, volume, oi, iv, delta
    FROM fno_ticks
    WHERE time >= %s
      AND time < %s
      AND token = ANY(%s)
    ORDER BY time ASC, token ASC
"""


def partition_path(
    trade_date: date,
    symbol: str,
    *,
    data_dir: Path | None = None,
) -> Path:
    """Return ``data/YYYY-MM-DD/<symbol>.parquet`` (symbol lowercased)."""
    root = data_dir or DATA_DIR
    return root / trade_date.isoformat() / f"{symbol.strip().lower()}.parquet"


def _day_bounds_ist(trade_date: date) -> tuple[datetime, datetime]:
    """Inclusive IST trading calendar day → half-open UTC timestamps."""
    start_ist = datetime(
        trade_date.year, trade_date.month, trade_date.day, tzinfo=IST
    )
    end_ist = start_ist + timedelta(days=1)
    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


def resolve_underlying_tokens(
    symbol: str,
    *,
    master_csv: Path | None = None,
) -> list[int]:
    """
    Resolve broker tokens for an underlying from the local NSE F&O master.

    Prefers ``data/zerodha_nse_fno.csv`` (``name`` + ``instrument_token``),
    falls back to ``data/dhan_nse_fno.csv``.
    """
    symbol_u = symbol.strip().upper()
    candidates = []
    if master_csv is not None:
        candidates.append(Path(master_csv))
    else:
        candidates.extend(
            [
                DATA_DIR / "zerodha_nse_fno.csv",
                DATA_DIR / "dhan_nse_fno.csv",
            ]
        )

    for path in candidates:
        if not path.exists():
            continue
        df = pd.read_csv(path, low_memory=False)
        if {"name", "instrument_token"}.issubset(df.columns):
            tokens = (
                df.loc[df["name"].astype(str).str.upper().eq(symbol_u), "instrument_token"]
                .dropna()
                .astype(int)
                .unique()
                .tolist()
            )
            if tokens:
                return sorted(tokens)
        if {"SEM_TRADING_SYMBOL", "SEM_SMST_SECURITY_ID"}.issubset(df.columns):
            mask = (
                df["SEM_TRADING_SYMBOL"]
                .astype(str)
                .str.upper()
                .str.startswith(f"{symbol_u}-")
            )
            tokens = (
                df.loc[mask, "SEM_SMST_SECURITY_ID"]
                .dropna()
                .astype(int)
                .unique()
                .tolist()
            )
            if tokens:
                return sorted(tokens)

    raise FileNotFoundError(
        f"No master tokens found for {symbol_u!r}. "
        "Run services/master_downloader.py or pass tokens= explicitly."
    )


def fetch_ticks_for_day(
    trade_date: date,
    *,
    tokens: Sequence[int] | None = None,
    database_url: str | None = None,
) -> pd.DataFrame:
    """Pull one IST calendar day of ``fno_ticks`` (optionally filtered by token)."""
    start_utc, end_utc = _day_bounds_ist(trade_date)
    url = database_url or get_settings().database_url

    with psycopg2.connect(url) as conn:
        if tokens:
            df = pd.read_sql_query(
                _FETCH_SQL_TOKENS,
                conn,
                params=(start_utc, end_utc, list(tokens)),
            )
        else:
            df = pd.read_sql_query(
                _FETCH_SQL,
                conn,
                params=(start_utc, end_utc),
            )

    if df.empty:
        return pd.DataFrame(columns=list(_TICK_COLUMNS))

    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


def write_parquet(
    df: pd.DataFrame,
    path: Path,
    *,
    compression: str = DEFAULT_COMPRESSION,
) -> Path:
    """Write a compressed PyArrow Parquet file, creating parent dirs as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, path, compression=compression)
    logger.info(
        "Wrote %d rows → %s (%s)",
        len(df),
        path,
        compression,
    )
    return path


def export_day(
    trade_date: date | str,
    symbol: str = "NIFTY",
    *,
    tokens: Sequence[int] | None = None,
    data_dir: Path | None = None,
    database_url: str | None = None,
    compression: str = DEFAULT_COMPRESSION,
) -> Path:
    """
    Dump one day's ticks for an underlying into
    ``data/YYYY-MM-DD/<symbol>.parquet``.
    """
    if isinstance(trade_date, str):
        trade_date = date.fromisoformat(trade_date)

    symbol_u = symbol.strip().upper()
    token_list: list[int] | None
    if tokens is not None:
        token_list = [int(t) for t in tokens]
    else:
        token_list = resolve_underlying_tokens(symbol_u)

    df = fetch_ticks_for_day(
        trade_date,
        tokens=token_list,
        database_url=database_url,
    )
    out = partition_path(trade_date, symbol_u, data_dir=data_dir)
    return write_parquet(df, out, compression=compression)


def export_days(
    start_date: date | str,
    end_date: date | str,
    symbols: Iterable[str] = ("NIFTY", "BANKNIFTY"),
    *,
    data_dir: Path | None = None,
    database_url: str | None = None,
    compression: str = DEFAULT_COMPRESSION,
) -> list[Path]:
    """Export an inclusive date range for one or more underlyings."""
    start = date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
    end = date.fromisoformat(end_date) if isinstance(end_date, str) else end_date
    if end < start:
        raise ValueError("end_date must be >= start_date")

    paths: list[Path] = []
    day = start
    while day <= end:
        for symbol in symbols:
            paths.append(
                export_day(
                    day,
                    symbol,
                    data_dir=data_dir,
                    database_url=database_url,
                    compression=compression,
                )
            )
        day += timedelta(days=1)
    return paths


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Export TimescaleDB fno_ticks to date-partitioned Parquet"
    )
    parser.add_argument("--date", required=True, help="Trade date YYYY-MM-DD (IST)")
    parser.add_argument(
        "--symbol",
        default="NIFTY",
        help="Underlying symbol (default NIFTY → nifty.parquet)",
    )
    parser.add_argument(
        "--compression",
        default=DEFAULT_COMPRESSION,
        choices=("zstd", "snappy", "gzip", "brotli", "none"),
    )
    args = parser.parse_args()
    path = export_day(
        args.date,
        args.symbol,
        compression="none" if args.compression == "none" else args.compression,
    )
    print(path)
