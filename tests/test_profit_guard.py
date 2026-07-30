"""Unit tests for profit booking / trailing rules."""

from __future__ import annotations

from services.profit_guard import evaluate_long_premium, target_price, trail_arm_price


def test_target_and_arm_prices() -> None:
    assert target_price(100.0, tp_frac=0.28) == 128.0
    assert trail_arm_price(100.0, arm_frac=0.15) == 115.0


def test_hard_take_profit() -> None:
    out = evaluate_long_premium(
        entry=100.0,
        ltp=130.0,
        peak_ltp=100.0,
        stop_price=65.0,
        tp_frac=0.28,
        arm_frac=0.15,
        giveback_frac=0.40,
    )
    assert out["exit_reason"] == "TAKE_PROFIT"
    assert out["target_price"] == 128.0
    assert out["peak_ltp"] == 130.0


def test_trail_arms_and_raises_stop() -> None:
    out = evaluate_long_premium(
        entry=100.0,
        ltp=120.0,
        peak_ltp=100.0,
        stop_price=65.0,
        tp_frac=0.40,
        arm_frac=0.15,
        giveback_frac=0.40,
    )
    assert out["trail_armed"] is True
    assert out["exit_reason"] is None
    # lock 60% of +20 gain = +12 → stop 112; also >= BE+2
    assert out["stop_price"] >= 112.0
    assert out["stop_price"] > 65.0


def test_trail_exit_after_giveback() -> None:
    # Peak was 130, giveback 40% → lock entry+18=118; LTP falls to 117
    out = evaluate_long_premium(
        entry=100.0,
        ltp=117.0,
        peak_ltp=130.0,
        stop_price=118.0,
        tp_frac=0.50,
        arm_frac=0.15,
        giveback_frac=0.40,
    )
    assert out["trail_armed"] is True
    assert out["exit_reason"] == "TRAIL_EXIT"


def test_hard_stop_before_trail() -> None:
    out = evaluate_long_premium(
        entry=100.0,
        ltp=60.0,
        peak_ltp=100.0,
        stop_price=65.0,
        tp_frac=0.28,
        arm_frac=0.15,
        giveback_frac=0.40,
    )
    assert out["exit_reason"] == "STOP"
    assert out["trail_armed"] is False
