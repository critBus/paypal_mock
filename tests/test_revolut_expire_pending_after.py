import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import app
from revolut import revolut as mock_revolut


@pytest.fixture(autouse=True)
def reset_revolut_orders():
    mock_revolut.orders.clear()
    mock_revolut.order_ids_by_token.clear()
    yield
    mock_revolut.orders.clear()
    mock_revolut.order_ids_by_token.clear()


@pytest.fixture
def client():
    return TestClient(app)


def create_order(client, **extra_payload):
    return client.post(
        "/api/orders",
        headers={"Authorization": f"Bearer {mock_revolut.REVOLUT_MOCK_SECRET_KEY}"},
        json={"amount": 1000, "currency": "EUR", **extra_payload},
    )


def test_create_order_allows_expiration_to_be_omitted(client):
    response = create_order(client)

    assert response.status_code == 201
    assert "_expire_pending_after" not in mock_revolut.orders[response.json()["id"]]


@pytest.mark.parametrize("duration", ["PT30M", "PT24H", "P7D"])
def test_create_order_accepts_supported_expiration_formats(client, duration):
    response = create_order(client, expire_pending_after=duration)

    assert response.status_code == 201
    assert mock_revolut.orders[response.json()["id"]]["_expire_pending_after"] == duration


@pytest.mark.parametrize(
    "duration",
    ["", " ", None, 30, "PT0M", "PT0H", "P0D", "30M", "P1DT2H", "PT30S"],
)
def test_create_order_rejects_empty_or_invalid_expiration(client, duration):
    response = create_order(client, expire_pending_after=duration)

    assert response.status_code == 400
    assert "expire_pending_after" in response.json()["detail"]
