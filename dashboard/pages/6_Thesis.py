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

    orders = list(tick.get("orders") or [])
    trades = list(tick.get("trades") or [])
    sleeves = list(tick.get("sleeves") or [])
    fee_legs = tick.get("fee_legs") or {}

    if orders or trades or sleeves:
        st.markdown("#### Executed trades (live)")
        if orders:
            order_rows = []
            for o in orders:
                flow = o.get("premium_flow")
                fees = o.get("fees")
                order_rows.append(
                    {
                        "Time": o.get("time") or "—",
                        "Order": str(o.get("order_id") or "")[-8:],
                        "Status": o.get("status"),
                        "Side": o.get("side"),
                        "Contract": o.get("symbol"),
                        "Opt": o.get("option_type") or "—",
                        "Strike": o.get("strike"),
                        "Filled": o.get("filled"),
                        "Avg": o.get("avg"),
                        "Premium flow": flow,
                        "Brokerage": o.get("brokerage"),
                        "STT": o.get("stt"),
                        "Exch": o.get("exchange"),
                        "SEBI": o.get("sebi"),
                        "Stamp": o.get("stamp"),
                        "GST": o.get("gst"),
                        "Fees": fees,
                        "Tag": o.get("tag") or "—",
                        "Reason": o.get("reason") or "—",
                    }
                )
            odf = pd.DataFrame(order_rows)

            def _style_orders(df: pd.DataFrame):
                styles = pd.DataFrame("", index=df.index, columns=df.columns)

                def _pnl_color(val: object) -> str:
                    try:
                        num = float(val)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        return ""
                    if num > 0:
                        return "color: #0a7a32; font-weight: 600"
                    if num < 0:
                        return "color: #c62828; font-weight: 600"
                    return "color: #666666"

                def _side_color(val: object) -> str:
                    text = str(val or "").upper()
                    if text == "BUY":
                        return "color: #0a7a32; font-weight: 600"
                    if text == "SELL":
                        return "color: #c62828; font-weight: 600"
                    return ""

                def _status_color(val: object) -> str:
                    text = str(val or "").upper()
                    if text in {"TRADED", "COMPLETE", "FILLED"}:
                        return "color: #0a7a32; font-weight: 600"
                    if text in {"REJECTED", "CANCELLED"}:
                        return "color: #c62828; font-weight: 600"
                    if text in {"PENDING", "TRANSIT", "OPEN"}:
                        return "color: #b26a00; font-weight: 600"
                    return ""

                if "Premium flow" in df.columns:
                    styles["Premium flow"] = df["Premium flow"].map(_pnl_color)
                if "Fees" in df.columns:
                    styles["Fees"] = df["Fees"].map(
                        lambda v: "color: #c62828" if float(v or 0) > 0 else ""
                    )
                if "Side" in df.columns:
                    styles["Side"] = df["Side"].map(_side_color)
                if "Status" in df.columns:
                    styles["Status"] = df["Status"].map(_status_color)
                return styles

            st.dataframe(
                odf.style.apply(_style_orders, axis=None).format(
                    {
                        "Avg": "{:.2f}",
                        "Premium flow": "{:+.2f}",
                        "Brokerage": "{:.2f}",
                        "STT": "{:.2f}",
                        "Exch": "{:.2f}",
                        "SEBI": "{:.2f}",
                        "Stamp": "{:.2f}",
                        "GST": "{:.2f}",
                        "Fees": "{:.2f}",
                    },
                    na_rep="—",
                ),
                use_container_width=True,
                hide_index=True,
                height=min(420, 48 + 36 * max(len(odf), 1)),
            )
            if fee_legs:
                f1, f2, f3, f4, f5, f6, f7 = st.columns(7)
                f1.metric("Brokerage", f"₹{float(fee_legs.get('brokerage') or 0):,.2f}")
                f2.metric("STT", f"₹{float(fee_legs.get('stt') or 0):,.2f}")
                f3.metric("Exchange", f"₹{float(fee_legs.get('exchange') or 0):,.2f}")
                f4.metric("SEBI", f"₹{float(fee_legs.get('sebi') or 0):,.2f}")
                f5.metric("Stamp", f"₹{float(fee_legs.get('stamp') or 0):,.2f}")
                f6.metric("GST", f"₹{float(fee_legs.get('gst') or 0):,.2f}")
                f7.metric("Fees total", f"₹{float(fee_legs.get('total') or 0):,.2f}")
            st.caption(
                "Premium flow: SELL credits green · BUY debits red. "
                "Fees are NSE options proxies (brokerage+STT+exchange+SEBI+stamp+GST). "
                "Reason maps hunt / TP / trail / stop tags + sleeve book."
            )

        if trades:
            with st.expander("Position roll-up (net by contract)", expanded=False):
                pos_rows = []
                for t in trades:
                    pos_rows.append(
                        {
                            "Status": t.get("status"),
                            "Contract": t.get("symbol"),
                            "Side": t.get("option_type") or "—",
                            "Strike": t.get("strike"),
                            "Net qty": t.get("qty"),
                            "Entry": t.get("entry"),
                            "LTP": t.get("ltp"),
                            "Realized": t.get("realized"),
                            "Unrealized": t.get("unrealized"),
                            "P&L": t.get("pnl"),
                        }
                    )
                pdf = pd.DataFrame(pos_rows)

                def _style_pos(df: pd.DataFrame):
                    styles = pd.DataFrame("", index=df.index, columns=df.columns)

                    def _c(val: object) -> str:
                        try:
                            num = float(val)  # type: ignore[arg-type]
                        except (TypeError, ValueError):
                            return ""
                        if num > 0:
                            return "color: #0a7a32; font-weight: 600"
                        if num < 0:
                            return "color: #c62828; font-weight: 600"
                        return ""

                    for col in ("Realized", "Unrealized", "P&L"):
                        if col in df.columns:
                            styles[col] = df[col].map(_c)
                    return styles

                st.dataframe(
                    pdf.style.apply(_style_pos, axis=None).format(
                        {
                            "Entry": "{:.2f}",
                            "LTP": "{:.2f}",
                            "Realized": "{:+.2f}",
                            "Unrealized": "{:+.2f}",
                            "P&L": "{:+.2f}",
                        },
                        na_rep="—",
                    ),
                    use_container_width=True,
                    hide_index=True,
                )

        if sleeves:
            with st.expander("Tactical sleeves / stops / targets", expanded=False):
                st.dataframe(pd.DataFrame(sleeves), use_container_width=True, hide_index=True)
    else:
        st.caption("No day trades booked yet — waiting for tactical fills.")

    pcr = tick.get("pcr")
    atm = tick.get("atm")
    err = tick.get("pnl_error")
    st.caption(
        f"Chase **{primary}** · gross ₹"
        f"{'—' if achieved_gross is None else f'{achieved_gross:+,.0f}'} · "
        f"nett after fees ₹"
        f"{'—' if achieved_nett is None else f'{achieved_nett:+,.0f}'} · "
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
