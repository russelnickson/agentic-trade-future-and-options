"""Real-time Black-Scholes IV and Greeks via py_vollib."""

from __future__ import annotations

import warnings
from dataclasses import asdict, dataclass
from typing import Any, Literal

import numpy as np

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from py_vollib.black_scholes.greeks.analytical import (
        delta as bs_delta,
        gamma as bs_gamma,
        theta as bs_theta,
        vega as bs_vega,
    )
    from py_vollib.black_scholes.implied_volatility import implied_volatility
    from py_vollib.helpers.exceptions import PriceIsAboveMaximum, PriceIsBelowIntrinsic

OptionSide = Literal["CE", "PE", "C", "P", "CALL", "PUT", "c", "p"]

DEFAULT_RISK_FREE_RATE = 0.10
_MIN_TTE_YEARS = 1.0 / (365.0 * 24.0 * 60.0)  # ~1 minute floor for numerical stability

_FLAG_MAP = {
    "CE": "c",
    "C": "c",
    "CALL": "c",
    "c": "c",
    "PE": "p",
    "P": "p",
    "PUT": "p",
    "p": "p",
}


@dataclass(frozen=True)
class GreeksResult:
    """Black-Scholes IV and first-order greeks for one option quote."""

    iv: float | None
    delta: float | None
    theta: float | None
    gamma: float | None
    vega: float | None
    spot: float
    strike: float
    tte: float
    option_ltp: float
    option_type: str
    risk_free_rate: float
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _flag(option_type: OptionSide | str) -> str:
    try:
        return _FLAG_MAP[str(option_type).strip().upper()]
    except KeyError as exc:
        raise ValueError(f"Unsupported option_type: {option_type!r}") from exc


def years_to_expiry(
    *,
    days: float | None = None,
    hours: float | None = None,
    seconds: float | None = None,
) -> float:
    """Convert calendar time remaining into Black-Scholes TTE (years)."""
    total_seconds = 0.0
    if days is not None:
        total_seconds += float(days) * 86_400.0
    if hours is not None:
        total_seconds += float(hours) * 3_600.0
    if seconds is not None:
        total_seconds += float(seconds)
    if total_seconds < 0:
        raise ValueError("time to expiry cannot be negative")
    return total_seconds / (365.0 * 86_400.0)


def compute_greeks(
    spot: float,
    strike: float,
    tte: float,
    option_ltp: float,
    option_type: OptionSide | str,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> GreeksResult:
    """
    Compute IV, Delta, Theta, Gamma, and Vega for a single option.

    Parameters
    ----------
    spot:
        Underlying / futures spot price (S).
    strike:
        Option strike (K).
    tte:
        Time to expiry in **years** (e.g. 7/365). Use `years_to_expiry` helpers
        if you have days/hours remaining.
    option_ltp:
        Observed option last traded price.
    option_type:
        ``CE`` / ``PE`` (also accepts CALL/PUT/c/p).
    risk_free_rate:
        Continuously compounded risk-free rate. Defaults to **10%**.

    Notes
    -----
    - IV is returned as a decimal (0.18 = 18%).
    - Theta is the py_vollib calendar-day theta (PnL per day).
    - Vega is the py_vollib 1% vol greek (price change per 1 vol point).
    """
    flag = _flag(option_type)
    side = "CE" if flag == "c" else "PE"
    S, K, T, r, price = (
        float(spot),
        float(strike),
        float(tte),
        float(risk_free_rate),
        float(option_ltp),
    )

    base = dict(
        spot=S,
        strike=K,
        tte=T,
        option_ltp=price,
        option_type=side,
        risk_free_rate=r,
    )

    if not np.isfinite(S) or S <= 0:
        return GreeksResult(iv=None, delta=None, theta=None, gamma=None, vega=None,
                            error="spot must be positive", **base)
    if not np.isfinite(K) or K <= 0:
        return GreeksResult(iv=None, delta=None, theta=None, gamma=None, vega=None,
                            error="strike must be positive", **base)
    if not np.isfinite(price) or price <= 0:
        return GreeksResult(iv=None, delta=None, theta=None, gamma=None, vega=None,
                            error="option_ltp must be positive", **base)
    if not np.isfinite(T) or T < 0:
        return GreeksResult(iv=None, delta=None, theta=None, gamma=None, vega=None,
                            error="tte must be >= 0", **base)

    # At/after expiry IV is undefined; still allow intrinsic-style short-circuit.
    if T < _MIN_TTE_YEARS:
        return GreeksResult(iv=None, delta=None, theta=None, gamma=None, vega=None,
                            error="tte too small for stable IV/greeks", **base)

    try:
        iv = float(implied_volatility(price, S, K, T, r, flag))
    except PriceIsBelowIntrinsic:
        return GreeksResult(iv=None, delta=None, theta=None, gamma=None, vega=None,
                            error="price below intrinsic", **base)
    except PriceIsAboveMaximum:
        return GreeksResult(iv=None, delta=None, theta=None, gamma=None, vega=None,
                            error="price above theoretical maximum", **base)
    except Exception as exc:  # noqa: BLE001 - surface solver failures cleanly
        return GreeksResult(iv=None, delta=None, theta=None, gamma=None, vega=None,
                            error=f"iv solver failed: {exc}", **base)

    if not np.isfinite(iv) or iv <= 0:
        return GreeksResult(iv=None, delta=None, theta=None, gamma=None, vega=None,
                            error="iv solver returned non-positive value", **base)

    try:
        return GreeksResult(
            iv=iv,
            delta=float(bs_delta(flag, S, K, T, r, iv)),
            theta=float(bs_theta(flag, S, K, T, r, iv)),
            gamma=float(bs_gamma(flag, S, K, T, r, iv)),
            vega=float(bs_vega(flag, S, K, T, r, iv)),
            error=None,
            **base,
        )
    except Exception as exc:  # noqa: BLE001
        return GreeksResult(
            iv=iv,
            delta=None,
            theta=None,
            gamma=None,
            vega=None,
            error=f"greeks failed: {exc}",
            **base,
        )


class GreeksEngine:
    """Thin stateful wrapper for repeated real-time greeks updates."""

    def __init__(self, risk_free_rate: float = DEFAULT_RISK_FREE_RATE) -> None:
        self.risk_free_rate = float(risk_free_rate)

    def compute(
        self,
        spot: float,
        strike: float,
        tte: float,
        option_ltp: float,
        option_type: OptionSide | str,
        risk_free_rate: float | None = None,
    ) -> GreeksResult:
        return compute_greeks(
            spot=spot,
            strike=strike,
            tte=tte,
            option_ltp=option_ltp,
            option_type=option_type,
            risk_free_rate=self.risk_free_rate if risk_free_rate is None else risk_free_rate,
        )

    def compute_from_days(
        self,
        spot: float,
        strike: float,
        days_to_expiry: float,
        option_ltp: float,
        option_type: OptionSide | str,
        risk_free_rate: float | None = None,
    ) -> GreeksResult:
        return self.compute(
            spot=spot,
            strike=strike,
            tte=years_to_expiry(days=days_to_expiry),
            option_ltp=option_ltp,
            option_type=option_type,
            risk_free_rate=risk_free_rate,
        )
