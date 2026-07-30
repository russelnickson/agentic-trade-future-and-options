"""Unit tests for day thesis nett-impact framework."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.day_thesis import (
    GRADE_PRIORITY,
    build_framework,
    estimate_option_charges,
    nett_pnl,
)


def test_grade_priority_order() -> None:
    assert list(GRADE_PRIORITY) == [
        "PHENOMENAL",
        "OKAY",
        "FLAT",
        "ACCEPTABLE_LOSS",
        "BREACH",
    ]


def test_charges_positive_and_breakdown() -> None:
    c = estimate_option_charges(100_000.0, buy_orders=2, sell_orders=2)
    assert c.total > 0
    assert c.brokerage == 80.0  # 4 × ₹20
    assert c.stt > 0
    assert abs(c.total - (c.brokerage + c.stt + c.exchange + c.sebi + c.stamp + c.gst)) < 0.02


def test_nett_subtracts_charges() -> None:
    assert nett_pnl(1_000.0, 120.5) == 879.5
    assert nett_pnl(None, 50.0) is None


def test_framework_priority_and_gross_covers_fees() -> None:
    charges = estimate_option_charges(50_000.0, buy_orders=1, sell_orders=1)
    bands = build_framework(
        capital_ref=50_000.0,
        session_charges=charges,
        day_budget=5_000.0,
    )
    assert [b.grade for b in bands] == list(GRADE_PRIORITY)
    assert bands[0].priority == 1
    okay = next(b for b in bands if b.grade == "OKAY")
    assert okay.gross_to_enter > okay.nett_min
    assert okay.estimated_charges_at_target == charges.total
    acc = next(b for b in bands if b.grade == "ACCEPTABLE_LOSS")
    assert acc.nett_min < acc.nett_max  # ordered band
    breach = next(b for b in bands if b.grade == "BREACH")
    d = breach.to_dict()
    assert d["nett_min"] is None
