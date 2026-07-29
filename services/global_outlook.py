"""Global outlook data: overseas markers, India proxies, FII/DII, and bias score.

Sources (lowest latency where possible):
  - DhanHQ charts — NIFTY / BANKNIFTY / INDIA_VIX / GIFTNIFTY / MCX CRUDE & GOLD
  - Yahoo Finance — US/EU/Asia indices, USDINR, US VIX (overseas tape)
  - NSE public API — latest FII / DII cash-market flows (appended to local history)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from services.history_downloader import (
    DHAN_HIST_URL,
    HISTORY_DIR,
    _candles_to_frame,
    _headers as dhan_headers,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GLOBAL_DIR = PROJECT_ROOT / "data" / "global"
FII_DII_PATH = GLOBAL_DIR / "fii_dii_daily.parquet"
MARKERS_PATH = GLOBAL_DIR / "markers_latest.parquet"
SNAPSHOT_PATH = GLOBAL_DIR / "outlook_snapshot.json"

# Overseas / cross-asset markers via Yahoo (Dhan does not list most of these).
YAHOO_MARKERS: dict[str, dict[str, str]] = {
    "SPX": {"ticker": "^GSPC", "name": "S&P 500", "region": "US", "why": "Risk appetite leader for EM / India"},
    "NASDAQ": {"ticker": "^IXIC", "name": "Nasdaq Composite", "region": "US", "why": "Growth / tech risk sentiment"},
    "DJI": {"ticker": "^DJI", "name": "Dow Jones", "region": "US", "why": "US industrial / value tone"},
    "US_VIX": {"ticker": "^VIX", "name": "CBOE VIX", "region": "US", "why": "Global fear gauge; spikes hurt NIFTY"},
    "NIKKEI": {"ticker": "^N225", "name": "Nikkei 225", "region": "ASIA", "why": "Asia risk-on cue into India open"},
    "HSI": {"ticker": "^HSI", "name": "Hang Seng", "region": "ASIA", "why": "China/HK risk; correlates with EM Asia"},
    "FTSE": {"ticker": "^FTSE", "name": "FTSE 100", "region": "EU", "why": "Europe overnight risk tone"},
    "DAX": {"ticker": "^GDAXI", "name": "DAX", "region": "EU", "why": "Eurozone cyclical tone"},
    "USDINR": {"ticker": "USDINR=X", "name": "USD / INR", "region": "FX", "why": "INR weakness often weighs on FII flows"},
    "WTI": {"ticker": "CL=F", "name": "WTI Crude", "region": "CMDTY", "why": "Oil shock → inflation / deficit risk"},
    "GOLD": {"ticker": "GC=F", "name": "Gold (COMEX)", "region": "CMDTY", "why": "Safe-haven bid when risk-off"},
}

# India-proximate markers via Dhan (IDX_I / MCX).
DHAN_MARKERS: dict[str, dict[str, str]] = {
    "NIFTY": {"security_id": "13", "segment": "IDX_I", "instrument": "INDEX", "name": "Nifty 50"},
    "BANKNIFTY": {"security_id": "25", "segment": "IDX_I", "instrument": "INDEX", "name": "Nifty Bank"},
    "INDIA_VIX": {"security_id": "21", "segment": "IDX_I", "instrument": "INDEX", "name": "India VIX"},
    "GIFTNIFTY": {"security_id": "5024", "segment": "IDX_I", "instrument": "INDEX", "name": "GIFT Nifty"},
}


@dataclass
class MarkerRow:
    symbol: str
    name: str
    region: str
    source: str
    last: float | None
    prev: float | None
    change_pct: float | None
    asof: str
    why: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OutlookSnapshot:
    asof: str
    bias: str
    score: float
    summary: str
    factors: list[dict[str, Any]] = field(default_factory=list)
    markers: list[dict[str, Any]] = field(default_factory=list)
    fii_dii: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure_dirs() -> None:
    GLOBAL_DIR.mkdir(parents=True, exist_ok=True)


def _pct(last: float | None, prev: float | None) -> float | None:
    if last is None or prev is None or prev == 0:
        return None
    return (last / prev - 1.0) * 100.0


def fetch_dhan_recent(symbol: str, *, days: int = 30) -> pd.DataFrame:
    meta = DHAN_MARKERS[symbol]
    end = date.today() + timedelta(days=1)
    start = date.today() - timedelta(days=days + 10)
    payload = {
        "securityId": meta["security_id"],
        "exchangeSegment": meta["segment"],
        "instrument": meta["instrument"],
        "expiryCode": 0,
        "oi": False,
        "fromDate": start.isoformat(),
        "toDate": end.isoformat(),
    }
    resp = requests.post(DHAN_HIST_URL, headers=dhan_headers(), json=payload, timeout=45)
    if resp.status_code != 200:
        raise RuntimeError(f"Dhan {symbol} HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not isinstance(data, dict) or "close" not in data:
        raise RuntimeError(f"Dhan {symbol} bad payload: {str(data)[:200]}")
    time.sleep(0.35)
    return _candles_to_frame(data, symbol=symbol)


def resolve_mcx_fut(symbol_name: str) -> tuple[str, str] | None:
    """Return (security_id, display) for nearest MCX FUTCOM."""
    headers = {k: v for k, v in dhan_headers().items() if k != "Content-Type"}
    r = requests.get("https://api.dhan.co/v2/instrument/MCX_COMM", headers=headers, timeout=120)
    r.raise_for_status()
    import io

    df = pd.read_csv(io.BytesIO(r.content), low_memory=False)
    m = df[(df["INSTRUMENT"] == "FUTCOM") & (df["SYMBOL_NAME"] == symbol_name)].copy()
    if m.empty:
        return None
    m["exp"] = m["SM_EXPIRY_DATE"].astype(str).str[:10]
    m = m[m["exp"] >= date.today().isoformat()].sort_values("exp")
    if m.empty:
        return None
    row = m.iloc[0]
    return str(int(row["SECURITY_ID"])), str(row.get("DISPLAY_NAME") or symbol_name)


def fetch_dhan_mcx_recent(symbol_name: str, label: str, *, days: int = 40) -> pd.DataFrame:
    resolved = resolve_mcx_fut(symbol_name)
    if not resolved:
        raise RuntimeError(f"No live MCX FUTCOM for {symbol_name}")
    sid, _ = resolved
    end = date.today() + timedelta(days=1)
    start = date.today() - timedelta(days=days + 10)
    payload = {
        "securityId": sid,
        "exchangeSegment": "MCX_COMM",
        "instrument": "FUTCOM",
        "expiryCode": 0,
        "oi": False,
        "fromDate": start.isoformat(),
        "toDate": end.isoformat(),
    }
    resp = requests.post(DHAN_HIST_URL, headers=dhan_headers(), json=payload, timeout=45)
    if resp.status_code != 200:
        raise RuntimeError(f"Dhan MCX {symbol_name} HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not isinstance(data, dict) or "close" not in data:
        raise RuntimeError(f"Dhan MCX {symbol_name} bad payload")
    time.sleep(0.35)
    return _candles_to_frame(data, symbol=label)


def fetch_yahoo_recent(ticker: str, *, period: str = "3mo") -> pd.DataFrame:
    import yfinance as yf

    df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
    if df is None or df.empty:
        return pd.DataFrame(columns=["time", "symbol", "open", "high", "low", "close", "volume"])
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.reset_index()
    time_col = "Date" if "Date" in df.columns else df.columns[0]
    out = pd.DataFrame(
        {
            "time": pd.to_datetime(df[time_col], utc=True),
            "symbol": ticker,
            "open": df.get("Open"),
            "high": df.get("High"),
            "low": df.get("Low"),
            "close": df.get("Close"),
            "volume": df.get("Volume", 0),
        }
    )
    return out.dropna(subset=["close"]).reset_index(drop=True)


def fetch_nse_fii_dii() -> list[dict[str, Any]]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/reports/fii-dii",
        }
    )
    session.get("https://www.nseindia.com", timeout=20)
    resp = session.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected FII/DII payload: {payload!r}")
    rows: list[dict[str, Any]] = []
    for item in payload:
        raw_date = str(item.get("date") or "")
        try:
            trade_date = datetime.strptime(raw_date, "%d-%b-%Y").date().isoformat()
        except ValueError:
            trade_date = raw_date
        rows.append(
            {
                "trade_date": trade_date,
                "category": str(item.get("category") or ""),
                "buy_cr": float(item.get("buyValue") or 0),
                "sell_cr": float(item.get("sellValue") or 0),
                "net_cr": float(item.get("netValue") or 0),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return rows


def upsert_fii_dii(rows: list[dict[str, Any]]) -> pd.DataFrame:
    _ensure_dirs()
    fresh = pd.DataFrame(rows)
    if FII_DII_PATH.is_file():
        old = pd.read_parquet(FII_DII_PATH)
        merged = pd.concat([old, fresh], ignore_index=True)
    else:
        merged = fresh
    merged = merged.drop_duplicates(subset=["trade_date", "category"], keep="last")
    merged = merged.sort_values(["trade_date", "category"]).reset_index(drop=True)
    merged.to_parquet(FII_DII_PATH, compression="zstd", index=False)
    return merged


def _marker_from_frame(
    symbol: str,
    name: str,
    region: str,
    source: str,
    frame: pd.DataFrame,
    why: str = "",
) -> MarkerRow:
    if frame.empty:
        return MarkerRow(symbol, name, region, source, None, None, None, "", why)
    closes = frame["close"].astype(float)
    last = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) > 1 else None
    asof = str(frame["time"].iloc[-1])
    return MarkerRow(symbol, name, region, source, last, prev, _pct(last, prev), asof, why)


def collect_markers() -> list[MarkerRow]:
    rows: list[MarkerRow] = []

    # Prefer long Dhan history files when present for India indices.
    for symbol, meta in DHAN_MARKERS.items():
        why = {
            "NIFTY": "Domestic benchmark",
            "BANKNIFTY": "Financials / rate-sensitivity proxy",
            "INDIA_VIX": "India implied vol; elevated → fragile bids",
            "GIFTNIFTY": "Near-24x7 SGX/GIFT cue into cash open",
        }.get(symbol, "")
        try:
            hist = HISTORY_DIR / f"{symbol.lower()}_daily.parquet"
            if symbol == "GIFTNIFTY" or not hist.is_file():
                frame = fetch_dhan_recent(symbol, days=60)
            else:
                frame = pd.read_parquet(hist)
            rows.append(
                _marker_from_frame(symbol, meta["name"], "INDIA", "dhan", frame, why)
            )
        except Exception as exc:
            logger.warning("Dhan marker %s failed: %s", symbol, exc)
            rows.append(
                MarkerRow(symbol, meta["name"], "INDIA", "dhan", None, None, None, "", why)
            )

    # MCX crude / gold via Dhan
    for sym_name, label, why in [
        ("CRUDEOIL", "MCX_CRUDE", "Domestic crude futures — energy shock channel"),
        ("GOLD", "MCX_GOLD", "Domestic gold futures — risk-off / INR hedge"),
    ]:
        try:
            frame = fetch_dhan_mcx_recent(sym_name, label)
            rows.append(
                _marker_from_frame(label, f"{sym_name} (MCX)", "CMDTY", "dhan", frame, why)
            )
        except Exception as exc:
            logger.warning("MCX %s failed: %s", sym_name, exc)

    # Overseas Yahoo markers
    for symbol, meta in YAHOO_MARKERS.items():
        try:
            frame = fetch_yahoo_recent(meta["ticker"])
            rows.append(
                _marker_from_frame(
                    symbol,
                    meta["name"],
                    meta["region"],
                    "yahoo",
                    frame,
                    meta["why"],
                )
            )
        except Exception as exc:
            logger.warning("Yahoo %s failed: %s", symbol, exc)
            rows.append(
                MarkerRow(
                    symbol,
                    meta["name"],
                    meta["region"],
                    "yahoo",
                    None,
                    None,
                    None,
                    "",
                    meta["why"],
                )
            )

    return rows


def _factor(name: str, signal: str, weight: float, detail: str) -> dict[str, Any]:
    return {"factor": name, "signal": signal, "weight": weight, "detail": detail}


def compute_outlook(
    markers: list[MarkerRow],
    fii_dii: pd.DataFrame,
) -> OutlookSnapshot:
    by = {m.symbol: m for m in markers}
    factors: list[dict[str, Any]] = []
    score = 0.0

    def add(sym: str, name: str, weight: float, bull_if_up: bool = True) -> None:
        nonlocal score
        m = by.get(sym)
        if not m or m.change_pct is None:
            factors.append(_factor(name, "n/a", weight, "No data"))
            return
        up = m.change_pct > 0
        bullish = up if bull_if_up else (not up)
        # VIX-style inverted
        signed = m.change_pct if bull_if_up else -m.change_pct
        # Soft clip contribution
        contrib = max(-weight, min(weight, signed / 1.5 * weight))
        score += contrib
        signal = "bullish" if bullish else "bearish"
        if abs(m.change_pct) < 0.15:
            signal = "neutral"
            contrib = 0.0
        factors.append(
            _factor(
                name,
                signal,
                weight,
                f"{m.name} {m.change_pct:+.2f}% (last {m.last})",
            )
        )

    add("SPX", "US S&P overnight", 1.2)
    add("NASDAQ", "US Nasdaq overnight", 1.0)
    add("US_VIX", "US VIX (invert)", 1.1, bull_if_up=False)
    add("NIKKEI", "Nikkei", 0.8)
    add("HSI", "Hang Seng", 0.7)
    add("USDINR", "USDINR (invert)", 1.0, bull_if_up=False)
    add("WTI", "Crude (invert mild)", 0.6, bull_if_up=False)
    add("INDIA_VIX", "India VIX (invert)", 1.0, bull_if_up=False)
    add("GIFTNIFTY", "GIFT Nifty", 1.3)
    add("MCX_CRUDE", "MCX Crude (invert mild)", 0.5, bull_if_up=False)

    # GIFT premium/discount vs NIFTY cash
    gift = by.get("GIFTNIFTY")
    nifty = by.get("NIFTY")
    if gift and nifty and gift.last and nifty.last:
        prem = (gift.last / nifty.last - 1.0) * 100.0
        if prem > 0.15:
            score += 0.4
            factors.append(_factor("GIFT premium", "bullish", 0.4, f"GIFT premium {prem:+.2f}% vs cash"))
        elif prem < -0.15:
            score -= 0.4
            factors.append(_factor("GIFT discount", "bearish", 0.4, f"GIFT discount {prem:+.2f}% vs cash"))
        else:
            factors.append(_factor("GIFT vs cash", "neutral", 0.4, f"GIFT flat {prem:+.2f}% vs cash"))

    # FII / DII
    if not fii_dii.empty:
        latest = fii_dii["trade_date"].max()
        day = fii_dii[fii_dii["trade_date"] == latest]
        fii = day[day["category"].str.contains("FII", case=False, na=False)]
        dii = day[day["category"].str.contains("DII", case=False, na=False)]
        fii_net = float(fii["net_cr"].sum()) if len(fii) else 0.0
        dii_net = float(dii["net_cr"].sum()) if len(dii) else 0.0
        # FII leads directionally; DII often offsets
        if fii_net > 500:
            score += 0.9
            sig = "bullish"
        elif fii_net < -500:
            score -= 0.9
            sig = "bearish"
        else:
            sig = "neutral"
        factors.append(
            _factor(
                "FII cash net",
                sig,
                0.9,
                f"{latest}: FII ₹{fii_net:+.0f} Cr · DII ₹{dii_net:+.0f} Cr",
            )
        )
        if dii_net > 1000 and fii_net < 0:
            factors.append(
                _factor(
                    "DII absorption",
                    "stabilizing",
                    0.3,
                    "DIIs buying while FIIs sell — cushions gaps",
                )
            )

    if score >= 1.5:
        bias = "BULLISH"
    elif score <= -1.5:
        bias = "BEARISH"
    else:
        bias = "RANGE / MIXED"

    bull = sum(1 for f in factors if f["signal"] == "bullish")
    bear = sum(1 for f in factors if f["signal"] == "bearish")
    summary = (
        f"Composite score {score:+.2f} → {bias}. "
        f"Bullish factors {bull}, bearish {bear}. "
        "Use as open-auction prior — confirm with live PCR / India VIX at 09:15–09:30 IST."
    )
    return OutlookSnapshot(
        asof=datetime.now(timezone.utc).isoformat(),
        bias=bias,
        score=round(score, 3),
        summary=summary,
        factors=factors,
        markers=[m.to_dict() for m in markers],
        fii_dii=fii_dii.tail(20).to_dict(orient="records") if not fii_dii.empty else [],
    )


def refresh_global_outlook() -> OutlookSnapshot:
    """Fetch all sources, persist caches, return snapshot for the UI."""
    _ensure_dirs()
    markers = collect_markers()
    markers_df = pd.DataFrame([m.to_dict() for m in markers])
    markers_df.to_parquet(MARKERS_PATH, compression="zstd", index=False)

    try:
        fii_rows = fetch_nse_fii_dii()
        fii_df = upsert_fii_dii(fii_rows)
    except Exception:
        logger.exception("FII/DII fetch failed")
        fii_df = pd.read_parquet(FII_DII_PATH) if FII_DII_PATH.is_file() else pd.DataFrame()

    snap = compute_outlook(markers, fii_df)
    SNAPSHOT_PATH.write_text(json.dumps(snap.to_dict(), indent=2, default=str), encoding="utf-8")
    return snap


def load_snapshot() -> OutlookSnapshot | None:
    if not SNAPSHOT_PATH.is_file():
        return None
    raw = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    return OutlookSnapshot(**raw)


def load_markers_table() -> pd.DataFrame:
    if MARKERS_PATH.is_file():
        return pd.read_parquet(MARKERS_PATH)
    return pd.DataFrame()


def load_fii_dii_table() -> pd.DataFrame:
    if FII_DII_PATH.is_file():
        return pd.read_parquet(FII_DII_PATH)
    return pd.DataFrame()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    snap = refresh_global_outlook()
    print(json.dumps(snap.to_dict(), indent=2, default=str)[:2000])
    print("bias", snap.bias, "score", snap.score)
