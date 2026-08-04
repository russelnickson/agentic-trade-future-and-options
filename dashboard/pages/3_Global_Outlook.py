"""Global Outlook — overseas markers, India proxies, FII/DII, open bias."""

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
from dashboard.components.console_runtime import session_clock
from dashboard.secrets_store import apply_secrets_to_environ
from dashboard.timefmt import format_ist
from services.global_outlook import (
    load_fii_dii_table,
    load_markers_table,
    load_snapshot,
    refresh_global_outlook,
)


def _style_chg_pct(df: pd.DataFrame):
    """Color chg % green when up, red when down."""
    if "chg %" not in df.columns:
        return df

    def _color(val: object) -> str:
        try:
            num = float(val)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return ""
        if num > 0:
            return "color: #0a7a32; font-weight: 600"
        if num < 0:
            return "color: #c62828; font-weight: 600"
        return "color: #666666"

    styler = df.style.map(_color, subset=["chg %"])
    styler = styler.format(
        {
            "last": "{:.2f}",
            "chg %": "{:+.2f}",
        },
        na_rep="—",
    )
    return styler


def _render_marker_table(df: pd.DataFrame) -> None:
    cols = [c for c in ["symbol", "name", "last", "change_pct", "source", "why", "asof"] if c in df.columns]
    show = df[cols].copy()
    show = show.rename(columns={"change_pct": "chg %", "asof": "as of"})
    if "chg %" in show.columns:
        show["chg %"] = pd.to_numeric(show["chg %"], errors="coerce")
    if "last" in show.columns:
        show["last"] = pd.to_numeric(show["last"], errors="coerce")
    if "as of" in show.columns:
        show["as of"] = show["as of"].map(
            lambda v: format_ist(v) if v is not None and str(v).strip() else "—"
        )
    st.dataframe(
        _style_chg_pct(show),
        use_container_width=True,
        hide_index=True,
        column_config={
            "chg %": st.column_config.NumberColumn("chg %", help="Green = up · Red = down"),
            "as of": st.column_config.TextColumn("as of"),
        },
    )


st.set_page_config(
    page_title="Global Outlook · Trade Console",
    page_icon="🌐",
    layout="wide",
    initial_sidebar_state="expanded",
)

require_login()
apply_secrets_to_environ()
render_sidebar_profile()

clock = session_clock()
live_desk = clock.is_live_desk or clock.phase in {"PRE_OPEN", "OPEN", "CLOSING"}

st.title("Global Outlook")
st.caption(
    "DhanHQ India / GIFT / MCX / USDINR markers plus NSE FII/DII — "
    "composite open bias prior for NIFTY (no Yahoo)."
)

with st.sidebar:
    st.subheader("Global Outlook")
    auto_live = st.toggle("Live refresh (market hours)", value=live_desk)
    tick_sec = st.select_slider("Tick seconds", options=[10, 30, 60], value=30)
    if st.button("Refresh all markers", type="primary", use_container_width=True):
        with st.spinner("Pulling Dhan indices · MCX · USDINR · NSE FII/DII…"):
            try:
                snap = refresh_global_outlook()
                st.success(f"Updated · bias **{snap.bias}** (score {snap.score:+.2f})")
            except Exception as exc:
                st.error(f"Refresh failed: {exc}")
        st.rerun()
    st.caption(
        "Sources: Dhan (NIFTY · Sensex · GIFT · India VIX · MCX · USDINR FUT), "
        "NSE FII/DII cash. Cached under `data/global/`."
    )

if auto_live:

    @st.fragment(run_every=timedelta(seconds=int(tick_sec)))
    def _live_pulse() -> None:
        c = session_clock()
        s = load_snapshot()
        st.caption(
            f"Live · {c.phase} · {c.now_ist}"
            + (f" · bias **{(s.bias if s else '—')}**" if s else " · no snapshot")
        )

    _live_pulse()

snap = load_snapshot()
markers = load_markers_table()
fii = load_fii_dii_table()

if snap is None and markers.empty:
    st.warning("No snapshot yet. Click **Refresh all markers** in the sidebar.")
    st.stop()

# ----- Bias hero -----
bias = (snap.bias if snap else "—") or "—"
score = snap.score if snap else 0.0
summary = snap.summary if snap else ""

if "BULL" in bias.upper():
    st.success(f"**Open bias: {bias}** · composite score {score:+.2f}")
elif "BEAR" in bias.upper():
    st.error(f"**Open bias: {bias}** · composite score {score:+.2f}")
else:
    st.warning(f"**Open bias: {bias}** · composite score {score:+.2f}")

st.write(summary)
if snap:
    st.caption(f"Snapshot as of {format_ist(snap.asof, seconds=True)}")

st.divider()

# ----- Factor board -----
st.subheader("Prediction factors")
factors = (snap.factors if snap else []) or []
if factors:
    cols = st.columns(min(4, len(factors)))
    for i, f in enumerate(factors[:8]):
        with cols[i % len(cols)]:
            sig = str(f.get("signal") or "n/a")
            label = str(f.get("factor") or "")
            detail = str(f.get("detail") or "")
            if sig == "bullish":
                st.metric(label, "▲ Bullish", delta=detail[:48] or None)
            elif sig == "bearish":
                st.metric(label, "▼ Bearish", delta=detail[:48] or None, delta_color="inverse")
            elif sig == "stabilizing":
                st.metric(label, "◆ Absorb", delta=detail[:48] or None)
            else:
                st.metric(label, sig.title(), delta=detail[:48] or None)

    with st.expander("All factors (weighted)", expanded=False):
        st.dataframe(
            pd.DataFrame(factors)[["factor", "signal", "weight", "detail"]],
            use_container_width=True,
            hide_index=True,
        )
else:
    st.info("No factors computed yet.")

st.divider()

# ----- Markers by region -----
st.subheader("Global markers & indices")
if markers.empty and snap and snap.markers:
    markers = pd.DataFrame(snap.markers)

if not markers.empty:
    view = markers.copy()
    for col in ("last", "prev", "change_pct"):
        if col in view.columns:
            view[col] = pd.to_numeric(view[col], errors="coerce")

    region_order = ["ASIA", "INDIA", "CMDTY", "FX"]
    tabs = st.tabs([r for r in region_order if r in set(view["region"].astype(str))] + ["All"])
    shown = [r for r in region_order if r in set(view["region"].astype(str))]
    for tab, region in zip(tabs[:-1], shown):
        with tab:
            sub = view[view["region"] == region].copy()
            _render_marker_table(sub)
    with tabs[-1]:
        _render_marker_table(view)
else:
    st.info("No marker rows cached.")

st.divider()

# ----- FII / DII -----
st.subheader("FII / DII cash flows")
st.caption("NSE institutional activity (₹ Cr). Fresh day appended on each refresh.")
if fii.empty and snap and snap.fii_dii:
    fii = pd.DataFrame(snap.fii_dii)

if not fii.empty:
    fii_view = fii.copy()
    for col in ("buy_cr", "sell_cr", "net_cr"):
        if col in fii_view.columns:
            fii_view[col] = pd.to_numeric(fii_view[col], errors="coerce")

    latest = fii_view["trade_date"].max()
    day = fii_view[fii_view["trade_date"] == latest]
    m1, m2, m3 = st.columns(3)
    fii_net = float(
        day[day["category"].astype(str).str.contains("FII", case=False, na=False)]["net_cr"].sum()
    )
    dii_net = float(
        day[day["category"].astype(str).str.contains("DII", case=False, na=False)]["net_cr"].sum()
    )
    with m1:
        st.metric("Latest trade date", format_ist(latest, with_time=False))
    with m2:
        st.metric("FII net (₹ Cr)", f"{fii_net:+,.0f}")
    with m3:
        st.metric("DII net (₹ Cr)", f"{dii_net:+,.0f}")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Latest session**")
        st.dataframe(
            day[["trade_date", "category", "buy_cr", "sell_cr", "net_cr"]],
            use_container_width=True,
            hide_index=True,
        )
    with c2:
        st.markdown("**History (local cache)**")
        pivot = (
            fii_view.pivot_table(
                index="trade_date",
                columns="category",
                values="net_cr",
                aggfunc="sum",
            )
            .reset_index()
            .sort_values("trade_date", ascending=False)
        )
        st.dataframe(pivot, use_container_width=True, hide_index=True)
        if len(pivot) >= 2:
            chart = fii_view.copy()
            chart["trade_date"] = pd.to_datetime(chart["trade_date"], errors="coerce")
            chart = chart.dropna(subset=["trade_date"]).sort_values("trade_date")
            wide = chart.pivot_table(
                index="trade_date", columns="category", values="net_cr", aggfunc="sum"
            )
            st.line_chart(wide)
else:
    st.info("No FII/DII rows yet — refresh after NSE has published the latest session.")

st.divider()
st.markdown(
    """
**How to use this page**
1. Refresh after GIFT move / before India open (≈ 08:00–09:10 IST).
2. Treat composite bias as an **open-auction prior**, not a trade signal.
3. Confirm with live India VIX, PCR, and first 15-minute range on the main dashboard.
4. All price markers are **DhanHQ** (paid). Hang Seng / Nasdaq / US VIX are not on Dhan —
   GIFT Nifty + India VIX cover that overnight / fear role.
"""
)
