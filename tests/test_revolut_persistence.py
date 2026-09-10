import json
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import app
from revolut import revolut as mock_revolut
from revolut.storage import RevolutStorage


@pytest.fixture
def client():
    return TestClient(
        app,
        headers={"Authorization": f"Bearer {mock_revolut.REVOLUT_MOCK_SECRET_KEY}"},
    )


def reopen_storage(monkeypatch):
    monkeypatch.setattr(
        mock_revolut, "storage", RevolutStorage(mock_revolut.storage.path)
    )


@pytest.mark.parametrize("prefix", ["/api", "/api/1.0"])
def test_webhook_lifecycle_survives_reopening(client, monkeypatch, prefix):
    response = client.post(
        f"{prefix}/webhooks",
        json={"url": "http://example.test/hook", "events": ["ORDER_COMPLETED"]},
    )
    assert response.status_code == 201
    webhook = response.json()
    url = f"{prefix}/webhooks/{webhook['id']}"
    reopen_storage(monkeypatch)
    assert client.get(url).json() == webhook
    assert client.get(f"{prefix}/webhooks").json() == {"webhooks": [webhook]}

    updates = {"url": "http://example.test/new", "events": ["ORDER_AUTHORISED"]}
    assert client.patch(url, json=updates).status_code == 200
    rotated = client.post(f"{url}/rotate-signing-secret").json()["signing_secret"]
    assert rotated != webhook["signing_secret"]
    reopen_storage(monkeypatch)
    assert client.get(url).json() == {**webhook, **updates, "signing_secret": rotated}

    assert client.delete(url).status_code == 204
    reopen_storage(monkeypatch)
    assert client.get(url).status_code == 404
    assert client.get(f"{prefix}/webhooks").json() == {"webhooks": []}
    assert client.delete(url).status_code == 404


def test_order_and_webhook_are_readable_from_another_process(client, monkeypatch):
    order = client.post(
        "/api/orders",
        json={
            "amount": 1234,
            "currency": "EUR",
            "metadata": {"order_number": "LOCAL-1"},
            "expire_pending_after": "PT30M",
        },
    ).json()
    webhook = client.post(
        "/api/webhooks",
        json={"url": "http://example.test/hook", "events": ["ORDER_COMPLETED"]},
    ).json()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys; from revolut.storage import RevolutStorage; "
            "store = RevolutStorage(sys.argv[1]); "
            "print(json.dumps([store.get_order(sys.argv[2]), "
            "store.get_order_by_token(sys.argv[3]), store.list_webhooks()]))",
            str(mock_revolut.storage.path),
            order["id"],
            order["token"],
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stored, by_token, webhooks = json.loads(result.stdout)
    assert stored == {**order, "_expire_pending_after": "PT30M"}
    assert by_token == stored
    assert webhooks == [webhook]
    reopen_storage(monkeypatch)
    assert client.get(f"/api/orders/{order['id']}").json() == order
    assert client.get(f"/mock-revolut/checkout/{order['token']}").status_code == 200


@pytest.mark.parametrize(
    ("action", "body", "expected_state", "refunded", "payment_count"),
    [
        ("capture", {}, "COMPLETED", 0, 1),
        ("cancel", {}, "CANCELLED", 0, 0),
        ("refund", {"amount": 400}, "PENDING", 400, 0),
        ("simulate", {"outcome": "success"}, "COMPLETED", 0, 1),
        ("simulate", {"outcome": "failure"}, "FAILED", 0, 1),
        ("simulate", {"outcome": "declined"}, "DECLINED", 0, 1),
        ("simulate", {"outcome": "authorised"}, "AUTHORISED", 0, 1),
    ],
)
def test_order_mutations_are_saved_before_notifications(
    client, monkeypatch, action, body, expected_state, refunded, payment_count
):
    async def check_committed_order(order, event_type):
        assert RevolutStorage(mock_revolut.storage.path).get_order(order["id"]) == order
        return []

    sender = AsyncMock(side_effect=check_committed_order)
    monkeypatch.setattr(mock_revolut, "_send_order_event", sender)
    order = client.post("/api/orders", json={"amount": 1000, "currency": "EUR"}).json()
    reopen_storage(monkeypatch)
    url = (
        f"/mock-revolut/checkout/{order['token']}/simulate"
        if action == "simulate"
        else f"/api/orders/{order['id']}/{action}"
    )
    assert client.post(url, json=body).status_code == 200
    reopen_storage(monkeypatch)
    saved = client.get(f"/api/orders/{order['id']}").json()
    assert saved["state"] == expected_state
    assert saved["refunded_amount"] == refunded
    assert len(saved["payments"]) == payment_count
    assert saved["outstanding_amount"] == (0 if expected_state == "COMPLETED" else 1000)
    if action in ("capture", "simulate"):
        sender.assert_awaited_once()
    else:
        sender.assert_not_awaited()


def test_notification_uses_persisted_webhook_and_signing_secret(client, monkeypatch):
    import httpx

    webhook = client.post(
        "/api/webhooks",
        json={"url": "http://example.test/persisted", "events": ["ORDER_COMPLETED"]},
    ).json()
    order = client.post("/api/orders", json={"amount": 1000, "currency": "EUR"}).json()
    reopen_storage(monkeypatch)
    deliveries = []

    def receive(request):
        deliveries.append(request)
        return httpx.Response(200, json={"ok": True})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        mock_revolut.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(receive), **kwargs
        ),
    )
    response = client.post(f"/api/orders/{order['id']}/capture")
    assert response.status_code == 200
    assert len(deliveries) == 1
    delivery = deliveries[0]
    assert str(delivery.url) == webhook["url"]
    payload = json.loads(delivery.content)
    assert payload["order_id"] == order["id"]
    assert delivery.headers["Revolut-Signature"] == mock_revolut._webhook_signature(
        payload,
        delivery.headers["Revolut-Request-Timestamp"],
        webhook["signing_secret"],
    )
