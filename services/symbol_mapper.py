"""Map option strike details to broker security IDs from the NSE F&O master CSV."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Union

import pandas as pd

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

ExpiryLike = Union[str, date, datetime]
StrikeLike = Union[int, float, str]
OptionToken = int

_OPTION_TYPE_ALIASES = {
    "CE": "CE",
    "C": "CE",
    "CALL": "CE",
    "PE": "PE",
    "P": "PE",
    "PUT": "PE",
}


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _normalize_expiry(expiry: ExpiryLike) -> date:
    if isinstance(expiry, datetime):
        return expiry.date()
    if isinstance(expiry, date):
        return expiry
    text = str(expiry).strip()
    if " " in text:
        text = text.split(" ", 1)[0]
    return date.fromisoformat(text)


def _normalize_strike(strike: StrikeLike) -> float:
    return float(strike)


def _normalize_option_type(option_type: str) -> str:
    key = option_type.strip().upper()
    try:
        return _OPTION_TYPE_ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported option_type: {option_type!r}") from exc


def _lookup_key(
    symbol: str,
    expiry: ExpiryLike,
    strike: StrikeLike,
    option_type: str,
) -> tuple[str, date, float, str]:
    return (
        _normalize_symbol(symbol),
        _normalize_expiry(expiry),
        _normalize_strike(strike),
        _normalize_option_type(option_type),
    )


class SymbolMapper:
    """In-memory index from (symbol, expiry, strike, CE/PE) -> broker security ID."""

    def __init__(self, master_csv: str | Path) -> None:
        self.master_csv = Path(master_csv)
        self._tokens: dict[tuple[str, date, float, str], OptionToken] = {}
        self.broker: str = "unknown"
        self._load(self.master_csv)

    @classmethod
    def from_zerodha(cls, path: str | Path | None = None) -> SymbolMapper:
        return cls(path or DATA_DIR / "zerodha_nse_fno.csv")

    @classmethod
    def from_dhan(cls, path: str | Path | None = None) -> SymbolMapper:
        return cls(path or DATA_DIR / "dhan_nse_fno.csv")

    def _load(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(
                f"Instrument master not found: {path}. "
                "Run services/master_downloader.py first."
            )

        df = pd.read_csv(path, low_memory=False)
        if {"name", "expiry", "strike", "instrument_type", "instrument_token"}.issubset(
            df.columns
        ):
            self.broker = "zerodha"
            self._load_zerodha(df)
        elif {
            "SEM_TRADING_SYMBOL",
            "SEM_EXPIRY_DATE",
            "SEM_STRIKE_PRICE",
            "SEM_OPTION_TYPE",
            "SEM_SMST_SECURITY_ID",
        }.issubset(df.columns):
            self.broker = "dhan"
            self._load_dhan(df)
        else:
            raise ValueError(f"Unrecognized instrument master schema: {path}")

    def _load_zerodha(self, df: pd.DataFrame) -> None:
        options = df.loc[df["instrument_type"].isin(["CE", "PE"])]
        for row in options.itertuples(index=False):
            key = _lookup_key(
                row.name,
                row.expiry,
                row.strike,
                row.instrument_type,
            )
            self._tokens[key] = int(row.instrument_token)

    def _load_dhan(self, df: pd.DataFrame) -> None:
        options = df.loc[
            df["SEM_INSTRUMENT_NAME"].isin(["OPTIDX", "OPTSTK"])
            & df["SEM_OPTION_TYPE"].isin(["CE", "PE"])
        ]
        for row in options.itertuples(index=False):
            symbol = str(row.SEM_TRADING_SYMBOL).split("-", 1)[0]
            key = _lookup_key(
                symbol,
                row.SEM_EXPIRY_DATE,
                row.SEM_STRIKE_PRICE,
                row.SEM_OPTION_TYPE,
            )
            self._tokens[key] = int(row.SEM_SMST_SECURITY_ID)

    def get_option_token(
        self,
        symbol: str,
        expiry: ExpiryLike,
        strike: StrikeLike,
        option_type: str,
    ) -> OptionToken:
        """Resolve an option contract to the broker security / instrument token."""
        key = _lookup_key(symbol, expiry, strike, option_type)
        try:
            return self._tokens[key]
        except KeyError as exc:
            raise KeyError(
                "No option token for "
                f"symbol={symbol!r} expiry={expiry!r} "
                f"strike={strike!r} option_type={option_type!r} "
                f"(broker={self.broker})"
            ) from exc

    def __len__(self) -> int:
        return len(self._tokens)
