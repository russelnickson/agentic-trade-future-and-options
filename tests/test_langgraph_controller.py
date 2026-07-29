"""Tests for LangGraph strategy controller gate."""

from __future__ import annotations

import os

os.environ["STRATEGY_DRY_RUN"] = "true"
os.environ["REGIME_ALLOW_UNKNOWN"] = "1"
os.environ["TRAILING_SL_ARMED"] = "1"
os.environ["MAX_OPEN_POSITIONS"] = "5"

from local_app.strategy.langgraph_controller import run_controller
from local_app.strategy.pipeline import execute_signal


def test_controller_authorizes_calm_signal() -> None:
    state = run_controller(
        {
            "symbol": "NIFTY24500CE",
            "action": "BUY",
            "qty": 65,
            "order_type": "LIMIT",
            "india_vix": 12.5,
            "trend_strength": 0.4,
            "day_pnl": 0.0,
            "open_positions": 0,
        }
    )
    assert state["regime_ok"] is True
    assert state["risk_ok"] is True
    assert state["authorized"] is True
    assert state["dispatch_result"]["ok"] is True


def test_controller_blocks_extreme_vix() -> None:
    state = run_controller(
        {
            "symbol": "NIFTY24500CE",
            "action": "BUY",
            "qty": 65,
            "india_vix": 32.0,
            "trend_strength": 0.9,
            "day_pnl": 0.0,
            "open_positions": 0,
        }
    )
    assert state["regime"] == "EXTREME_VOL"
    assert state["authorized"] is False
    assert "VIX" in (state.get("abort_reason") or "")


def test_controller_blocks_drawdown() -> None:
    state = run_controller(
        {
            "symbol": "NIFTY24500CE",
            "action": "BUY",
            "qty": 65,
            "india_vix": 14.0,
            "trend_strength": 0.5,
            "day_pnl": -6000.0,
            "open_positions": 0,
        }
    )
    assert state["regime_ok"] is True
    assert state["risk_ok"] is False
    assert state["authorized"] is False


def test_pipeline_execute_signal() -> None:
    out = execute_signal(
        "NIFTY24500CE",
        "BUY",
        65,
        india_vix=13.0,
        trend_strength=0.5,
        day_pnl=0.0,
        open_positions=0,
    )
    assert out["ok"] is True
    assert out["aborted"] is False
