"""Insights — top backtested day strategies + historic context."""

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
from dashboard.components.agent_journal import (
    build_insight_from_market,
    load_insights,
    load_strategy_snapshot,
    seed_sample_session,
)
from dashboard.components.console_runtime import session_clock
from dashboard.secrets_store import apply_secrets_to_environ
from dashboard.timefmt import format_ist
from database.redis_client import RedisClient
from services.day_strategy_backtest import load_strategies, refresh_top_strategies

st.set_page_config(
    page_title="Insights · Trade Console",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded",
)

require_login()
apply_secrets_to_environ()
render_sidebar_profile()


@st.cache_resource
def _redis() -> RedisClient | None:
    try:
        client = RedisClient.from_settings()
        client.ping()
        return client
    except Exception:
        return None


client = _redis()

st.title("Insights")
st.caption(
    "Top **5 backtested day strategies** with confidence scores — plus historic tape "
    "and Research notes for the session."
)

with st.sidebar:
    st.subheader("Insights")
    symbol = st.selectbox("Underlying", ["NIFTY", "BANKNIFTY"])
    clock = session_clock()
    live_desk = clock.is_live_desk or clock.phase in {"PRE_OPEN", "OPEN", "CLOSING"}
    auto_live = st.toggle("Live refresh (market hours)", value=live_desk)
    tick_sec = st.select_slider("Tick seconds", options=[5, 10, 30], value=10)
    if st.button("Refresh top 5 strategies", type="primary", use_container_width=True):
        with st.spinner("Backtesting day strategies on Dhan history…"):
            payload = refresh_top_strategies(symbol, redis_client=client, top_n=5)
        st.success(
            f"Ranked {len(payload.get('strategies') or [])} strategies · "
            f"bars={payload.get('history_bars')}"
        )
        st.rerun()
    if st.button("Generate insight note", use_container_width=True):
        note = build_insight_from_market(symbol, redis_client=client)
        st.success(f"Insight saved: {note.title}")
        st.rerun()
    if st.button("Seed sample insight", use_container_width=True):
        seed_sample_session(client)
        st.success("Sample insight written.")
        st.rerun()
    st.caption(
        "Strategies: `data/insights/top_strategies_*.json` · Redis `agent:strategies:today`. "
        "OHLC proxies — confirm with live PCR/IV."
    )

strategies = load_strategies(symbol, redis_client=client)
if strategies is None:
    with st.spinner("Building initial backtest…"):
        try:
            strategies = refresh_top_strategies(symbol, redis_client=client, top_n=5)
        except Exception as exc:
            st.error(f"Backtest failed: {exc}")
            strategies = None


def _render_strategy_panel(strategies: dict | None) -> None:
    st.subheader(f"Top 5 strategies for the day · {symbol}")
    if not strategies or not strategies.get("strategies"):
        st.warning("No strategy ranking yet. Click **Refresh top 5 strategies**.")
        return
    meta1, meta2, meta3, meta4 = st.columns(4)
    meta1.metric("Last close", f"{strategies.get('last_close') or '—'}")
    vol = strategies.get("vol20")
    meta2.metric("20d vol", f"{float(vol):.1%}" if vol is not None else "—")
    meta3.metric("Trend (MA20)", "UP" if strategies.get("trend_up") else "DOWN")
    meta4.metric("History bars", f"{strategies.get('history_bars') or 0:,}")
    st.caption(
        f"As of {format_ist(strategies.get('asof'), seconds=True)} · "
        f"{strategies.get('disclaimer') or ''}"
    )


if auto_live and live_desk:

    @st.fragment(run_every=timedelta(seconds=int(tick_sec)))
    def _live_insights() -> None:
        fresh = load_strategies(symbol, redis_client=client) or strategies
        clock_now = session_clock()
        st.caption(f"Live · {clock_now.phase} · {clock_now.now_ist}")
        _render_strategy_panel(fresh)

    _live_insights()
else:
    _render_strategy_panel(strategies)

# ----- Strategy detail (table + expanders) -----
if strategies and strategies.get("strategies"):
    if strategies.get("signals_used"):
        sig = ", ".join(f"{k}={v}" for k, v in strategies["signals_used"].items())
        st.caption(f"Broker speculation soft bias used: {sig}")

    table_rows = []
    for s in strategies["strategies"]:
        table_rows.append(
            {
                "Rank": s.get("rank"),
                "Strategy": s.get("name"),
                "Confidence": s.get("confidence"),
                "Win rate": s.get("win_rate"),
                "Trades": s.get("trades"),
                "Expectancy (R)": s.get("expectancy_r"),
                "Profit factor": s.get("profit_factor"),
                "Regime win": s.get("regime_win_rate"),
                "Regime n": s.get("regime_n"),
            }
        )
    st.dataframe(
        pd.DataFrame(table_rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Confidence": st.column_config.ProgressColumn(
                "Confidence",
                min_value=0.0,
                max_value=1.0,
                format="%.0%%",
            ),
            "Win rate": st.column_config.ProgressColumn(
                "Win rate",
                min_value=0.0,
                max_value=1.0,
                format="%.0%%",
            ),
            "Regime win": st.column_config.ProgressColumn(
                "Regime win",
                min_value=0.0,
                max_value=1.0,
                format="%.0%%",
            ),
            "Expectancy (R)": st.column_config.NumberColumn(format="%+.3f"),
        },
    )

    for s in strategies["strategies"]:
        conf = float(s.get("confidence") or 0)
        with st.expander(
            f"#{s.get('rank')} · {s.get('name')} · confidence {conf:.0%}",
            expanded=s.get("rank") == 1,
        ):
            c1, c2 = st.columns([2, 1])
            with c1:
                st.markdown(f"**Structure:** {s.get('structure')}")
                st.markdown(f"**Thesis:** {s.get('thesis')}")
                st.markdown(f"**When:** {s.get('when_to_use')}")
                st.markdown(f"**Invalidate:** {s.get('invalidate')}")
                st.caption(s.get("notes") or "")
            with c2:
                st.metric("Confidence", f"{conf:.0%}")
                st.metric("Win rate", f"{float(s.get('win_rate') or 0):.0%}")
                st.metric("Trades", f"{int(s.get('trades') or 0):,}")
                st.metric("Expectancy (R)", f"{float(s.get('expectancy_r') or 0):+.3f}")
                pf = s.get("profit_factor")
                st.metric("Profit factor", f"{float(pf):.2f}" if pf is not None else "—")

st.divider()

# ----- Snapshot / archive -----
snapshot = load_strategy_snapshot(client)
insights, src = load_insights(client, limit=40)

st.subheader("Research snapshot")
if snapshot:
    c1, c2 = st.columns([2, 1])
    with c1:
        st.markdown(f"### {snapshot.get('title') or 'Outlook'}")
        st.write(snapshot.get("strategy_for_tomorrow") or "—")
        st.markdown("**Why**")
        st.write(snapshot.get("why") or "—")
    with c2:
        st.metric("Symbol", snapshot.get("symbol") or "—")
        st.metric("As of (trade date)", format_ist(snapshot.get("trade_date"), with_time=False))
        st.caption(f"Agent: `{snapshot.get('agent')}`")
    with st.expander("Supporting metrics", expanded=False):
        st.json(snapshot.get("supporting_metrics") or {})
    st.info(snapshot.get("outlook") or "—")
else:
    st.caption("No Research snapshot yet — refresh strategies or generate an insight note.")

st.divider()
st.subheader("Insight archive")
st.caption(f"Source: `{src}`")
if not insights:
    st.info("No historic insights logged.")
else:
    archive = []
    for row in insights:
        archive.append(
            {
                "Date": format_ist(row.get("trade_date"), with_time=False),
                "Symbol": row.get("symbol"),
                "Title": row.get("title"),
                "Strategy": row.get("strategy_for_tomorrow"),
                "Agent": row.get("agent"),
                "Logged": format_ist(row.get("timestamp")),
            }
        )
    st.dataframe(pd.DataFrame(archive), use_container_width=True, hide_index=True)

st.divider()
st.subheader("Historic tape (Dhan daily)")
history_root = _ROOT / "data" / "history"
parts = sorted(history_root.glob(f"{symbol.lower()}*_daily.parquet"), reverse=True)
if not parts:
    st.info(f"No history for {symbol}. Run `python services/history_downloader.py --years 5`.")
else:
    chosen = st.selectbox(
        "History file",
        parts,
        format_func=lambda p: str(p.relative_to(_ROOT)),
    )
    try:
        df = pd.read_parquet(chosen)
        st.write(f"**{len(df):,}** rows · columns: {', '.join(map(str, df.columns))}")
        show_cols = [
            c
            for c in ("time", "open", "high", "low", "close", "volume", "oi", "iv")
            if c in df.columns
        ]
        st.dataframe(df[show_cols].tail(250), use_container_width=True, hide_index=True)
        if "close" in df.columns:
            st.line_chart(
                df.set_index("time")["close"].tail(500) if "time" in df.columns else df["close"].tail(500)
            )
    except Exception as exc:
        st.error(f"Could not read parquet: {exc}")
