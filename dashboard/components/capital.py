"""Live capital / margin metrics for the Streamlit terminal."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any, Literal

import streamlit as st

from config.settings import get_settings

logger = logging.getLogger(__name__)

BrokerName = Literal["dhan", "zerodha"]
CASH_RULE_RATIO = 0.50  # 50% cash rule


@dataclass(frozen=True)
class CapitalSnapshot:
    broker: BrokerName
    available_margin: float
    utilized_margin: float
    cash_balance: float
    collateral_value: float
    cash_ratio: float | None
    cash_rule_ok: bool | None
    raw: dict[str, Any] | None = None
    error: str | None = None

    @property
    def total_capital(self) -> float:
        return float(self.cash_balance) + float(self.collateral_value)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["total_capital"] = self.total_capital
        return data


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _cash_rule(cash: float, collateral: float) -> tuple[float | None, bool | None]:
    total = cash + collateral
    if total <= 0:
        return None, None
    ratio = cash / total
    return ratio, ratio >= CASH_RULE_RATIO


def fetch_dhan_capital() -> CapitalSnapshot:
    """Pull fund limits from DhanHQ ``/fundlimit``."""
    from dhanhq import DhanContext, Funds

    settings = get_settings()
    try:
        ctx = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
        response = Funds(ctx).get_fund_limits()
        data = response.get("data", response) if isinstance(response, dict) else {}
        if not isinstance(data, dict):
            data = {}

        available = _as_float(
            data.get("availabelBalance", data.get("availableBalance"))
        )
        utilized = _as_float(data.get("utilizedAmount", data.get("utilisedAmount")))
        # Prefer withdrawable / explicit cash; fall back to available when cash-only.
        cash = _as_float(
            data.get("withdrawableBalance", data.get("sodLimit", available))
        )
        collateral = _as_float(data.get("collateralAmount"))
        # If SOD limit includes collateral, approximate cash as available - collateral.
        if collateral > 0 and cash == available:
            cash = max(available - collateral, 0.0)

        ratio, ok = _cash_rule(cash, collateral)
        return CapitalSnapshot(
            broker="dhan",
            available_margin=available,
            utilized_margin=utilized,
            cash_balance=cash,
            collateral_value=collateral,
            cash_ratio=ratio,
            cash_rule_ok=ok,
            raw=response if isinstance(response, dict) else {"data": data},
        )
    except Exception as exc:
        logger.exception("Dhan capital fetch failed")
        return CapitalSnapshot(
            broker="dhan",
            available_margin=0.0,
            utilized_margin=0.0,
            cash_balance=0.0,
            collateral_value=0.0,
            cash_ratio=None,
            cash_rule_ok=None,
            error=str(exc),
        )


def fetch_zerodha_capital() -> CapitalSnapshot:
    """Pull equity margins from Zerodha Kite Connect."""
    from kiteconnect import KiteConnect

    settings = get_settings()
    try:
        kite = KiteConnect(api_key=settings.zerodha_api_key)
        token = settings.zerodha_access_token or os.getenv("ZERODHA_ACCESS_TOKEN", "")
        if not token:
            return CapitalSnapshot(
                broker="zerodha",
                available_margin=0.0,
                utilized_margin=0.0,
                cash_balance=0.0,
                collateral_value=0.0,
                cash_ratio=None,
                cash_rule_ok=None,
                error="ZERODHA_ACCESS_TOKEN missing — complete headless login first",
            )

        kite.set_access_token(token)
        margins = kite.margins()
        equity = (margins or {}).get("equity") or {}
        available_block = equity.get("available") or {}
        utilised_block = equity.get("utilised") or {}

        cash = _as_float(available_block.get("cash"))
        collateral = _as_float(available_block.get("collateral"))
        available = _as_float(
            equity.get("net", available_block.get("live_balance", cash + collateral))
        )
        # Sum primary utilisation buckets when a single total is absent.
        utilized = _as_float(utilised_block.get("debits"))
        if utilized == 0.0:
            utilized = sum(
                _as_float(utilised_block.get(key))
                for key in (
                    "span",
                    "exposure",
                    "option_premium",
                    "holding_sales",
                    "turnover",
                    "m2m_realised",
                    "m2m_unrealised",
                )
            )

        ratio, ok = _cash_rule(cash, collateral)
        return CapitalSnapshot(
            broker="zerodha",
            available_margin=available,
            utilized_margin=utilized,
            cash_balance=cash,
            collateral_value=collateral,
            cash_ratio=ratio,
            cash_rule_ok=ok,
            raw=margins if isinstance(margins, dict) else None,
        )
    except Exception as exc:
        logger.exception("Zerodha capital fetch failed")
        return CapitalSnapshot(
            broker="zerodha",
            available_margin=0.0,
            utilized_margin=0.0,
            cash_balance=0.0,
            collateral_value=0.0,
            cash_ratio=None,
            cash_rule_ok=None,
            error=str(exc),
        )


def fetch_capital(broker: BrokerName = "dhan") -> CapitalSnapshot:
    if broker == "dhan":
        return fetch_dhan_capital()
    if broker == "zerodha":
        return fetch_zerodha_capital()
    raise ValueError(f"Unsupported broker: {broker!r}")


def _fmt_inr(value: float) -> str:
    return f"₹{value:,.2f}"


def render_capital_cards(
    broker: BrokerName = "dhan",
    *,
    snapshot: CapitalSnapshot | None = None,
) -> CapitalSnapshot:
    """
    Fetch (unless ``snapshot`` provided) and render Streamlit metric cards:

    - Available Margin
    - Utilized Margin
    - Cash Balance (+ 50% cash-rule compliance)
    - Collateral Value
    """
    snap = snapshot or fetch_capital(broker)

    st.subheader(f"Capital — {snap.broker.upper()}")
    if snap.error:
        st.error(f"Unable to load live balances: {snap.error}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Available Margin", _fmt_inr(snap.available_margin))
    c2.metric("Utilized Margin", _fmt_inr(snap.utilized_margin))

    if snap.cash_rule_ok is True:
        cash_delta = f"OK · {snap.cash_ratio:.0%} cash"
    elif snap.cash_rule_ok is False:
        cash_delta = f"BREACH · {snap.cash_ratio:.0%} cash (<50%)"
    else:
        cash_delta = "50% rule n/a"

    c3.metric(
        "Cash Balance",
        _fmt_inr(snap.cash_balance),
        delta=cash_delta,
        delta_color="normal" if snap.cash_rule_ok is not False else "inverse",
    )
    c4.metric("Collateral Value", _fmt_inr(snap.collateral_value))

    if snap.cash_rule_ok is False:
        st.warning(
            f"50% cash rule **not met**: cash is "
            f"{(snap.cash_ratio or 0):.1%} of cash+collateral "
            f"(required ≥ {CASH_RULE_RATIO:.0%})."
        )
    elif snap.cash_rule_ok is True:
        st.success(
            f"50% cash rule compliant "
            f"({(snap.cash_ratio or 0):.1%} cash vs collateral)."
        )

    return snap
