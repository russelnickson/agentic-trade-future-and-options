"""Agents — live multi-agent discussion and Trade decisions."""

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
    append_conversation,
    append_decision,
    load_conversations,
    load_decisions,
    seed_sample_session,
)
from dashboard.components.console_runtime import AGENT_FLEET, session_clock
from dashboard.secrets_store import apply_secrets_to_environ
from dashboard.timefmt import format_ist
from database.redis_client import RedisClient

st.set_page_config(
    page_title="Agents · Trade Console",
    page_icon="◈",
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


def _agent_badge(agent: str) -> str:
    colors = {
        "orchestrator": "◆",
        "scout": "◆",
        "voices": "◆",
        "researcher": "◆",
        "thesis": "◆",
        "risk": "◆",
        "execution": "◆",
        "system": "◇",
        "user": "●",
    }
    return f"{colors.get(str(agent).lower(), '·')} **{agent}**"


client = _redis()
clock = session_clock()

st.title("Agents")
st.caption(
    f"{clock.phase} · {clock.label} — Scout, Voices, Research, and Risk debate facts; "
    "**Trade** decides so the day can close decent."
)

with st.sidebar:
    st.subheader("Agents")
    auto = st.toggle("Auto-refresh", value=True)
    refresh_sec = st.select_slider("Interval", options=[2, 5, 10, 30], value=5)
    limit = st.slider("Rows", min_value=20, max_value=300, value=100, step=10)
    if st.button("Seed sample session", use_container_width=True):
        seed_sample_session(client)
        st.success("Sample conversation + decisions written.")
        st.rerun()
    st.caption("Streams: `agent:conversations` / `agent:decisions`.")

st.markdown("##### Fleet")
fc = st.columns(len(AGENT_FLEET))
for col, spec in zip(fc, AGENT_FLEET):
    with col:
        st.markdown(f"**{spec.name}**")
        st.caption(spec.mandate)


def _render_body() -> None:
    conversations, c_src = load_conversations(client, limit=limit)
    decisions, d_src = load_decisions(client, limit=limit)

    left, right = st.columns([1.2, 1])
    with left:
        st.subheader("Discussion")
        st.caption(f"Source: `{c_src}`")
        if not conversations:
            st.info("No turns yet — open **Console** home and pulse agents, or seed a sample.")
        else:
            for row in conversations[:limit]:
                ts = format_ist(row.get("timestamp"))
                st.markdown(
                    f"{_agent_badge(str(row.get('agent') or ''))} · `{ts}`  \n"
                    f"{row.get('message') or ''}"
                )
                st.divider()
    with right:
        st.subheader("Trade decisions")
        st.caption(f"Source: `{d_src}`")
        if not decisions:
            st.info("No decisions logged.")
        else:
            rows = []
            for d in decisions[:limit]:
                rows.append(
                    {
                        "When": format_ist(d.get("timestamp")),
                        "Kind": d.get("kind"),
                        "Symbol": d.get("symbol"),
                        "Summary": d.get("summary"),
                        "Status": d.get("status"),
                        "Agent": d.get("agent"),
                        "Confidence": d.get("confidence"),
                    }
                )
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    with st.expander("Post a manual note / decision"):
        with st.form("manual_agent_note"):
            agent = st.selectbox(
                "Agent",
                ["orchestrator", "scout", "voices", "researcher", "risk", "execution", "user"],
            )
            message = st.text_area("Message")
            kind = st.selectbox(
                "If decision",
                ["", "ENTRY", "EXIT", "HEDGE", "SKIP", "SQUARE_OFF", "ADJUST", "OBSERVE"],
            )
            symbol = st.selectbox("Symbol", ["NIFTY", "BANKNIFTY"])
            submitted = st.form_submit_button("Publish")
        if submitted and message.strip():
            append_conversation(
                {"agent": agent, "role": agent if agent != "user" else "user", "message": message.strip()},
                redis_client=client,
            )
            if kind:
                append_decision(
                    {
                        "agent": agent if agent != "user" else "execution",
                        "kind": kind,
                        "symbol": symbol,
                        "summary": message.strip()[:200],
                        "rationale": "Manual console note",
                        "status": "PROPOSED",
                    },
                    redis_client=client,
                )
            st.success("Published")
            st.rerun()


if auto:

    @st.fragment(run_every=timedelta(seconds=int(refresh_sec)))
    def _live() -> None:
        _render_body()

    _live()
else:
    _render_body()
