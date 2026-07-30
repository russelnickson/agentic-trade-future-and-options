"""Thesis — nett-impact day framework with live target vs achieved ticker."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dashboard.auth import render_sidebar_profile, require_login
from dashboard.components.capital import fetch_capital
from dashboard.components.console_runtime import classify_day_outcome, session_clock
from dashboard.components.positions import fetch_positions
from dashboard.secrets_store import apply_secrets_to_environ
from dashboard.timefmt import format_ist
from database.redis_client import RedisClient
from services.day_thesis import (
    live_market_tick,
    load_thesis,
    progress_to_target,
    refresh_day_thesis,
)

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
    "Live **target vs achieved nett P&L after brokerage, SEBI, STT, GST, stamp & exchange** — "
    "priority **PHENOMENAL → OKAY → FLAT → ACCEPTABLE_LOSS → BREACH**, ticking with the market."
)

clock = session_clock()
live_desk = clock.is_live_desk or clock.phase in {"PRE_OPEN", "OPEN", "CLOSING"}

with st.sidebar:
    st.subheader("Thesis")
    symbol = st.selectbox("Underlying", ["NIFTY", "BANKNIFTY", "FINNIFTY"])
    broker = st.selectbox("Broker (P&L / capital)", ["dhan", "zerodha"])
    auto_live = st.toggle("Live refresh (market hours)", value=live_desk)
    refresh_sec = st.select_slider("Live tick (sec)", options=[2, 5, 10, 30], value=5 if live_desk else 30)
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
            f"Target nett ₹{payload.get('target_profit_nett'):+,.0f} · "
            f"chase **{payload.get('primary_target')}**"
        )
        st.rerun()
    st.caption("Persists to `data/insights/day_thesis_*.json` · Redis `agent:thesis:today`.")

thesis = load_thesis(symbol, redis_client=client)
if not thesis:
    st.info("No thesis yet — click **Rebuild thesis** in the sidebar.")
    st.stop()

target_nett = float(thesis.get("target_profit_nett") or 0)
target_gross = float(thesis.get("target_profit_gross") or 0)
primary = str(thesis.get("primary_target") or "OKAY")
charges = thesis.get("session_charges") or {}
fee_total = float(charges.get("total") or 0)
capital_ref = float(thesis.get("capital_ref") or 0)


def _render_ticker() -> None:
    tick = live_market_tick(
        symbol,
        broker=broker,
        redis_client=client,
        session_charges_total=fee_total,
    )
    achieved_gross = tick.get("gross_pnl")
    achieved_nett = tick.get("nett_pnl")
    realized = tick.get("realized_pnl")
    unrealized = tick.get("unrealized_pnl")
    fees_live = float(tick.get("fees_live") or fee_total or 0)
    gap, progress = progress_to_target(achieved_nett, target_nett)
    live_grade = classify_day_outcome(
        achieved_nett,
        capital_ref=capital_ref or None,
    ).grade
    color = GRADE_COLORS.get(str(live_grade), "#6B7280")
    ltp = tick.get("underlying_ltp")
    ltp_s = "—" if ltp is None else f"{ltp:,.2f}"

    st.markdown("### Live ticker")
    t1, t2, t3, t4, t5, t6 = st.columns([1.1, 1, 1, 0.9, 0.9, 1])
    t1.metric(f"{symbol} LTP", ltp_s)
    t2.metric(
        "Day target (nett)",
        f"₹{target_nett:+,.0f}",
        help=(
            f"Enter **{primary}** after brokerage+SEBI+STT+GST · "
            f"gross ≈ ₹{target_gross:+,.0f}"
        ),
    )
    t3.metric(
        "Achieved nett (after fees)",
        "—" if achieved_nett is None else f"₹{achieved_nett:+,.0f}",
        delta=(
            None
            if gap is None
            else (f"gap ₹{gap:+,.0f}" if gap > 0 else f"ahead ₹{abs(gap):,.0f}")
        ),
        delta_color="inverse" if (gap or 0) > 0 else "normal",
    )
    t4.metric(
        "Realized",
        "—" if realized is None else f"₹{float(realized):+,.0f}",
    )
    t5.metric(
        "Unrealized",
        "—" if unrealized is None else f"₹{float(unrealized):+,.0f}",
    )
    t6.markdown(
        f"<div style='padding:0.65rem;border-radius:8px;background:{color}22;"
        f"border:1px solid {color}'><div style='font-size:0.7rem;color:#6B7280'>"
        f"Live grade</div><div style='font-size:1.25rem;font-weight:700;color:{color}'>"
        f"{live_grade}</div></div>",
        unsafe_allow_html=True,
    )

    pct = 0.0 if progress is None else min(float(progress), 150.0)
    st.progress(min(pct / 100.0, 1.0), text=f"Progress to day target · {pct:.0f}%")

    insight = tick.get("insight") or ""
    st.info(insight)

    trades = list(tick.get("trades") or [])
    sleeves = list(tick.get("sleeves") or [])
    if trades or sleeves:
        st.markdown("#### Executed trades (live)")
        table = []
        for t in trades:
            table.append(
                {
                    "Status": t.get("status"),
                    "Contract": t.get("symbol"),
                    "Side": t.get("option_type") or "—",
                    "Strike": t.get("strike"),
                    "Qty": t.get("qty"),
                    "Entry": t.get("entry"),
                    "LTP": t.get("ltp"),
                    "Realized": t.get("realized"),
                    "Unrealized": t.get("unrealized"),
                    "P&L": t.get("pnl"),
                }
            )
        if table:
            st.dataframe(
                pd.DataFrame(table),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Entry": st.column_config.NumberColumn(format="%.2f"),
                    "LTP": st.column_config.NumberColumn(format="%.2f"),
                    "Realized": st.column_config.NumberColumn(format="%+.2f"),
                    "Unrealized": st.column_config.NumberColumn(format="%+.2f"),
                    "P&L": st.column_config.NumberColumn(format="%+.2f"),
                },
            )
        if sleeves:
            with st.expander("Tactical sleeves / stops", expanded=False):
                st.dataframe(pd.DataFrame(sleeves), use_container_width=True, hide_index=True)
    else:
        st.caption("No day trades booked yet — waiting for tactical fills.")

    pcr = tick.get("pcr")
    atm = tick.get("atm")
    err = tick.get("pnl_error")
    st.caption(
        f"Chase **{primary}** · gross ₹"
        f"{'—' if achieved_gross is None else f'{achieved_gross:+,.0f}'} · "
        f"live fees ₹{fees_live:,.2f} "
        f"(brokerage+SEBI+STT+exchange+stamp+GST) · "
        f"ATM {atm or '—'} · PCR {f'{pcr:.3f}' if isinstance(pcr, (int, float)) else '—'} · "
        f"tick {format_ist(tick.get('asof'))}"
        + (f" · pnl warn: {err}" if err else "")
    )


if auto_live:

    @st.fragment(run_every=timedelta(seconds=int(refresh_sec)))
    def live_ticker() -> None:
        _render_ticker()

    live_ticker()
else:
    _render_ticker()

st.divider()
st.markdown("### Consolidation")
st.write(thesis.get("consolidation") or "")

m1, m2, m3 = st.columns(3)
m1.metric("Capital ref", f"₹{capital_ref:,.0f}")
m2.metric("Day loss budget", f"₹{float(thesis.get('day_budget') or 0):,.0f}")
m3.metric("Est. session charges", f"₹{fee_total:,.2f}")

st.markdown("### Priority framework (nett of charges)")
rows = []
for band in thesis.get("framework") or []:
    lo = band.get("nett_min")
    hi = band.get("nett_max")
    lo_num = lo if isinstance(lo, (int, float)) else None
    hi_num = hi if isinstance(hi, (int, float)) else None
    if lo_num is None and hi_num is not None:
        band_s = f"< ₹{hi_num:,.0f}"
    elif lo_num is not None and hi_num is None:
        band_s = f"≥ ₹{lo_num:,.0f}"
    elif lo_num is not None and hi_num is not None:
        low, high = (lo_num, hi_num) if lo_num <= hi_num else (hi_num, lo_num)
        band_s = f"₹{low:,.0f} … ₹{high:,.0f}"
    else:
        band_s = "—"
    marker = "← target" if band.get("grade") == primary else ""
    rows.append(
        {
            "Priority": band.get("priority"),
            "Grade": band.get("grade"),
            "Nett band": band_s,
            "Gross to hit": f"₹{float(band.get('gross_to_enter') or 0):,.0f}",
            "Fees in model": f"₹{float(band.get('estimated_charges_at_target') or 0):,.2f}",
            "": marker,
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
st.caption(f"thesis built {format_ist(asof) if asof else '—'} · symbol {thesis.get('symbol')}")
