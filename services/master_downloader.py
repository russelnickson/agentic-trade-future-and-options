"""Download daily instrument masters from Zerodha and Dhan, keep NSE F&O only."""

from __future__ import annotations

import logging
from io import StringIO
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

logger = logging.getLogger(__name__)

ZERODHA_INSTRUMENTS_URL = "https://api.kite.trade/instruments"
DHAN_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"

# Dhan equity derivatives (NSE F&O); excludes currency (C) and commodity (M).
DHAN_NSE_FNO_INSTRUMENTS = frozenset({"FUTIDX", "FUTSTK", "OPTIDX", "OPTSTK"})


def _fetch_csv(url: str) -> str:
    request = Request(url, headers={"User-Agent": "trade-data-engine/1.0"})
    with urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8")


def download_zerodha_nse_fno() -> pd.DataFrame:
    """Zerodha NSE F&O lives on exchange NFO (segments NFO-FUT / NFO-OPT)."""
    raw = _fetch_csv(ZERODHA_INSTRUMENTS_URL)
    df = pd.read_csv(StringIO(raw))
    filtered = df.loc[df["exchange"].eq("NFO")].copy()
    logger.info("Zerodha: %d / %d rows are NSE F&O (NFO)", len(filtered), len(df))
    return filtered


def download_dhan_nse_fno() -> pd.DataFrame:
    """Dhan NSE F&O: exchange NSE + FUTIDX/FUTSTK/OPTIDX/OPTSTK (segment D)."""
    raw = _fetch_csv(DHAN_SCRIP_MASTER_URL)
    df = pd.read_csv(StringIO(raw), low_memory=False)
    mask = df["SEM_EXM_EXCH_ID"].eq("NSE") & df["SEM_INSTRUMENT_NAME"].isin(
        DHAN_NSE_FNO_INSTRUMENTS
    )
    filtered = df.loc[mask].copy()
    logger.info("Dhan: %d / %d rows are NSE F&O", len(filtered), len(df))
    return filtered


def save_masters(output_dir: Path | None = None) -> dict[str, Path]:
    output_dir = output_dir or DATA_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    zerodha = download_zerodha_nse_fno()
    dhan = download_dhan_nse_fno()

    paths = {
        "zerodha": output_dir / "zerodha_nse_fno.csv",
        "dhan": output_dir / "dhan_nse_fno.csv",
    }
    zerodha.to_csv(paths["zerodha"], index=False)
    dhan.to_csv(paths["dhan"], index=False)
    logger.info("Wrote %s and %s", paths["zerodha"], paths["dhan"])
    return paths


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    paths = save_masters()
    for broker, path in paths.items():
        print(f"{broker}: {path}")


if __name__ == "__main__":
    main()
