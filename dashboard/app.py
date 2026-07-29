"""EC2 worker telemetry dashboard — health, positions, emergency square-off."""

from __future__ import annotations

import os
from datetime import timedelta

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
load_dotenv(".secrets.env")

st.set_page_config(
    page_title="EC2 Worker Telemetry",
    page_icon="📡",
    layout="wide",
)

POLL_SEC = 5


def _client():
    from local_app.remote_client import RemoteClient

    return RemoteClient()


@st.cache_resource
def get_remote():
    try:
        return _client()
    except Exception as exc:
        st.session_state["client_error"] = str(exc)
        return None


st.title("EC2 Execution Worker Telemetry")
st.caption(
    f"Polling `http://$EC2_ELASTIC_IP:8000` every {POLL_SEC}s · "
    "auth via `INTERNAL_AUTH_SECRET`"
)

ip = (os.getenv("EC2_ELASTIC_IP") or os.getenv("EC2_HOST") or "").strip()
if not ip:
    st.error("Set `EC2_ELASTIC_IP` (or `EC2_HOST`) in `.env` / `.secrets.env`.")
    st.stop()
if not (os.getenv("INTERNAL_AUTH_SECRET") or "").strip():
    st.error("Set `INTERNAL_AUTH_SECRET` in `.env` / `.secrets.env`.")
    st.stop()

st.sidebar.markdown(f"**Target**  \n`{ip}:8000`")
st.sidebar.markdown(f"**Refresh**  \n{POLL_SEC}s")


@st.fragment(run_every=timedelta(seconds=POLL_SEC))
def live_panel() -> None:
    client = get_remote()
    if client is None:
        st.markdown(
            '<span style="background:#B42318;color:#fff;padding:6px 14px;'
            'border-radius:6px;font-weight:600;">Disconnected</span>',
            unsafe_allow_html=True,
        )
        st.warning(st.session_state.get("client_error") or "Remote client failed to init")
        return

    health = client.health_payload()
    live = bool(health and health.get("status") == "ok")

    c1, c2, c3 = st.columns([1, 2, 2])
    with c1:
        if live:
            st.markdown(
                '<span style="background:#0F6E56;color:#fff;padding:6px 14px;'
                'border-radius:6px;font-weight:600;">Live</span>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<span style="background:#B42318;color:#fff;padding:6px 14px;'
                'border-radius:6px;font-weight:600;">Disconnected</span>',
                unsafe_allow_html=True,
            )
    with c2:
        if health:
            st.write(
                f"ts `{health.get('timestamp', '—')}` · "
                f"static IP `{health.get('static_ip') or '—'}` · "
                f"broker `{((health.get('broker') or {}).get('broker_name'))}` · "
                f"paper `{((health.get('broker') or {}).get('paper_trading'))}`"
            )
        else:
            st.write("Worker unreachable")
    with c3:
        st.caption(f"Auto-refresh every {POLL_SEC}s")

    st.subheader("Live positions & P&L")
    if not live:
        st.info("Connect the worker to load positions.")
        return

    try:
        payload = client.get_positions()
        rows = payload.get("positions") or []
        if not rows:
            st.success("No open positions.")
        else:
            df = pd.DataFrame(rows)
            # Prefer readable columns when present
            prefer = [
                c
                for c in (
                    "symbol",
                    "exchange",
                    "qty",
                    "avg_price",
                    "pnl",
                    "ltp",
                    "updated_at",
                )
                if c in df.columns
            ]
            show = df[prefer] if prefer else df.drop(columns=["raw"], errors="ignore")
            st.dataframe(show, use_container_width=True, hide_index=True)
            if "pnl" in show.columns:
                total = pd.to_numeric(show["pnl"], errors="coerce").fillna(0).sum()
                st.metric("Unrealized P&L (sum)", f"₹{total:,.2f}")
            st.caption(f"{payload.get('count', len(rows))} position(s)")
    except Exception as exc:
        st.error(f"Positions fetch failed: {exc}")


live_panel()

st.divider()
st.subheader("Emergency kill-switch")
st.markdown(
    "Triggers **`POST /order/square_off_all`** on the EC2 worker and "
    "attempts to flatten every open position immediately."
)

confirm = st.checkbox(
    "I understand this will market-close all open positions on the worker.",
    key="kill_confirm",
)
if st.button(
    "SQUARE OFF ALL POSITIONS",
    type="primary",
    disabled=not confirm,
    use_container_width=True,
):
    client = get_remote()
    if client is None:
        st.error("Remote client unavailable.")
    else:
        try:
            result = client.square_off_all()
            if result.get("ok"):
                st.success(
                    f"Square-off sent · closed {result.get('closed', 0)} leg(s)."
                )
            else:
                st.warning(result)
            st.json(result)
        except Exception as exc:
            st.error(f"Square-off failed: {exc}")

st.markdown(
    """
<style>
div.stButton > button[kind="primary"] {
    background-color: #B42318;
    border-color: #B42318;
}
div.stButton > button[kind="primary"]:hover {
    background-color: #912018;
    border-color: #912018;
}
</style>
""",
    unsafe_allow_html=True,
)
