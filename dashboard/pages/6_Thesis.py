"""Thesis — nett-impact day framework (PHENOMENAL → BREACH)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dashboard.auth import render_sidebar_profile, require_login
from dashboard.components.capital import fetch_capital
from dashboard.components.positions import fetch_positions
from dashboard.secrets_store import apply_secrets_to_environ
from dashboard.timefmt import format_ist
from database.redis_client import RedisClient
from services.day_thesis import load_thesis, refresh_day_thesis

st.set_page_config(
    page_title="Thesis · Trade Console",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)

require_login()
apply_secrets_to_environ()
render_sidebar_profile()

GRADE_COLORS = {
    "PHENOMENAL": "#0B3D1E",
    "OKAY": "#1D4E89",
    "FLAT": "#3D4A57",
    "ACCEPTABLE_LOSS": "#7A4D00",
    "BREACH": "#7F1D1D",
    "NO_DATA": "#6B7280",
}


@st.cache_resource
def _redis() -> RedisClient | None:
    try:
        client = RedisClient.from_settings()
        client.ping()
        return client
    except Exception:
        return None


client = _redis()

st.title("Thesis")
st.caption(
    "Agent consolidates the desk into a **nett-impact** day framework — "
    "priority **PHENOMENAL → OKAY → FLAT → ACCEPTABLE_LOSS → BREACH** after trade charges."
)

with st.sidebar:
    st.subheader("Thesis")
    symbol = st.selectbox("Underlying", ["NIFTY", "BANKNIFTY", "FINNIFTY"])
    broker = st.selectbox("Broker (P&L / capital)", ["dhan", "zerodha"])
    turnover = st.number_input(
        "Premium turnover proxy (₹)",
        min_value=0.0,
        value=0.0,
        step=1000.0,
        help="Leave 0 to auto-estimate ~2% of capital as options premium churn for fee proxy.",
    )
    if st.button("Rebuild thesis", type="primary", use_container_width=True):
        with st.spinner("Thesis agent consolidating nett framework…"):
            try:
                cap = fetch_capital(broker)  # type: ignore[arg-type]
                capital_ref = float(cap.available_margin or cap.total_capital or 0) or None
            except Exception:
                capital_ref = None
            try:
                rows, _err = fetch_positions(broker, redis_client=client)  # type: ignore[arg-type]
                gross = float(sum(r.pnl for r in rows)) if rows else 0.0
            except Exception:
                gross = None
            payload = refresh_day_thesis(
                symbol,
                gross_pnl=gross,
                capital_ref=capital_ref,
                premium_turnover=turnover or None,
                redis_client=client,
            )
        st.success(
            f"Primary target **{payload.get('primary_target')}** · "
            f"now **{payload.get('current_grade')}**"
        )
        st.rerun()
    st.caption("Persists to `data/insights/day_thesis_*.json` · Redis `agent:thesis:today`.")

thesis = load_thesis(symbol, redis_client=client)
if not thesis:
    st.info("No thesis yet — click **Rebuild thesis** in the sidebar.")
    st.stop()

current = str(thesis.get("current_grade") or "NO_DATA")
primary = str(thesis.get("primary_target") or "OKAY")
color = GRADE_COLORS.get(current, "#6B7280")

c1, c2, c3, c4 = st.columns(4)
c1.markdown(
    f"<div style='padding:0.75rem;border-radius:8px;background:{color}22;"
    f"border:1px solid {color}'><div style='font-size:0.75rem;color:#6B7280'>"
    f"Current (nett)</div><div style='font-size:1.4rem;font-weight:700;color:{color}'>"
    f"{current}</div></div>",
    unsafe_allow_html=True,
)
c2.metric("Primary chase", primary)
gross = thesis.get("current_gross_pnl")
nett = thesis.get("current_nett_pnl")
c3.metric(
    "Gross MTM",
    "—" if gross is None else f"₹{gross:,.0f}",
)
c4.metric(
    "Nett impact",
    "—" if nett is None else f"₹{nett:,.0f}",
    help="Gross − estimated session charges",
)

charges = thesis.get("session_charges") or {}
m1, m2, m3 = st.columns(3)
m1.metric("Capital ref", f"₹{float(thesis.get('capital_ref') or 0):,.0f}")
m2.metric("Day loss budget", f"₹{float(thesis.get('day_budget') or 0):,.0f}")
m3.metric("Est. session charges", f"₹{float(charges.get('total') or 0):,.2f}")

st.markdown("### Consolidation")
st.write(thesis.get("consolidation") or "")

st.markdown("### Priority framework (nett of charges)")
rows = []
for band in thesis.get("framework") or []:
    lo = band.get("nett_min")
    hi = band.get("nett_max")
    if lo == float("-inf") or lo == "-inf":
        band_s = f"< ₹{hi:,.0f}" if isinstance(hi, (int, float)) else "breach"
    elif hi is None:
        band_s = f"≥ ₹{lo:,.0f}"
    else:
        band_s = f"₹{lo:,.0f} … ₹{hi:,.0f}"
    rows.append(
        {
            "Priority": band.get("priority"),
            "Grade": band.get("grade"),
            "Nett band": band_s,
            "Gross to hit": f"₹{float(band.get('gross_to_enter') or 0):,.0f}",
            "Fees in model": f"₹{float(band.get('estimated_charges_at_target') or 0):,.2f}",
            "Playbook": band.get("playbook"),
        }
    )
st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

with st.expander("Charge breakdown (proxy)"):
    if charges:
        st.json(
            {
                k: charges.get(k)
                for k in (
                    "premium_turnover",
                    "buy_orders",
                    "sell_orders",
                    "brokerage",
                    "stt",
                    "exchange",
                    "sebi",
                    "stamp",
                    "gst",
                    "total",
                    "notes",
                )
            }
        )
    st.caption(thesis.get("disclaimer") or "")

sources = thesis.get("sources") or {}
strat = sources.get("strategies") or []
if strat:
    st.markdown("### Structures feeding the thesis")
    st.dataframe(pd.DataFrame(strat), use_container_width=True, hide_index=True)

asof = thesis.get("asof")
st.caption(f"asof {format_ist(asof) if asof else '—'} · symbol {thesis.get('symbol')}")
