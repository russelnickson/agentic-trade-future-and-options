"""PyTest suite for the EC2 execution worker API endpoints."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("INTERNAL_AUTH_SECRET", "test-secret-token")
os.environ.setdefault("EC2_STATIC_IP", "203.0.113.10")
os.environ["PAPER_TRADING"] = "true"
os.environ.setdefault("BROKER_NAME", "dhan")

from execution_worker.main import app  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "timestamp" in body


def test_unauthorized_order(client: TestClient) -> None:
    response = client.post(
        "/order/place",
        json={
            "symbol": "NIFTY24500CE",
            "action": "BUY",
            "qty": 65,
            "order_type": "LIMIT",
        },
    )
    assert response.status_code == 401


def test_valid_order_placement(client: TestClient) -> None:
    response = client.post(
        "/order/place",
        headers={"X-Auth-Token": "test-secret-token"},
        json={
            "symbol": "NIFTY24500CE",
            "action": "BUY",
            "qty": 65,
            "order_type": "LIMIT",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["order"]["symbol"] == "NIFTY24500CE"
    assert body["order"]["transaction_type"] == "BUY"
    assert body["order"]["qty"] == 65
    assert body["order"]["status"] == "FILLED"
    assert body["order"]["paper_trading"] is True
