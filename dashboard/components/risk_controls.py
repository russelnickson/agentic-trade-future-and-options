"""Emergency risk controls for the Streamlit trading terminal."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st

from config.settings import get_settings
from database.redis_client import RedisClient
from dashboard.components.orders import append_order_audit
from dashboard.timefmt import format_ist

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Shared with the execution engine / dashboard app.
CONTROL_KEY = "terminal:controls"
RISK_BROADCAST_CHANNEL = "risk:broadcast"
EMERGENCY_EVENT_KEY = "risk:emergency:last"


@dataclass
class EmergencyBroadcast:
    """Payload published when EMERGENCY SQUARE OFF ALL is armed."""

    event: str = "EMERGENCY_SQUARE_OFF_ALL"
    action: str = "LIQUIDATE_INTRADAY_AND_DISABLE_TRADING"
    timestamp: str = ""
    trading_disabled_until: str = ""
    kill_switch: bool = True
    square_off_all_intraday: bool = True
    disable_agent_trading: bool = True
    source: str = "dashboard.risk_controls"
    broker_results: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def end_of_trading_day_ist(as_of: datetime | None = None) -> datetime:
    """Return 23:59:59 IST on the trading calendar day (agents stay disabled for the day)."""
    now_ist = (as_of or _utc_now()).astimezone(IST)
    eod = datetime.combine(now_ist.date(), time(23, 59, 59), tzinfo=IST)
    return eod


def load_terminal_controls(client: RedisClient) -> dict[str, Any]:
    defaults: dict[str, Any] = {
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
    try:
        raw = client.client.get(CONTROL_KEY)
        if not raw:
            return defaults
        data = json.loads(raw)
        return {**defaults, **data}
    except Exception:
        logger.exception("Failed to load terminal controls")
        return defaults


def save_terminal_controls(client: RedisClient, controls: dict[str, Any]) -> dict[str, Any]:
    payload = {**controls, "updated_at": _utc_now_iso()}
    try:
        client.client.set(CONTROL_KEY, json.dumps(payload, default=str))
    except Exception:
        logger.exception("Failed to persist terminal controls")
    return payload


def is_trading_disabled(controls: dict[str, Any] | None = None, client: RedisClient | None = None) -> bool:
    """True when kill-switch / emergency disable is active for today."""
    state = controls if controls is not None else (
        load_terminal_controls(client) if client is not None else {}
    )
    if state.get("kill_switch") or state.get("trading_disabled") or state.get("emergency_square_off"):
        until = state.get("trading_disabled_until")
        if not until:
            return True
        try:
            deadline = datetime.fromisoformat(str(until))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return _utc_now() <= deadline.astimezone(timezone.utc)
        except ValueError:
            return True
    return False


def _dhan_exit_all_and_kill_switch() -> dict[str, Any]:
    """Best-effort: exit all positions + activate Dhan day kill switch."""
    from dhanhq import DhanContext, TraderControl

    settings = get_settings()
    ctx = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
    http = ctx.get_dhan_http()
    results: dict[str, Any] = {}

    try:
        results["exit_all_positions"] = http.delete("/positions")
    except Exception as exc:
        logger.exception("Dhan exit-all failed")
        results["exit_all_positions"] = {"status": "failure", "remarks": str(exc)}

    try:
        results["kill_switch"] = TraderControl(ctx).kill_switch("activate")
    except Exception as exc:
        logger.exception("Dhan kill switch activate failed")
        results["kill_switch"] = {"status": "failure", "remarks": str(exc)}

    return results


def _zerodha_broadcast_only() -> dict[str, Any]:
    """
    Zerodha has no single exit-all API in Kite Connect comparable to Dhan DELETE /positions.
    Execution workers subscribed to the Redis broadcast must flatten NRML/MIS positions.
    """
    return {
        "status": "broadcast_only",
        "remarks": "Zerodha flatten delegated to execution workers via risk:broadcast",
    }


def trigger_emergency_square_off(
    client: RedisClient,
    *,
    broker: str = "dhan",
    call_broker_apis: bool = True,
    source: str = "dashboard.risk_controls",
    reason: str | None = None,
) -> EmergencyBroadcast:
    """
    Broadcast emergency liquidation + disable agent trading for the rest of the IST day.

    Side effects:
      1. Redis PUBLISH on ``risk:broadcast``
      2. Persist flags on ``terminal:controls`` (kill switch + trading_disabled)
      3. Store last event at ``risk:emergency:last``
      4. Append an order-audit sentinel event
      5. Optionally call broker APIs (Dhan exit-all + kill switch)
    """
    eod = end_of_trading_day_ist()
    broker_results: dict[str, Any] = {}

    if call_broker_apis:
        if broker == "dhan":
            try:
                broker_results["dhan"] = _dhan_exit_all_and_kill_switch()
            except Exception as exc:
                broker_results["dhan"] = {"status": "failure", "remarks": str(exc)}
        elif broker == "zerodha":
            broker_results["zerodha"] = _zerodha_broadcast_only()

    event = EmergencyBroadcast(
        timestamp=_utc_now_iso(),
        trading_disabled_until=eod.isoformat(),
        source=source,
        broker_results=broker_results,
    )
    payload = event.to_dict()
    if reason:
        payload["reason"] = reason
    blob = json.dumps(payload, default=str)

    # 1) Fan-out to all execution / strategy workers.
    try:
        receivers = client.client.publish(RISK_BROADCAST_CHANNEL, blob)
        payload["subscribers_notified"] = int(receivers)
    except Exception as exc:
        logger.exception("Emergency Redis PUBLISH failed")
        payload["publish_error"] = str(exc)

    # 2) Durable last-event + terminal controls.
    try:
        client.client.set(EMERGENCY_EVENT_KEY, blob)
    except Exception:
        logger.exception("Failed to store emergency event key")

    controls = load_terminal_controls(client)
    controls.update(
        {
            "kill_switch": True,
            "square_off_requested": True,
            "square_off_at": payload["timestamp"],
            "emergency_square_off": True,
            "emergency_square_off_at": payload["timestamp"],
            "trading_disabled": True,
            "trading_disabled_until": payload["trading_disabled_until"],
            "circuit_breaker_reason": reason,
        }
    )
    save_terminal_controls(client, controls)
    try:
        import streamlit as st

        st.session_state["terminal_controls"] = controls
    except Exception:
        pass

    # 3) Audit trail.
    try:
        append_order_audit(
            {
                "order_id": f"EMERGENCY-{date.today().isoformat()}-{int(_utc_now().timestamp())}",
                "timestamp": payload["timestamp"],
                "strategy_name": "RISK_EMERGENCY",
                "strike": "ALL",
                "action": "SELL",
                "quantity": 0,
                "status": "PENDING",
                "execution_latency_ms": 0,
                "note": reason or "EMERGENCY_SQUARE_OFF_ALL broadcast",
            },
            redis_client=client,
        )
    except Exception:
        logger.exception("Failed to append emergency audit event")

    logger.error(
        "EMERGENCY SQUARE OFF ALL triggered (source=%s) — trading disabled until %s",
        source,
        payload["trading_disabled_until"],
    )
    return event


def render_risk_controls(
    client: RedisClient,
    *,
    broker: str = "dhan",
) -> dict[str, Any]:
    """
    Render a prominent red **EMERGENCY SQUARE OFF ALL** control.

    Requires an explicit confirmation checkbox before the destructive action runs.
    """
    controls = load_terminal_controls(client)
    disabled = is_trading_disabled(controls)

    st.markdown("### Risk Controls")
    st.markdown(
        """
        <div style="
            border: 2px solid #b71c1c;
            background: linear-gradient(180deg, #3b0a0a 0%, #1a0505 100%);
            border-radius: 12px;
            padding: 1.1rem 1.25rem;
            margin-bottom: 0.75rem;
        ">
          <div style="color:#ffcdd2; font-size:0.85rem; letter-spacing:0.08em; text-transform:uppercase;">
            Emergency
          </div>
          <div style="color:#ffffff; font-size:1.35rem; font-weight:700; margin:0.25rem 0 0.5rem 0;">
            Square off all intraday risk immediately
          </div>
          <div style="color:#ef9a9a; font-size:0.95rem;">
            Broadcasts liquidation to execution workers, arms the kill-switch, and disables
            agent trading for the rest of the IST trading day.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if disabled:
        until = format_ist(controls.get("trading_disabled_until"), seconds=True)
        st.error(f"Agent trading is **DISABLED** for the day (until {until}).")
        if controls.get("emergency_square_off"):
            st.warning(
                f"Last emergency square-off at {format_ist(controls.get('emergency_square_off_at'), seconds=True)}"
            )
        if st.button("Re-enable trading for today", type="primary"):
            cleared = {
                **controls,
                "kill_switch": False,
                "square_off_requested": False,
                "square_off_at": None,
                "emergency_square_off": False,
                "emergency_square_off_at": None,
                "trading_disabled": False,
                "trading_disabled_until": None,
                "circuit_breaker_reason": None,
            }
            save_terminal_controls(client, cleared)
            st.success("Trading re-enabled.")
            st.rerun()

    confirm = st.checkbox(
        "I understand this will liquidate open intraday positions and block new agent orders today.",
        key="emergency_square_off_confirm",
        value=False,
    )

    clicked = st.button(
        "EMERGENCY SQUARE OFF ALL",
        type="primary",
        use_container_width=True,
        disabled=not confirm,
        help="Requires confirmation checkbox. Publishes risk:broadcast and disables trading until EOD IST.",
    )

    # Force a red look via custom CSS scoped to this button label.
    st.markdown(
        """
        <style>
        div[data-testid="stButton"] button[kind="primary"] {
            background-color: #c62828 !important;
            border-color: #b71c1c !important;
            color: #ffffff !important;
            font-weight: 800 !important;
            font-size: 1.05rem !important;
            letter-spacing: 0.04em;
            min-height: 3.2rem;
        }
        div[data-testid="stButton"] button[kind="primary"]:hover {
            background-color: #e53935 !important;
            border-color: #c62828 !important;
        }
        div[data-testid="stButton"] button[kind="primary"]:disabled {
            background-color: #7f1d1d !important;
            opacity: 0.55;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    if clicked:
        with st.spinner("Broadcasting emergency square-off…"):
            event = trigger_emergency_square_off(
                client, broker=broker, call_broker_apis=True
            )
        st.error(
            "EMERGENCY SQUARE OFF ALL executed — liquidation broadcast sent; "
            "agent trading disabled until "
            f"{event.trading_disabled_until}."
        )
        with st.expander("Broadcast payload / broker results"):
            st.json(event.to_dict())
        st.session_state["emergency_square_off_confirm"] = False
        st.rerun()

    return controls
