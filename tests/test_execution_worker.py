"""Smoke tests for execution_worker auth and routes."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# Ensure secret before app import path resolves auth
os.environ["INTERNAL_AUTH_SECRET"] = "test-secret-token"
os.environ["EC2_STATIC_IP"] = "203.0.113.10"
os.environ["PAPER_TRADING"] = "true"
os.environ.setdefault("BROKER_NAME", "dhan")

from execution_worker.main import app  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def test_health_open(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["static_ip"] == "203.0.113.10"
    assert body["http_pool_ready"] is True
    assert "timestamp" in body


def test_place_rejects_missing_token(client: TestClient) -> None:
    r = client.post(
        "/order/place",
        json={"symbol": "NIFTY", "action": "BUY", "qty": 65, "order_type": "LIMIT"},
    )
    assert r.status_code == 401


def test_place_and_positions(client: TestClient) -> None:
    headers = {"X-Auth-Token": "test-secret-token"}
    r = client.post(
        "/order/place",
        headers=headers,
        json={"symbol": "NIFTY24XXX", "action": "BUY", "qty": 65, "order_type": "LIMIT"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["order"]["status"] == "FILLED"

    pos = client.get("/order/positions", headers=headers)
    assert pos.status_code == 200
    data = pos.json()
    assert data["count"] >= 1
    assert any(p["symbol"] == "NIFTY24XXX" for p in data["positions"])
