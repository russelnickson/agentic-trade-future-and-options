"""Runtime mode: local paper vs EC2 live."""

from __future__ import annotations

import os

from config import runtime_mode


def test_local_defaults_to_paper(monkeypatch) -> None:
    runtime_mode.clear_runtime_cache()
    monkeypatch.delenv("DESK_ROLE", raising=False)
    monkeypatch.delenv("TRADE_RUNTIME", raising=False)
    monkeypatch.delenv("PAPER_TRADING", raising=False)
    monkeypatch.delenv("ALLOW_LOCAL_LIVE", raising=False)
    monkeypatch.setattr(runtime_mode, "_looks_like_ec2", lambda: False)
    runtime_mode.clear_runtime_cache()
    assert runtime_mode.desk_role() == "local_paper"
    assert runtime_mode.paper_trading_enabled() is True
    assert runtime_mode.live_dhan_execution() is False
    assert runtime_mode.auto_execute_default() is False


def test_ec2_live_when_marked(monkeypatch) -> None:
    runtime_mode.clear_runtime_cache()
    monkeypatch.setenv("DESK_ROLE", "ec2_live")
    monkeypatch.setenv("PAPER_TRADING", "false")
    monkeypatch.setenv("TRADE_AUTO_EXECUTE", "1")
    runtime_mode.clear_runtime_cache()
    assert runtime_mode.desk_role() == "ec2_live"
    assert runtime_mode.paper_trading_enabled() is False
    assert runtime_mode.live_dhan_execution() is True
    assert runtime_mode.auto_execute_default() is True
