"""Agentic Trade — live desk + conversation (latest first)."""

from __future__ import annotations

import html
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
    chat_role,
    load_agentic_feed,
    load_live_desk_context,
    publish_operator_turn,
)
from dashboard.components.console_runtime import (
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
  font-size: clamp(1.8rem, 3vw, 2.4rem);
  font-weight: 650;
  letter-spacing: -0.03em;
  margin: 0;
  line-height: 1.1;
}
.live-desk {
  margin: 0.55rem 0 1rem 0;
  padding: 1.15rem 1.25rem 1.25rem;
  border: 1px solid #C9D4DE;
  background:
    radial-gradient(120% 80% at 0% 0%, #E8F3EE 0%, transparent 55%),
    linear-gradient(155deg, #F4F7FA 0%, #EEF2F6 48%, #F7F3EC 100%);
}
.live-kicker {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 0.72rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #5B6B7A;
  margin-bottom: 0.35rem;
}
.live-stance {
  font-family: "Fraunces", Georgia, serif;
  font-size: clamp(1.85rem, 3.2vw, 2.6rem);
  font-weight: 700;
  letter-spacing: -0.02em;
  line-height: 1.05;
  margin: 0 0 0.35rem 0;
}
.live-meta {
  font-size: 0.92rem;
  color: #334155;
  margin-bottom: 0.85rem;
}
.live-grid {
  display: grid;
  grid-template-columns: 7.5rem 1fr;
  gap: 0.85rem 1.1rem;
  align-items: start;
}
.live-conf {
  border-right: 1px solid #D0D9E2;
  padding-right: 0.9rem;
}
.live-conf .num {
  font-family: "Fraunces", Georgia, serif;
  font-size: 2.35rem;
  font-weight: 700;
  line-height: 1;
  color: #0F3D2E;
}
.live-conf .lbl {
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.68rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: #5B6B7A;
  margin-top: 0.25rem;
}
.live-block-label {
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.68rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: #5B6B7A;
  margin-bottom: 0.2rem;
}
.live-block-body {
  font-size: 0.98rem;
  line-height: 1.45;
  color: #1E293B;
}
.live-tactical {
  margin-top: 0.9rem;
  padding-top: 0.75rem;
  border-top: 1px solid #D0D9E2;
  font-size: 0.88rem;
  color: #334155;
}
.status-ACTIVE { color: #0a7a32; font-weight: 650; }
.status-EXECUTED { color: #0a7a32; font-weight: 650; }
.status-PROPOSED { color: #b26a00; font-weight: 650; }
.status-FAILED, .status-REJECTED { color: #c62828; font-weight: 650; }
@media (max-width: 720px) {
  .live-grid { grid-template-columns: 1fr; }
  .live-conf { border-right: 0; border-bottom: 1px solid #D0D9E2; padding: 0 0 0.75rem 0; }
}
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


def _esc(value: object) -> str:
    text = str(value or "")
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return html.escape(text, quote=True)


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
    limit = st.slider("Chat depth", min_value=40, max_value=300, value=100, step=20)
    if st.button("Pulse desk", use_container_width=True):
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
            st.success("Desk pulsed.")
        except Exception as exc:
            logger.exception("Agentic Trade pulse failed")
            st.error(f"Pulse failed: {exc}")
        st.rerun()


def _render_live_desk(ctx: dict) -> None:
    st.markdown('<p class="agentic-brand">Agentic Trade</p>', unsafe_allow_html=True)

    stance = _esc(ctx.get("stance") or "—")
    regime = _esc(ctx.get("regime") or "—")
    sentiment = _esc(ctx.get("sentiment") or "—")
    symbol_u = _esc(ctx.get("symbol") or symbol)
    conf = ctx.get("confidence")
    conf_s = f"{float(conf) * 100:.0f}%" if conf is not None else "—"
    conviction = _esc(ctx.get("conviction") or "No conviction thesis yet.")
    learning = _esc(ctx.get("learning") or "No learning logged yet.")
    grade = _esc(ctx.get("current_grade") or "NO_DATA")
    primary = _esc(ctx.get("primary_target") or "OKAY")
    nett = ctx.get("nett_pnl")
    target = ctx.get("target_nett")
    progress = ctx.get("progress_pct")
    nett_s = f"₹{float(nett):+,.0f}" if isinstance(nett, (int, float)) else "—"
    target_s = f"₹{float(target):+,.0f}" if isinstance(target, (int, float)) else "—"
    prog_s = f"{float(progress):.0f}%" if isinstance(progress, (int, float)) else "—"
    manage = ctx.get("manage") or []
    bits = ctx.get("manage_bits") or []
    tactical_line = (
        _esc(" · ".join(bits))
        if manage
        else "Flat — no open sleeves · tactical idle"
    )
    entries = "entries on" if ctx.get("allow_new") else "entries off"

    st.markdown(
        f"""
        <div class="live-desk">
          <div class="live-kicker">Live desk · {symbol_u} · {_esc(clock.phase)} · {_esc(clock.label)}</div>
          <div class="live-stance">{stance}</div>
          <div class="live-meta">
            {regime} / {sentiment} · chase <b>{primary}</b> · day <b>{grade}</b>
            · nett {nett_s} / target {target_s} ({prog_s}) · {entries}
          </div>
          <div class="live-grid">
            <div class="live-conf">
              <div class="num">{_esc(conf_s)}</div>
              <div class="lbl">Confidence</div>
            </div>
            <div>
              <div style="margin-bottom:0.75rem">
                <div class="live-block-label">Conviction thesis</div>
                <div class="live-block-body">{conviction}</div>
              </div>
              <div>
                <div class="live-block-label">Learning from today</div>
                <div class="live-block-body">{learning}</div>
              </div>
            </div>
          </div>
          <div class="live-tactical"><b>Tactical</b> · {tactical_line}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_chat(events: list[dict]) -> None:
    st.markdown("##### Desk chat")
    st.caption("Latest first · agents + your turns")
    if not events:
        st.info("No desk talk yet — pulse from the sidebar, or type below.")
        return
    # Newest on top
    for ev in reversed(events):
        agent = str(ev.get("agent") or "system")
        ts = format_ist(ev.get("timestamp"))
        status = str(ev.get("status") or "")
        kind = str(ev.get("kind") or "chat")
        with st.chat_message(chat_role(agent), avatar=agent_avatar(agent)):
            meta = f"`{ts}` · **{agent}**"
            if kind != "chat":
                meta = f"{meta} · `{kind}`"
            if status:
                meta = f'{meta} · <span class="status-{status}">{status}</span>'
            st.markdown(meta, unsafe_allow_html=True)
            st.markdown(str(ev.get("message") or ""))


def _render_body() -> None:
    ctx = load_live_desk_context(client, symbol=symbol)
    _render_live_desk(ctx)
    events, _sources = load_agentic_feed(client, limit=int(limit))
    _render_chat(events)


if auto:

    @st.fragment(run_every=timedelta(seconds=int(refresh_sec)))
    def _live() -> None:
        _render_body()

    _live()
else:
    _render_body()

prompt = st.chat_input("Add to the desk — ask about conviction, confidence, or today’s learning…")
if prompt:
    publish_operator_turn(prompt, redis_client=client, symbol=symbol)
    st.rerun()
