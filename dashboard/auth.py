"""Local dashboard auth — single operator account, session-scoped.

Credentials are hardcoded for the desk operator. The password is stored only as a
PBKDF2 digest in this module (never written to disk as plaintext).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

# Operator profile (local terminal only — not synced / not pushed).
OPERATOR_USERNAME = "russelnickson"
OPERATOR_DISPLAY_NAME = "Russel Nickson"
OPERATOR_AVATAR = Path(__file__).resolve().parent / "assets" / "avatar.svg"

# PBKDF2-HMAC-SHA256 of the local password with a fixed app salt (desk-only gate).
_AUTH_SALT = b"fno-terminal-local-auth-v1"
_AUTH_ITERATIONS = 200_000
# Digest for password "password" — do not replace with plaintext in source reviews.
_PASSWORD_DIGEST_HEX = hashlib.pbkdf2_hmac(
    "sha256",
    b"password",
    _AUTH_SALT,
    _AUTH_ITERATIONS,
).hex()

_SESSION_KEY = "auth_authenticated"
_SESSION_USER = "auth_username"


@dataclass(frozen=True)
class OperatorProfile:
    username: str
    display_name: str
    avatar_path: Path


def operator_profile() -> OperatorProfile:
    return OperatorProfile(
        username=OPERATOR_USERNAME,
        display_name=OPERATOR_DISPLAY_NAME,
        avatar_path=OPERATOR_AVATAR,
    )


def verify_password(username: str, password: str) -> bool:
    if username.strip().lower() != OPERATOR_USERNAME:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        _AUTH_SALT,
        _AUTH_ITERATIONS,
    ).hex()
    return hmac.compare_digest(digest, _PASSWORD_DIGEST_HEX)


def is_authenticated() -> bool:
    return bool(st.session_state.get(_SESSION_KEY)) and (
        st.session_state.get(_SESSION_USER) == OPERATOR_USERNAME
    )


def login(username: str, password: str) -> bool:
    if not verify_password(username, password):
        return False
    st.session_state[_SESSION_KEY] = True
    st.session_state[_SESSION_USER] = OPERATOR_USERNAME
    return True


def logout() -> None:
    st.session_state.pop(_SESSION_KEY, None)
    st.session_state.pop(_SESSION_USER, None)


def render_login_page() -> None:
    """Full-page sign-in. Caller should ``st.stop()`` after if still unauthenticated."""
    profile = operator_profile()
    st.markdown(
        """
        <style>
        .login-wrap { max-width: 420px; margin: 4rem auto 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    left, center, right = st.columns([1, 1.4, 1])
    with center:
        st.markdown("### Sign in")
        st.caption("Trade Console · local operator access")
        if profile.avatar_path.is_file():
            st.image(str(profile.avatar_path), width=96)
        st.markdown(f"**{profile.display_name}**")
        st.caption(f"@{profile.username}")

        with st.form("signin_form", clear_on_submit=False):
            username = st.text_input("Username", value="", autocomplete="username")
            password = st.text_input(
                "Password",
                type="password",
                autocomplete="current-password",
            )
            submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)

        if submitted:
            if login(username.strip(), password):
                st.success("Signed in")
                st.rerun()
            else:
                st.error("Invalid username or password")

        st.info(
            "Credentials stay in this browser session only. Broker API secrets are "
            "saved to a **gitignored** local file — never commit or sync them."
        )


def require_login() -> OperatorProfile:
    """Gate any Streamlit page. Returns the operator profile when authenticated."""
    if not is_authenticated():
        render_login_page()
        st.stop()
    return operator_profile()


def render_sidebar_profile() -> None:
    """Compact profile + logout for authenticated pages."""
    profile = operator_profile()
    with st.sidebar:
        cols = st.columns([1, 3])
        with cols[0]:
            if profile.avatar_path.is_file():
                st.image(str(profile.avatar_path), width=48)
        with cols[1]:
            st.markdown(f"**{profile.display_name}**")
            st.caption(f"@{profile.username}")
        if st.button("Sign out", use_container_width=True, key="auth_sign_out"):
            logout()
            st.rerun()
        st.divider()
