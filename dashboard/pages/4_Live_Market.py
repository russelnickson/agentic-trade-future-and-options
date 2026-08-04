"""Live Market — curated direct-source voices (no generated quotes)."""

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
from services.live_market_voices import (
    balance_india_global,
    credibility_legend,
    filter_horizon,
    load_snapshot,
    load_voices,
    refresh_live_market,
)

st.set_page_config(
    page_title="Live Market · Trade Console",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

require_login()
apply_secrets_to_environ()
render_sidebar_profile()

clock = session_clock()
live_desk = clock.is_live_desk or clock.phase in {"PRE_OPEN", "OPEN", "CLOSING"}

st.title("Live Market")
st.caption(
    "Curated voices from regulators, policymakers, Nifty 100 exchange filings, "
    "and niche economist/central-bank speeches — **direct sources only**. "
    "Target mix ~80% India / 20% global. No paraphrased or AI-generated quotes."
)

with st.sidebar:
    st.subheader("Live Market")
    auto_live = st.toggle("Live refresh (market hours)", value=live_desk)
    tick_sec = st.select_slider("Tick seconds", options=[15, 30, 60], value=30)
    horizon = st.radio(
        "Horizon",
        ["day", "week", "month", "quarter", "year"],
        index=1,
        format_func=lambda x: x.title(),
    )
    voice_filter = st.multiselect(
        "Voice class",
        ["regulator", "policymaker", "company", "executive", "economist"],
        default=["regulator", "policymaker", "company", "executive", "economist"],
    )
    min_cred = st.slider("Min credibility", 0.85, 0.99, 0.90, 0.01)
    limit = st.slider("Max items shown", 20, 120, 60, 10)
    if st.button("Refresh direct sources", type="primary", use_container_width=True):
        with st.spinner("Pulling RBI · NITI · PIB · NSE Nifty 100 · Fed · ECB · BoE · BIS…"):
            try:
                # Quarter lookback for company filings; year horizon still filled by RSS archives
                snap = refresh_live_market(nse_lookback_days=92)
                st.success(
                    f"Updated · week India share "
                    f"{(snap.india_share_latest_week or 0)*100:.0f}% · "
                    f"sources ok "
                    f"{sum(1 for h in snap.source_health if h.get('status')=='ok')}"
                    f"/{len(snap.source_health)}"
                )
            except Exception as exc:
                st.error(f"Refresh failed: {exc}")
        st.rerun()
    st.caption("Caches: `data/live_market/`. Failed feeds are listed — never invent filled.")

if auto_live:

    @st.fragment(run_every=timedelta(seconds=int(tick_sec)))
    def _live_pulse() -> None:
        c = session_clock()
        s = load_snapshot()
        st.caption(
            f"Live · {c.phase} · {c.now_ist}"
            + (
                f" · snapshot {format_ist(s.asof, seconds=True)}"
                if s
                else " · no snapshot yet"
            )
        )

    _live_pulse()

snap = load_snapshot()
voices = load_voices()

st.info(
    (snap.policy if snap else None)
    or "No snapshot yet. Click **Refresh direct sources** to pull primary feeds."
)

if snap:
    c1, c2, c3, c4, c5 = st.columns(5)
    for col, key in zip([c1, c2, c3, c4, c5], ["day", "week", "month", "quarter", "year"]):
        with col:
            st.metric(key.title(), snap.counts_by_horizon.get(key, 0))
    st.caption(
        f"Snapshot {format_ist(snap.asof, seconds=True)}"
        + (
            f" · latest-week India share {(snap.india_share_latest_week or 0)*100:.0f}%"
            if snap.india_share_latest_week is not None
            else ""
        )
    )

if voices.empty:
    st.warning("No voice rows cached yet.")
    st.stop()

filtered = filter_horizon(voices, horizon)
if voice_filter:
    filtered = filtered[filtered["voice_class"].isin(voice_filter)]
filtered = filtered[filtered["credibility"] >= float(min_cred)]
shown = balance_india_global(filtered, limit=int(limit))

# ----- Mix meters -----
m1, m2, m3 = st.columns(3)
with m1:
    india_n = int((shown["region"] == "INDIA").sum()) if not shown.empty else 0
    glob_n = int((shown["region"] == "GLOBAL").sum()) if not shown.empty else 0
    total = max(india_n + glob_n, 1)
    st.metric("Shown · India", f"{india_n} ({india_n/total*100:.0f}%)")
with m2:
    st.metric("Shown · Global", f"{glob_n} ({glob_n/total*100:.0f}%)")
with m3:
    avg_cred = float(shown["credibility"].mean()) if not shown.empty else 0.0
    st.metric("Avg credibility", f"{avg_cred:.3f}")

st.divider()

# ----- Class tabs -----
classes = ["all"] + [c for c in ["regulator", "policymaker", "company", "executive", "economist"] if c in set(shown.get("voice_class", pd.Series(dtype=str)))]
tabs = st.tabs([c.replace("_", " ").title() for c in classes])


def _render_cards(df: pd.DataFrame) -> None:
    if df.empty:
        st.caption("No direct-source items in this slice.")
        return
    for _, row in df.iterrows():
        pub = format_ist(row.get("published_at"))
        cred = float(row.get("credibility") or 0)
        title = str(row.get("title") or "")
        summary = str(row.get("summary") or "").strip()
        url = str(row.get("url") or "")
        issuer = str(row.get("speaker_or_issuer") or "")
        src = str(row.get("source_name") or "")
        region = str(row.get("region") or "")
        vclass = str(row.get("voice_class") or "")
        symbol = str(row.get("symbol") or "")
        badge = f"`{region}` · `{vclass}` · cred **{cred:.2f}**"
        if symbol:
            badge += f" · `{symbol}`"
        st.markdown(f"**{title}**")
        st.caption(f"{badge} · {issuer} · {pub} · {src}")
        if summary and summary != title:
            st.write(summary[:600] + ("…" if len(summary) > 600 else ""))
        if url:
            st.markdown(f"[Open primary source]({url})")
        st.divider()


with tabs[0]:
    _render_cards(shown)

for tab, cls in zip(tabs[1:], classes[1:]):
    with tab:
        _render_cards(shown[shown["voice_class"] == cls])

st.subheader("Source health")
if snap and snap.source_health:
    health_df = pd.DataFrame(snap.source_health)[
        ["source_id", "name", "region", "status", "items", "credibility", "error", "url"]
    ]
    st.dataframe(health_df, use_container_width=True, hide_index=True)
else:
    st.caption("No health report yet.")

with st.expander("Credibility legend (source-tier, not truth score)", expanded=False):
    st.dataframe(pd.DataFrame(credibility_legend()), use_container_width=True, hide_index=True)
    st.markdown(
        """
**Anti-hallucination rules baked into this module**
- Only issuer / exchange URLs from a curated whitelist
- Titles and summaries copied verbatim from the feed or NSE filing text
- Undated items are dropped (never assigned a fake timestamp)
- Non–Nifty 100 filings are dropped (never labeled as top-100 without proof)
- Failed sources appear in Source health with the error — slots are not filled with synthetic news
"""
    )
