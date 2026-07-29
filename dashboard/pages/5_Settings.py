"""Settings — broker selection, local API credentials, on-screen validation."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dashboard.auth import render_sidebar_profile, require_login
from dashboard.secrets_store import (
    apply_secrets_to_environ,
    exchange_zerodha_request_token,
    kite_login_url,
    load_local_secrets,
    mask_secret,
    save_local_secrets,
    secrets_path,
    validate_dhan,
    validate_redis,
    validate_zerodha,
)

st.set_page_config(
    page_title="Settings · Trade Console",
    page_icon="⚙️",
    layout="wide",
    initial_sidebar_state="expanded",
)

require_login()
apply_secrets_to_environ()
render_sidebar_profile()

st.title("Settings")
st.caption(
    "Broker and API credentials are saved only to a **gitignored** local file "
    f"`{secrets_path().name}` (mode 0600). They are never pushed to git, Redis, or logs."
)

with st.expander("How secrets are kept safe", expanded=False):
    st.markdown(
        f"""
1. Values are written to `{secrets_path()}` on this machine only.
2. That path is listed in `.gitignore` — do not force-add it.
3. File permissions are set to **0600** (owner read/write).
4. Validation calls go **directly** from this host to the broker REST API.
5. Prefer a vault or OS keychain on shared production hosts; this UI is for desk-local setup.
"""
    )

secrets = load_local_secrets()

st.subheader("Active broker")
broker = st.selectbox(
    "TRADE_BROKER",
    options=["dhan", "zerodha"],
    index=0 if (secrets.get("TRADE_BROKER") or "dhan").lower() != "zerodha" else 1,
    format_func=str.upper,
    help="Used by capital cards, positions, circuit breaker, and preflight.",
)

st.divider()

# --------------------------------------------------------------------------- Dhan
st.subheader("DhanHQ credentials")
dhan_help = st.popover("How to get Dhan API keys")
with dhan_help:
    st.markdown(
        """
**DhanHQ setup**

1. Open [web.dhan.co](https://web.dhan.co) and sign in.
2. Go to **Profile → DhanHQ Trading APIs** (or Developer / API section).
3. Create or open an app to obtain:
   - **Client ID** (`DHAN_CLIENT_ID`)
   - **Access Token** (`DHAN_ACCESS_TOKEN`) — regenerate if leaked.
4. Paste both below, then click **Validate Dhan**.
5. Save — the token stays in `.secrets.env` only.

**Tips**

- Access tokens can expire; re-generate and re-validate on this screen.
- Never share the token in chat, screenshots, or git commits.
- Market feed uses the same Client ID + Access Token.
"""
    )

dhan_c1, dhan_c2 = st.columns(2)
with dhan_c1:
    dhan_client_id = st.text_input(
        "Client ID",
        value=secrets.get("DHAN_CLIENT_ID", ""),
        key="set_dhan_client_id",
    )
with dhan_c2:
    st.caption(
        f"Access token on disk: `{mask_secret(secrets.get('DHAN_ACCESS_TOKEN', '')) or 'not set'}`"
    )
    dhan_token = st.text_input(
        "Access Token (leave blank to keep existing)",
        value="",
        type="password",
        key="set_dhan_token",
    )

dhan_v1, dhan_v2 = st.columns([1, 3])
with dhan_v1:
    validate_dhan_btn = st.button("Validate Dhan", use_container_width=True)
if validate_dhan_btn:
    token_for_check = (dhan_token or secrets.get("DHAN_ACCESS_TOKEN", "")).strip()
    ok, msg = validate_dhan(dhan_client_id, token_for_check)
    if ok:
        # Persist immediately — validating a pasted token without Save left
        # .secrets.env on the old JWT and Capital showed ₹0 / DH-906.
        path = save_local_secrets(
            {
                "TRADE_BROKER": "dhan",
                "DHAN_CLIENT_ID": (dhan_client_id or "").strip(),
                "DHAN_ACCESS_TOKEN": token_for_check,
            }
        )
        try:
            from config.settings import get_settings

            get_settings.cache_clear()
        except Exception:
            pass
        apply_secrets_to_environ()
        st.success(f"{msg} — saved to `{path.name}`")
        st.caption("Restart/refresh the Trade Console so capital cards reload.")
    else:
        st.error(msg)

st.divider()

# ----------------------------------------------------------------------- Zerodha
st.subheader("Zerodha Kite — simple login")
st.caption(
    "You do **not** need a TOTP secret. Log in once in the browser each trading day, "
    "paste the redirect token here, and we save the access token locally."
)

z_api_key = st.text_input(
    "1. API Key",
    value=secrets.get("ZERODHA_API_KEY", ""),
    key="set_z_key",
    help="From https://developers.kite.trade → your app",
)
z_api_secret = st.text_input(
    "2. API Secret (leave blank to keep saved value)",
    value="",
    type="password",
    key="set_z_secret",
    help=f"On disk: {mask_secret(secrets.get('ZERODHA_API_SECRET', '')) or 'not set'}",
)

secret_for_login = z_api_secret.strip() or secrets.get("ZERODHA_API_SECRET", "")
login_url = kite_login_url(z_api_key) if z_api_key.strip() else ""

st.markdown("**3. Open Zerodha login**")
if login_url:
    st.link_button("Open Kite login in browser", login_url, use_container_width=False)
    st.code(login_url, language=None)
else:
    st.info("Enter your API Key above to generate the login link.")

st.markdown(
    """
After you sign in, Zerodha redirects you to your app URL. Copy either:
- the full redirect URL, or  
- just the `request_token=...` value from it
"""
)
z_request = st.text_input(
    "4. Paste request_token (or full redirect URL)",
    value="",
    key="set_z_request",
    placeholder="request_token=…  or  https://…?request_token=…",
)

ex1, ex2 = st.columns([1, 1])
with ex1:
    exchange_btn = st.button("Get & save access token", type="primary", use_container_width=True)
with ex2:
    validate_z_btn = st.button("Validate saved session", use_container_width=True)

if exchange_btn:
    ok, msg, _token = exchange_zerodha_request_token(
        z_api_key,
        secret_for_login,
        z_request,
    )
    if ok:
        st.success(msg)
        st.rerun()
    else:
        st.error(msg)

st.caption(
    f"Saved access token: `{mask_secret(secrets.get('ZERODHA_ACCESS_TOKEN', '')) or 'not set yet'}`"
)

if validate_z_btn:
    ok, msg = validate_zerodha(
        z_api_key,
        access_token=secrets.get("ZERODHA_ACCESS_TOKEN", ""),
        api_secret=secret_for_login,
    )
    if ok:
        st.success(msg)
    else:
        st.error(msg)

with st.expander("Advanced (optional) — headless login with TOTP"):
    st.caption("Skip this unless you already saved your authenticator seed.")
    z_user_id = st.text_input("User ID", value=secrets.get("ZERODHA_USER_ID", ""), key="set_z_uid")
    z_password = st.text_input(
        "Password (leave blank to keep)",
        value="",
        type="password",
        key="set_z_pass",
    )
    z_totp = st.text_input(
        "TOTP Secret (leave blank to keep)",
        value="",
        type="password",
        key="set_z_totp",
    )
    z_access = st.text_input(
        "Access Token override (leave blank to keep)",
        value="",
        type="password",
        key="set_z_access",
    )
    if st.button("Validate with advanced fields", key="z_adv_validate"):
        ok, msg = validate_zerodha(
            z_api_key,
            access_token=z_access or secrets.get("ZERODHA_ACCESS_TOKEN", ""),
            api_secret=secret_for_login,
            user_id=z_user_id,
            password=z_password or secrets.get("ZERODHA_PASSWORD", ""),
            totp_secret=z_totp or secrets.get("ZERODHA_TOTP_SECRET", ""),
        )
        if ok:
            st.success(msg)
        else:
            st.error(msg)

# Advanced fields may be absent if expander never opened — read session / secrets.
z_user_id = st.session_state.get("set_z_uid", secrets.get("ZERODHA_USER_ID", ""))
z_password = st.session_state.get("set_z_pass", "")
z_totp = st.session_state.get("set_z_totp", "")
z_access = st.session_state.get("set_z_access", "")

st.divider()

# ------------------------------------------------------------------ Infra / risk
st.subheader("Infrastructure & risk")
infra_help = st.popover("Redis / Timescale tips")
with infra_help:
    st.markdown(
        """
**Local Docker defaults** (from `docker-compose.yml`):

- Redis: `redis://localhost:6379/0`
- Timescale: `postgresql://trade:trade@localhost:5432/trade`

Start with `docker compose up -d`, then validate Redis below.
Leave blank to keep values already in `.env`.
"""
    )

i1, i2 = st.columns(2)
with i1:
    redis_url = st.text_input(
        "REDIS_URL",
        value=secrets.get("REDIS_URL", ""),
        key="set_redis",
        placeholder="redis://localhost:6379/0",
    )
    max_loss = st.text_input(
        "MAX_DAILY_LOSS (INR)",
        value=secrets.get("MAX_DAILY_LOSS", "5000"),
        key="set_max_loss",
    )
with i2:
    st.caption(
        f"DATABASE_URL on disk: `{mask_secret(secrets.get('DATABASE_URL', ''), visible=8) or 'not set'}`"
    )
    database_url = st.text_input(
        "DATABASE_URL (leave blank to keep)",
        value="",
        type="password",
        key="set_db",
        placeholder="postgresql://trade:trade@localhost:5432/trade",
    )
    trade_tokens = st.text_input(
        "TRADE_TOKENS (comma-separated)",
        value=secrets.get("TRADE_TOKENS", ""),
        key="set_tokens",
        help="Instrument tokens for tick_worker / preflight.",
    )

r1, r2 = st.columns([1, 3])
with r1:
    validate_redis_btn = st.button("Validate Redis", use_container_width=True)
if validate_redis_btn:
    ok, msg = validate_redis(redis_url or secrets.get("REDIS_URL") or None)
    if ok:
        st.success(msg)
    else:
        st.error(msg)

st.divider()

save_col, path_col = st.columns([1, 3])
with save_col:
    save_clicked = st.button("Save settings", type="primary", use_container_width=True)
with path_col:
    st.caption(f"Target file: `{secrets_path()}` (gitignored)")


def _keep(new: str, old: str) -> str:
    """Blank password-style fields mean 'do not change'."""
    return new.strip() if new.strip() else old


if save_clicked:
    path = save_local_secrets(
        {
            "TRADE_BROKER": broker,
            "DHAN_CLIENT_ID": dhan_client_id,
            "DHAN_ACCESS_TOKEN": _keep(dhan_token, secrets.get("DHAN_ACCESS_TOKEN", "")),
            "ZERODHA_API_KEY": z_api_key,
            "ZERODHA_API_SECRET": _keep(z_api_secret, secrets.get("ZERODHA_API_SECRET", "")),
            "ZERODHA_USER_ID": z_user_id,
            "ZERODHA_PASSWORD": _keep(z_password, secrets.get("ZERODHA_PASSWORD", "")),
            "ZERODHA_TOTP_SECRET": _keep(z_totp, secrets.get("ZERODHA_TOTP_SECRET", "")),
            "ZERODHA_ACCESS_TOKEN": _keep(z_access, secrets.get("ZERODHA_ACCESS_TOKEN", "")),
            "REDIS_URL": redis_url,
            "DATABASE_URL": _keep(database_url, secrets.get("DATABASE_URL", "")),
            "MAX_DAILY_LOSS": max_loss,
            "TRADE_TOKENS": trade_tokens,
        }
    )
    st.success(f"Saved locally to `{path.name}`. Restart PM2 workers to pick up new broker tokens.")
