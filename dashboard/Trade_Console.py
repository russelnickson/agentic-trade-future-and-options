"""Trade Console — F&O Trading Console & Agentic Dashboard."""

from __future__ import annotations

import logging
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dashboard.auth import render_sidebar_profile, require_login
from dashboard.components.capital import fetch_capital, render_capital_cards
from dashboard.components.console_runtime import (
    AGENT_FLEET,
    build_agent_statuses,
    classify_day_outcome,
    recent_discussion,
    rerun_desk,
    session_clock,
    sync_agent_briefing,
)
from dashboard.components.orders import render_orders_table
from dashboard.components.positions import fetch_positions, render_positions_table
from dashboard.components.risk_controls import (
    is_trading_disabled,
    load_terminal_controls,
    render_risk_controls,
    save_terminal_controls,
)
from dashboard.secrets_store import apply_secrets_to_environ
from dashboard.timefmt import format_ist
from database.redis_client import RedisClient
from services.oi_tracker import OITracker, compute_pcr

logger = logging.getLogger(__name__)

CONSOLE_TITLE = "F&O Trading Console & Agentic Dashboard"
REFRESH_RATE_OPTIONS = {
    "2 seconds": 2,
    "5 seconds": 5,
    "10 seconds": 10,
    "30 seconds": 30,
    "Manual only": 0,
}

st.set_page_config(
    page_title="Trade Console · Live Desk",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)

apply_secrets_to_environ()

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,600;0,9..40,700;1,9..40,400&family=IBM+Plex+Mono:wght@400;500&display=swap');
html, body, [class*="css"]  {
  font-family: "DM Sans", "Segoe UI", sans-serif;
}
.console-brand {
  font-size: 1.85rem;
  font-weight: 700;
  letter-spacing: -0.03em;
  line-height: 1.15;
  margin: 0 0 0.15rem 0;
  color: #12161C;
}
.console-tag {
  font-size: 0.95rem;
  color: #3D4A57;
  margin-bottom: 1rem;
}
.console-phase {
  display: inline-block;
  padding: 0.35rem 0.75rem;
  background: linear-gradient(120deg, #E7F3EE 0%, #E8EEF6 100%);
  border-left: 3px solid #0F6E56;
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.82rem;
  margin-bottom: 0.75rem;
}
.grade-pill {
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.9rem;
  padding: 0.55rem 0.85rem;
  margin-bottom: 0.5rem;
}
.grade-PHENOMENAL { background: #D8F3DC; color: #0B3D1E; }
.grade-OKAY { background: #E4F0E8; color: #14532D; }
.grade-FLAT { background: #EEF1F4; color: #334155; }
.grade-ACCEPTABLE_LOSS { background: #FFF3D6; color: #7A4D00; }
.grade-BREACH { background: #FCE8E6; color: #7F1D1D; }
.grade-NO_DATA { background: #EEF1F4; color: #475569; }
.agent-card {
  border: 1px solid #D5DCE3;
  background: #FBFCFD;
  padding: 0.75rem 0.85rem;
  margin-bottom: 0.5rem;
  min-height: 7.5rem;
}
.agent-name { font-weight: 700; font-size: 1.05rem; }
.agent-status {
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.72rem;
  letter-spacing: 0.04em;
}
.chat-line {
  padding: 0.45rem 0;
  border-bottom: 1px solid #E8ECF0;
  font-size: 0.92rem;
}
.chat-meta {
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.72rem;
  color: #64748B;
}
div[data-testid="stSidebar"] {
  background: linear-gradient(180deg, #F7F9FB 0%, #EEF2F6 100%);
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
    except Exception as exc:
        logger.warning("Redis unavailable: %s", exc)
        return None


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _load_controls(client: RedisClient | None) -> dict:
    if client is None:
        return {
            "kill_switch": False,
            "square_off_requested": False,
            "square_off_at": None,
            "emergency_square_off": False,
            "emergency_square_off_at": None,
            "trading_disabled": False,
            "trading_disabled_until": None,
            "refresh_rate_sec": 5,
            "updated_at": None,
        }
    return load_terminal_controls(client)


def _save_controls(client: RedisClient | None, controls: dict) -> dict:
    if client is None:
        st.session_state["terminal_controls"] = controls
        return controls
    payload = save_terminal_controls(client, controls)
    st.session_state["terminal_controls"] = payload
    return payload


def _patch_controls(client: RedisClient | None, patch: dict) -> dict:
    """Merge a small patch onto the latest Redis controls (avoids stale session overwrite)."""
    base = _load_controls(client)
    return _save_controls(client, {**base, **patch})


def _day_pnl(broker: str, client: RedisClient | None) -> tuple[float | None, float | None]:
    try:
        rows, _err = fetch_positions(broker, redis_client=client)  # type: ignore[arg-type]
        pnl = float(sum(r.pnl for r in rows)) if rows else 0.0
    except Exception:
        pnl = None
    try:
        cap = fetch_capital(broker)  # type: ignore[arg-type]
        capital_ref = float(cap.available_margin or cap.total_capital or 0) or None
    except Exception:
        capital_ref = None
    return pnl, capital_ref


def _chain_frame(chain: dict) -> pd.DataFrame:
    rows: list[dict] = []
    for strike_key, sides in (chain.get("strikes") or {}).items():
        if not isinstance(sides, dict):
            continue
        ce = sides.get("CE") or {}
        pe = sides.get("PE") or {}
        rows.append(
            {
                "strike": float(strike_key),
                "ce_ltp": ce.get("ltp"),
                "ce_oi": ce.get("oi"),
                "ce_volume": ce.get("volume"),
                "pe_ltp": pe.get("ltp"),
                "pe_oi": pe.get("oi"),
                "pe_volume": pe.get("volume"),
            }
        )
    if not rows:
        return pd.DataFrame(
            columns=["strike", "ce_ltp", "ce_oi", "ce_volume", "pe_ltp", "pe_oi", "pe_volume"]
        )
    return pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)


def _render_sidebar(client: RedisClient | None) -> tuple[str, int, dict, str]:
    stored = _load_controls(client)
    if "terminal_controls" not in st.session_state:
        st.session_state["terminal_controls"] = stored
    else:
        # Risk / disable flags are authoritative from Redis (avoid stale session re-arming)
        merged = dict(st.session_state["terminal_controls"])
        for key in (
            "trading_disabled",
            "trading_disabled_until",
            "emergency_square_off",
            "emergency_square_off_at",
            "square_off_requested",
            "square_off_at",
            "circuit_breaker_reason",
            "kill_switch",
        ):
            if key in stored:
                merged[key] = stored[key]
        st.session_state["terminal_controls"] = merged
    controls = dict(st.session_state["terminal_controls"])

    with st.sidebar:
        st.markdown("### Desk controls")
        st.caption("Risk overrides · API keys on **Settings**")
        if client is None:
            st.warning("Redis offline — controls are session-only.")

        kill_switch = st.toggle(
            "HALT Trade agent orders",
            value=bool(controls.get("kill_switch")),
            help="When armed, Trade must not send new orders.",
            key="ui_kill_switch",
        )
        if kill_switch:
            st.error("KILL-SWITCH ARMED")
        else:
            st.success("Trade agent permitted")

        st.markdown("##### Square-off")
        square_col, clear_col = st.columns(2)
        square_patch: dict = {}
        if square_col.button("Square Off Now", use_container_width=True):
            square_patch = {
                "square_off_requested": True,
                "square_off_at": _utc_now_iso(),
            }
        if clear_col.button("Clear Flag", use_container_width=True):
            square_patch = {
                "square_off_requested": False,
                "square_off_at": None,
            }
        if square_patch:
            controls = _patch_controls(client, square_patch)

        if controls.get("square_off_requested"):
            st.warning(f"Pending since {format_ist(controls.get('square_off_at'))}")

        st.markdown("##### Live refresh")
        rate_labels = list(REFRESH_RATE_OPTIONS.keys())
        current_sec = int(controls.get("refresh_rate_sec") or 5)
        default_label = next(
            (label for label, sec in REFRESH_RATE_OPTIONS.items() if sec == current_sec),
            "5 seconds",
        )
        rate_label = st.select_slider("Console refresh", options=rate_labels, value=default_label)
        refresh_rate_sec = REFRESH_RATE_OPTIONS[rate_label]
        if st.button("Refresh now", use_container_width=True):
            st.rerun()
        if st.button("Pulse agents (force briefing)", use_container_width=True):
            st.session_state["force_briefing"] = True
            st.rerun()
        if st.button("Reset Trade + rerun desk", use_container_width=True):
            st.session_state["rerun_desk"] = True
            st.rerun()

        st.divider()
        broker = st.selectbox("Broker", ["dhan", "zerodha"], format_func=str.upper)
        symbol = st.selectbox("Underlying", ["NIFTY", "BANKNIFTY"])

        controls = _patch_controls(
            client,
            {
                "kill_switch": bool(kill_switch),
                "refresh_rate_sec": int(refresh_rate_sec),
            },
        )

        st.divider()
        st.caption("Deep pages")
        st.markdown(
            "- Agents\n"
            "- Insights\n"
            "- Global Outlook\n"
            "- Live Market\n"
            "- Settings"
        )

    return symbol, refresh_rate_sec, controls, broker


def _render_header(clock, day) -> None:
    st.markdown(f'<p class="console-brand">{CONSOLE_TITLE}</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="console-tag">LangGraph strategy (slow) · tactical Python orders/stops (fast) · '
        "probabilistic sleeves · hard day-loss · "
        "Scout · Voices · Research · Risk · Trade · "
        "decent close (phenomenal · okay · flat · acceptable loss)</p>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="console-phase">{clock.phase} · {clock.now_ist} · {clock.label}</div>',
        unsafe_allow_html=True,
    )
    grade = day.grade
    st.markdown(
        f'<div class="grade-pill grade-{grade}">DAY GRADE · {grade}'
        f'{" · DECENT" if day.decent and grade != "NO_DATA" else ""}'
        f'{" · NOT DECENT" if not day.decent else ""}'
        f" — {day.message}</div>",
        unsafe_allow_html=True,
    )


def _render_agents(statuses) -> None:
    st.subheader("Agent fleet")
    cols = st.columns(len(statuses))
    for col, s in zip(cols, statuses):
        with col:
            st.markdown(
                f"""
                <div class="agent-card" style="border-top: 3px solid {s.color}">
                  <div class="agent-name">{s.name}</div>
                  <div class="agent-status">{s.status}</div>
                  <div style="margin-top:0.4rem;font-size:0.88rem;font-weight:600">{s.headline}</div>
                  <div style="margin-top:0.25rem;font-size:0.8rem;color:#4B5563">{s.detail[:140]}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.caption(s.mandate)


def _render_discussion(client: RedisClient | None) -> None:
    st.subheader("Live discussion")
    st.caption("Agents debate facts from Outlook, Voices, chain, and risk — Trade decides.")
    rows = recent_discussion(client, limit=36)
    if not rows:
        st.info("No discussion yet — click **Pulse agents** or wait for the next live refresh.")
        return
    for row in rows[:24]:
        agent = str(row.get("agent") or "?")
        msg = str(row.get("message") or "")
        ts = format_ist(row.get("timestamp"))
        st.markdown(
            f'<div class="chat-line"><span class="chat-meta">{ts} · {agent.upper()}</span><br/>{msg}</div>',
            unsafe_allow_html=True,
        )


def _render_tape(client: RedisClient, symbol: str) -> None:
    chain = client.get_option_chain_state(symbol) or {
        "symbol": symbol,
        "underlying_ltp": None,
        "atm": None,
        "expiry": None,
        "updated_at": None,
        "strikes": {},
    }
    call_oi = put_oi = 0
    for sides in (chain.get("strikes") or {}).values():
        if not isinstance(sides, dict):
            continue
        ce_oi = (sides.get("CE") or {}).get("oi")
        pe_oi = (sides.get("PE") or {}).get("oi")
        if ce_oi is not None:
            call_oi += int(ce_oi)
        if pe_oi is not None:
            put_oi += int(pe_oi)
    pcr = compute_pcr(call_oi, put_oi)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric(f"{symbol} LTP", chain.get("underlying_ltp") or "—")
    m2.metric("PCR (OI)", f"{pcr:.3f}" if pcr is not None else "—")
    m3.metric("ATM", chain.get("atm") or "—")
    m4.metric("Expiry", chain.get("expiry") or "—")

    df = _chain_frame(chain)
    if df.empty:
        st.caption("Strike ladder empty — start tick workers to feed Research.")
        return
    left, right = st.columns([1.2, 1])
    with left:
        st.dataframe(df, use_container_width=True, hide_index=True, height=320)
    with right:
        st.bar_chart(df.set_index("strike")[["ce_oi", "pe_oi"]].fillna(0))
        with st.expander("OI signals"):
            tracker = OITracker()
            prev = st.session_state.get("oi_prev_chain")
            if prev and prev.get("symbol") == symbol:
                tracker.snapshot(prev)
            snap = tracker.snapshot(chain)
            st.session_state["oi_prev_chain"] = chain
            st.write(tracker.summarize_signals(snap))


def _render_main(symbol: str, controls: dict, broker: str) -> None:
    client = _redis()
    clock = session_clock()
    pnl, capital_ref = _day_pnl(broker, client)
    day = classify_day_outcome(pnl, capital_ref=capital_ref)

    _render_header(clock, day)

    if client is None:
        st.error(
            "Redis is not reachable. Start `docker compose up -d`, then **Settings → Validate Redis**. "
            "Agent discussion still works from local logs once Redis returns."
        )

    if client is not None:
        render_risk_controls(client, broker=broker)
        controls = load_terminal_controls(client)

    disabled = is_trading_disabled(controls) if controls else False
    statuses = build_agent_statuses(
        client=client,
        symbol=symbol,
        controls=controls,
        day=day,
        trading_disabled=disabled,
    )

    force = bool(st.session_state.pop("force_briefing", False))
    if st.session_state.pop("rerun_desk", False):
        with st.spinner("Resetting Trade decisions · refreshing Outlook, Voices, Insights…"):
            try:
                result = rerun_desk(client=client, symbol=symbol, controls=controls, broker=broker)
                dec = result.get("latest_decision") or {}
                st.success(
                    f"Desk rerun complete · {dec.get('kind', '—')} · {str(dec.get('summary') or '')[:120]}"
                )
                # Refresh local day/status after inputs updated
                pnl, capital_ref = _day_pnl(broker, client)
                day = classify_day_outcome(pnl, capital_ref=capital_ref)
                statuses = build_agent_statuses(
                    client=client,
                    symbol=symbol,
                    controls=controls,
                    day=day,
                    trading_disabled=disabled,
                )
            except Exception as exc:
                st.error(f"Desk rerun failed: {exc}")
                logger.exception("Desk rerun failed")
    elif force or clock.is_live_desk or clock.phase in {"PRE_MARKET", "PRE_OPEN"}:
        try:
            sync_agent_briefing(
                client=client,
                symbol=symbol,
                controls=controls,
                day=day,
                statuses=statuses,
                force=force,
            )
        except Exception as exc:
            logger.warning("Briefing sync failed: %s", exc)

    _render_agents(statuses)

    st.divider()
    c_disc, c_book = st.columns([1.15, 1])
    with c_disc:
        _render_discussion(client)
    with c_book:
        st.subheader("Book & capital")
        if client is not None:
            render_capital_cards(broker)  # type: ignore[arg-type]
            render_positions_table(broker, redis_client=client)  # type: ignore[arg-type]
            st.divider()
            render_orders_table(redis_client=client)
        else:
            st.caption("Connect Redis for live book.")

    st.divider()
    st.subheader("Research tape")
    if client is not None:
        _render_tape(client, symbol)
    else:
        st.caption("Tape requires Redis option_chain state.")

    with st.expander("What “decent day” means"):
        st.markdown(
            """
| Grade | Meaning |
|-------|---------|
| **PHENOMENAL** | Strong green vs capital |
| **OKAY** | Modest profit |
| **FLAT** | No meaningful P&L — capital preserved |
| **ACCEPTABLE_LOSS** | Small loss inside budget — still decent |
| **BREACH** | Beyond budget — Trade must cut |

See **Thesis** for the same ladder on **nett impact** (after trade charges).
The fleet’s job is to discuss **critical facts** (global bias, direct voices, chain/PCR, risk)
and let **Trade** decide so the session closes on one of the first four outcomes whenever possible.
"""
        )
        st.dataframe(pd.DataFrame(day.ladder), use_container_width=True, hide_index=True)
        st.caption("Agents: " + " · ".join(f"{a.name}" for a in AGENT_FLEET))


def main() -> None:
    require_login()
    render_sidebar_profile()

    client = _redis()
    symbol, refresh_rate_sec, controls, broker = _render_sidebar(client)

    if refresh_rate_sec > 0:

        @st.fragment(run_every=timedelta(seconds=refresh_rate_sec))
        def _live() -> None:
            _render_main(symbol, controls, broker)

        _live()
    else:
        _render_main(symbol, controls, broker)


if __name__ == "__main__":
    main()
