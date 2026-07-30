"""Agentic Trade — all agent outputs in one conversation."""

from __future__ import annotations

import logging
import sys
from datetime import timedelta
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dashboard.auth import render_sidebar_profile, require_login
from dashboard.components.agentic_trade import (
    agent_avatar,
    load_agentic_feed,
    load_live_desk_context,
    publish_operator_turn,
)
from dashboard.components.console_runtime import (
    AGENT_FLEET,
    build_agent_statuses,
    classify_day_outcome,
    session_clock,
    sync_agent_briefing,
)
from dashboard.components.risk_controls import is_trading_disabled, load_terminal_controls
from dashboard.secrets_store import apply_secrets_to_environ
from dashboard.timefmt import format_ist
from database.redis_client import RedisClient

logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Agentic Trade · Desk",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)

require_login()
apply_secrets_to_environ()
render_sidebar_profile()

st.markdown(
    """
<style>
.agentic-brand {
  font-family: "Fraunces", "Iowan Old Style", Georgia, serif;
  font-size: 2.1rem;
  font-weight: 650;
  letter-spacing: -0.02em;
  margin: 0 0 0.15rem 0;
}
.agentic-sub {
  color: #4B5563;
  font-size: 0.95rem;
  margin-bottom: 0.75rem;
}
.live-strip {
  border: 1px solid #D5DCE3;
  background: linear-gradient(120deg, #F7FAFC 0%, #EEF4F1 55%, #F8F5F0 100%);
  padding: 0.75rem 1rem;
  margin-bottom: 0.85rem;
}
.live-strip code { font-size: 0.78rem; }
.status-ACTIVE { color: #0a7a32; font-weight: 650; }
.status-EXECUTED { color: #0a7a32; font-weight: 650; }
.status-PROPOSED { color: #b26a00; font-weight: 650; }
.status-FAILED, .status-REJECTED { color: #c62828; font-weight: 650; }
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_resource
def _redis() -> RedisClient | None:
    try:
        client = RedisClient.from_settings()
        client.ping()
        return client
    except Exception:
        return None


client = _redis()
clock = session_clock()

with st.sidebar:
    st.subheader("Agentic Trade")
    symbol = st.selectbox("Underlying", ["NIFTY", "BANKNIFTY"])
    live_desk = clock.is_live_desk or clock.phase in {"PRE_OPEN", "OPEN", "CLOSING"}
    auto = st.toggle("Live refresh (market hours)", value=live_desk)
    refresh_sec = st.select_slider(
        "Interval", options=[2, 5, 10, 30], value=5 if live_desk else 10
    )
    limit = st.slider("Feed depth", min_value=40, max_value=300, value=120, step=20)
    show_decisions = st.toggle("Include decision cards in chat", value=True)
    show_insights = st.toggle("Include insights in chat", value=True)
    if st.button("Pulse desk agents", use_container_width=True):
        try:
            controls = load_terminal_controls(client) if client is not None else {}
            day = classify_day_outcome(None)
            disabled = is_trading_disabled(controls) if controls else False
            statuses = build_agent_statuses(
                client=client,
                symbol=symbol,
                controls=controls,
                day=day,
                trading_disabled=disabled,
            )
            sync_agent_briefing(
                client=client,
                symbol=symbol,
                controls=controls,
                day=day,
                statuses=statuses,
                force=True,
            )
            st.success("Fleet pulsed into the conversation.")
        except Exception as exc:
            logger.exception("Agentic Trade pulse failed")
            st.error(f"Pulse failed: {exc}")
        st.rerun()
    st.caption(
        "Streams: `agent:conversations` · `agent:decisions` · `agent:insights` · "
        "`agent:tactical:state` · `agent:strategy:directive`"
    )


def _render_header(ctx: dict) -> None:
    st.markdown('<p class="agentic-brand">Agentic Trade</p>', unsafe_allow_html=True)
    st.markdown(
        f'<p class="agentic-sub">{clock.phase} · {clock.label} — '
        "one place for Scout, Voices, Research, Thesis, Risk, Trade, Strategic & Tactical. "
        "Add your own turn; agents answer from live desk state.</p>",
        unsafe_allow_html=True,
    )
    manage = ctx.get("manage") or []
    bits = ctx.get("manage_bits") or ["Flat — no open sleeves"]
    st.markdown(
        f"""
        <div class="live-strip">
          <div><b>Live desk</b> · {ctx.get('symbol')} · strategic
          <code>{ctx.get('stance')}</code> {ctx.get('regime')}/{ctx.get('sentiment')}
          · entries {'on' if ctx.get('allow_new') else 'off'}</div>
          <div style="margin-top:0.35rem">Tactical:
          {' · '.join(bits) if manage else 'flat / idle'}
          </div>
          <div style="margin-top:0.35rem;font-size:0.85rem;color:#4B5563">
          MANAGE rows = monitoring (ACTIVE). Broker exits only when LTP hits TP / trail / stop.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    cols = st.columns(len(AGENT_FLEET))
    for col, spec in zip(cols, AGENT_FLEET):
        with col:
            st.markdown(f"**{spec.name}**")
            st.caption(spec.mandate[:72] + ("…" if len(spec.mandate) > 72 else ""))


def _render_chat(events: list[dict], *, show_decisions: bool, show_insights: bool) -> None:
    if not events:
        st.info("No desk talk yet — pulse agents from the sidebar, or type below.")
        return
    for ev in events:
        kind = str(ev.get("kind") or "chat")
        if kind == "decision" and not show_decisions:
            continue
        if kind == "insight" and not show_insights:
            continue
        agent = str(ev.get("agent") or "system")
        ts = format_ist(ev.get("timestamp"))
        status = str(ev.get("status") or "")
        with st.chat_message(agent if agent != "user" else "user", avatar=agent_avatar(agent)):
            meta = f"`{ts}` · **{agent}**"
            if kind != "chat":
                meta = f"{meta} · `{kind}`"
            if status:
                meta = f'{meta} · <span class="status-{status}">{status}</span>'
            st.markdown(meta, unsafe_allow_html=True)
            st.markdown(str(ev.get("message") or ""))


def _render_body() -> None:
    ctx = load_live_desk_context(client, symbol=symbol)
    _render_header(ctx)
    events, sources = load_agentic_feed(client, limit=int(limit))
    st.caption(
        "Sources: "
        + " · ".join(f"`{k}`={v}" for k, v in sources.items())
    )
    _render_chat(events, show_decisions=show_decisions, show_insights=show_insights)

    # Raw tactical JSON for operators debugging MANAGE vs fills
    with st.expander("Tactical / directive JSON", expanded=False):
        left, right = st.columns(2)
        with left:
            st.markdown("**Tactical state**")
            st.json(ctx.get("tactical") or {})
        with right:
            st.markdown("**Strategic directive**")
            st.json(ctx.get("directive") or {})


if auto:

    @st.fragment(run_every=timedelta(seconds=int(refresh_sec)))
    def _live() -> None:
        _render_body()

    _live()
else:
    _render_body()

prompt = st.chat_input("Talk to the desk — ask why MANAGE is waiting, status, or note a plan…")
if prompt:
    publish_operator_turn(prompt, redis_client=client, symbol=symbol)
    st.rerun()
