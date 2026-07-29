"""Real-time open F&O positions table for the Streamlit terminal."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import pandas as pd
import streamlit as st

from config.settings import get_settings
from database.redis_client import RedisClient
from services.greeks_engine import compute_greeks, years_to_expiry

logger = logging.getLogger(__name__)

BrokerName = Literal["dhan", "zerodha"]

_FNO_SEGMENTS = frozenset({"NSE_FNO", "BSE_FNO", "NFO", "BFO", "MCX"})
_ZERODHA_OPT_RE = re.compile(
    r"^(?P<symbol>[A-Z0-9]+?)(?P<y>\d{2})(?P<mon>[A-Z]{3})(?P<strike>\d+(?:\.\d+)?)(?P<opt>CE|PE)$"
)
_ZERODHA_FUT_RE = re.compile(
    r"^(?P<symbol>[A-Z0-9]+?)(?P<y>\d{2})(?P<mon>[A-Z]{3})FUT$"
)
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


@dataclass
class PositionRow:
    symbol: str
    expiry: str
    strike: float | None
    qty: int
    entry_price: float
    ltp: float | None
    delta: float | None
    pnl: float
    option_type: str | None = None
    token: int | None = None

    def to_display_dict(self) -> dict[str, Any]:
        return {
            "Symbol": self.symbol,
            "Expiry": self.expiry,
            "Strike": self.strike if self.strike is not None else "—",
            "Qty": self.qty,
            "Entry Price": self.entry_price,
            "LTP": self.ltp if self.ltp is not None else "—",
            "Delta": self.delta if self.delta is not None else "—",
            "P&L": self.pnl,
        }


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _unwrap_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for key in ("data", "net", "positions"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [p for p in inner if isinstance(p, dict)]
            if isinstance(inner, dict):
                # Dhan sometimes wraps again
                nested = inner.get("data")
                if isinstance(nested, list):
                    return [p for p in nested if isinstance(p, dict)]
    return []


def _parse_zerodha_contract(tradingsymbol: str) -> tuple[str, str, float | None, str | None]:
    """Return (symbol, expiry_label, strike, CE/PE|None) from Kite tradingsymbol."""
    text = (tradingsymbol or "").upper().strip()
    m = _ZERODHA_OPT_RE.match(text)
    if m:
        year = 2000 + int(m.group("y"))
        month = _MONTHS.get(m.group("mon"), 1)
        expiry = f"{year}-{month:02d}"
        return m.group("symbol"), expiry, float(m.group("strike")), m.group("opt")
    m = _ZERODHA_FUT_RE.match(text)
    if m:
        year = 2000 + int(m.group("y"))
        month = _MONTHS.get(m.group("mon"), 1)
        return m.group("symbol"), f"{year}-{month:02d}", None, None
    return text, "—", None, None


def _ltp_from_redis(token: int | None, redis_client: RedisClient | None) -> float | None:
    if token is None or redis_client is None:
        return None
    try:
        tick = redis_client.get_latest_tick(token)
    except Exception:
        return None
    if not tick:
        return None
    for key in ("ltp", "last_price", "last_traded_price", "LTP"):
        if key in tick and tick[key] is not None:
            return _as_float(tick[key])
    return None


def _estimate_delta(
    *,
    symbol: str,
    strike: float | None,
    option_type: str | None,
    ltp: float | None,
    expiry: str,
    underlying_ltp: float | None,
) -> float | None:
    if (
        strike is None
        or option_type not in {"CE", "PE", "CALL", "PUT"}
        or ltp is None
        or ltp <= 0
        or underlying_ltp is None
        or underlying_ltp <= 0
    ):
        return None
    try:
        # Expiry may be YYYY-MM or YYYY-MM-DD — use mid-month if day missing.
        if len(expiry) == 7:
            exp_date = date(int(expiry[:4]), int(expiry[5:7]), 28)
        else:
            exp_date = date.fromisoformat(expiry[:10])
        days = max((exp_date - date.today()).days, 1)
        result = compute_greeks(
            spot=underlying_ltp,
            strike=strike,
            tte=years_to_expiry(days=days),
            option_ltp=ltp,
            option_type=option_type,
        )
        return result.delta
    except Exception:
        logger.debug("Delta estimate failed for %s", symbol, exc_info=True)
        return None


def _underlying_ltp(symbol: str, redis_client: RedisClient | None) -> float | None:
    if redis_client is None:
        return None
    root = re.sub(r"[^A-Z]", "", symbol.upper())
    for candidate in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        if root.startswith(candidate):
            try:
                chain = redis_client.get_option_chain_state(candidate)
            except Exception:
                return None
            if chain and chain.get("underlying_ltp") is not None:
                return _as_float(chain["underlying_ltp"])
    return None


def fetch_dhan_positions(
    redis_client: RedisClient | None = None,
) -> tuple[list[PositionRow], str | None]:
    from dhanhq import DhanContext, Portfolio

    settings = get_settings()
    try:
        ctx = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
        payload = Portfolio(ctx).get_positions()
        rows: list[PositionRow] = []
        for pos in _unwrap_list(payload):
            segment = str(pos.get("exchangeSegment") or "").upper()
            if segment and segment not in _FNO_SEGMENTS and "FNO" not in segment:
                # Still allow rows that look like options/futures via drv fields.
                if not pos.get("drvOptionType") and not pos.get("drvExpiryDate"):
                    continue
            qty = _as_int(pos.get("netQty"))
            if qty == 0:
                continue
            opt = pos.get("drvOptionType")
            option_type = None
            if opt:
                option_type = "CE" if str(opt).upper() in {"CALL", "CE", "C"} else "PE"
            token = _as_int(pos.get("securityId"), default=-1)
            token_i = token if token >= 0 else None
            entry = _as_float(pos.get("costPrice", pos.get("buyAvg")))
            if qty < 0:
                entry = _as_float(pos.get("sellAvg", entry))
            ltp = _ltp_from_redis(token_i, redis_client)
            if ltp is None and entry:
                # Back out LTP from unrealized PnL when broker omits last price.
                upnl = _as_float(pos.get("unrealizedProfit"))
                if qty != 0:
                    ltp = entry + (upnl / qty)
            expiry = str(pos.get("drvExpiryDate") or "—")
            if expiry.startswith("0001"):
                expiry = "—"
            strike = pos.get("drvStrikePrice")
            strike_f = _as_float(strike) if strike not in (None, "", 0, 0.0) else None
            symbol = str(pos.get("tradingSymbol") or token_i or "—")
            underlying = _underlying_ltp(symbol, redis_client)
            delta = _estimate_delta(
                symbol=symbol,
                strike=strike_f,
                option_type=option_type,
                ltp=ltp,
                expiry=expiry if expiry != "—" else date.today().isoformat(),
                underlying_ltp=underlying,
            )
            pnl = _as_float(pos.get("unrealizedProfit"))
            rows.append(
                PositionRow(
                    symbol=symbol,
                    expiry=expiry[:10] if expiry != "—" else "—",
                    strike=strike_f,
                    qty=qty,
                    entry_price=entry,
                    ltp=ltp,
                    delta=round(delta, 4) if delta is not None else None,
                    pnl=pnl,
                    option_type=option_type,
                    token=token_i,
                )
            )
        return rows, None
    except Exception as exc:
        logger.exception("Dhan positions fetch failed")
        return [], str(exc)


def fetch_zerodha_positions(
    redis_client: RedisClient | None = None,
) -> tuple[list[PositionRow], str | None]:
    from kiteconnect import KiteConnect

    settings = get_settings()
    try:
        token = settings.zerodha_access_token or os.getenv("ZERODHA_ACCESS_TOKEN", "")
        if not token:
            return [], "ZERODHA_ACCESS_TOKEN missing — complete headless login first"

        kite = KiteConnect(api_key=settings.zerodha_api_key)
        kite.set_access_token(token)
        payload = kite.positions()
        net = _unwrap_list(payload if not isinstance(payload, dict) else payload.get("net", payload))
        rows: list[PositionRow] = []
        for pos in net:
            exchange = str(pos.get("exchange") or "").upper()
            if exchange and exchange not in _FNO_SEGMENTS and exchange not in {"NFO", "BFO", "MCX", "CDS"}:
                continue
            qty = _as_int(pos.get("quantity"))
            if qty == 0:
                continue
            tradingsymbol = str(pos.get("tradingsymbol") or "")
            symbol, expiry, strike, option_type = _parse_zerodha_contract(tradingsymbol)
            instrument_token = _as_int(pos.get("instrument_token"), default=-1)
            token_i = instrument_token if instrument_token >= 0 else None
            entry = _as_float(pos.get("average_price"))
            ltp = _as_float(pos.get("last_price")) if pos.get("last_price") is not None else None
            if ltp is None:
                ltp = _ltp_from_redis(token_i, redis_client)
            underlying = _underlying_ltp(symbol, redis_client)
            # Prefer month-end-ish expiry for greeks when only YYYY-MM known.
            expiry_for_greeks = expiry
            if re.fullmatch(r"\d{4}-\d{2}", expiry):
                expiry_for_greeks = f"{expiry}-28"
            delta = _estimate_delta(
                symbol=symbol,
                strike=strike,
                option_type=option_type,
                ltp=ltp,
                expiry=expiry_for_greeks if expiry != "—" else date.today().isoformat(),
                underlying_ltp=underlying,
            )
            pnl = _as_float(pos.get("pnl", pos.get("unrealised")))
            rows.append(
                PositionRow(
                    symbol=tradingsymbol or symbol,
                    expiry=expiry,
                    strike=strike,
                    qty=qty,
                    entry_price=entry,
                    ltp=ltp,
                    delta=round(delta, 4) if delta is not None else None,
                    pnl=pnl,
                    option_type=option_type,
                    token=token_i,
                )
            )
        return rows, None
    except Exception as exc:
        logger.exception("Zerodha positions fetch failed")
        return [], str(exc)


def fetch_positions(
    broker: BrokerName = "dhan",
    redis_client: RedisClient | None = None,
) -> tuple[list[PositionRow], str | None]:
    if broker == "dhan":
        return fetch_dhan_positions(redis_client)
    if broker == "zerodha":
        return fetch_zerodha_positions(redis_client)
    raise ValueError(f"Unsupported broker: {broker!r}")


def _style_pnl(df: pd.DataFrame) -> Any:
    display = df.copy()

    def _color(val: Any) -> str:
        try:
            num = float(val)
        except (TypeError, ValueError):
            return ""
        if num > 0:
            return "color: #0a7a32; font-weight: 600"
        if num < 0:
            return "color: #c62828; font-weight: 600"
        return "color: #666666"

    styler = display.style.map(_color, subset=["P&L"])

    def _fmt_num(val: Any, digits: int) -> str:
        if val == "—" or val is None:
            return "—"
        try:
            return f"{float(val):.{digits}f}"
        except (TypeError, ValueError):
            return str(val)

    return styler.format(
        {
            "Entry Price": lambda v: _fmt_num(v, 2),
            "LTP": lambda v: _fmt_num(v, 2),
            "Delta": lambda v: _fmt_num(v, 4),
            "P&L": lambda v: _fmt_num(v, 2) if v == "—" else f"{float(v):+.2f}",
            "Strike": lambda v: "—"
            if v == "—"
            else (
                f"{float(v):.0f}"
                if float(v) == int(float(v))
                else f"{float(v):.2f}"
            ),
        }
    )


def positions_dataframe(rows: list[PositionRow]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(
            columns=[
                "Symbol",
                "Expiry",
                "Strike",
                "Qty",
                "Entry Price",
                "LTP",
                "Delta",
                "P&L",
            ]
        )
    return pd.DataFrame([r.to_display_dict() for r in rows])


def render_positions_table(
    broker: BrokerName = "dhan",
    *,
    redis_client: RedisClient | None = None,
) -> list[PositionRow]:
    """Fetch open F&O positions and render a color-coded P&L table."""
    st.subheader(f"Open F&O Positions — {broker.upper()}")
    rows, error = fetch_positions(broker, redis_client=redis_client)
    if error:
        st.error(f"Unable to load positions: {error}")
        return rows

    df = positions_dataframe(rows)
    if df.empty:
        st.info("No open F&O positions.")
        return rows

    total_pnl = float(sum(r.pnl for r in rows))
    st.metric(
        "Net unrealized P&L",
        f"₹{total_pnl:+,.2f}",
        delta="profit" if total_pnl >= 0 else "loss",
        delta_color="normal" if total_pnl >= 0 else "inverse",
    )

    try:
        st.dataframe(_style_pnl(df), use_container_width=True, hide_index=True)
    except Exception:
        # Fallback if Styler formatting fails on mixed types.
        st.dataframe(df, use_container_width=True, hide_index=True)

    return rows
