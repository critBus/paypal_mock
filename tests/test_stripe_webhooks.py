"""Stripe checkout callbacks must support managed and environment listeners together."""

import hashlib
import hmac
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import app
from stripe import stripe as stripe
from stripe.storage import StripeMockStorage


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(
        stripe, "storage", StripeMockStorage(tmp_path / "stripe.sqlite3")
    )
    monkeypatch.setattr(
        stripe, "STRIPE_MOCK_WEBHOOK_URL", "https://callbacks.test/stripe"
    )
    monkeypatch.setattr(stripe, "STRIPE_MOCK_WEBHOOK_SECRET", "whsec_environment_test")
    monkeypatch.setattr(stripe, "STRIPE_MOCK_STRICT_AUTH", False)
    monkeypatch.setattr(stripe, "STRIPE_MOCK_LIVEMODE", False)
    monkeypatch.setenv("STRIPE_MOCK_WEBHOOK_ATTEMPTS", "1")
    outgoing = []

    class CallbackClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            # Every outgoing request is intercepted, including retries.
            outgoing.append({"url": url, **kwargs})
            return SimpleNamespace(status_code=200, text="OK")

    monkeypatch.setattr(httpx, "AsyncClient", CallbackClient)
    with TestClient(app) as http:
        http.outgoing = outgoing
        yield http


def register_endpoint(client, url="https://callbacks.test/stripe", events=None):
    response = client.post(
        "/v1/webhook_endpoints",
        data={"url": url, "enabled_events[]": events or ["*"]},
    )
    assert response.status_code == 200
    return response.json()


def checkout(client, capture_method="automatic"):
    metadata = {
        "order_request_id": "00000000-0000-4000-8000-000000000001",
        "order_number": "TEST-1",
    }
    form = {
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": "1050",
        "line_items[0][price_data][product_data][name]": "Test payment",
        "payment_intent_data[capture_method]": capture_method,
        **{f"metadata[{key}]": value for key, value in metadata.items()},
        **{
            f"payment_intent_data[metadata][{key}]": value
            for key, value in metadata.items()
        },
    }
    response = client.post("/v1/payment_links", data=form)
    assert response.status_code == 200
    link = response.json()
    assert client.get(link["url"]).status_code == 200
    sessions = client.get(
        "/v1/checkout/sessions", params={"payment_link": link["id"]}
    ).json()["data"]
    response = client.post(
        f"/mock-stripe/checkout/{sessions[0]['id']}/simulate",
        json={"outcome": "success"},
    )
    assert response.status_code == 200
    return response.json(), metadata


def verify_signature(delivery, secret):
    parts = dict(
        part.split("=", 1)
        for part in delivery["headers"]["Stripe-Signature"].split(",")
    )
    signed = parts["t"].encode() + b"." + delivery["content"]
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(parts["v1"], expected)


def test_environment_listener_receives_checkout_alongside_managed_endpoint(client):
    """An old managed endpoint must not suppress a different configuration's listener."""
    endpoint = register_endpoint(client)
    result, metadata = checkout(client)
    assert result["payment_intent"]["status"] == "succeeded"
    assert result["payment_intent"]["amount_received"] == 1050
    assert result["checkout_session"]["payment_status"] == "paid"
    deliveries = stripe.storage.list_webhook_deliveries()
    assert len(deliveries) == 4
    for event_type in ("payment_intent.succeeded", "checkout.session.completed"):
        matching = [d for d in deliveries if d["event_type"] == event_type]
        assert {d["webhook_endpoint_id"] for d in matching} == {
            endpoint["id"],
            "we_environment",
        }
        assert len({d["event_id"] for d in matching}) == 1
    for outgoing, delivery in zip(client.outgoing, reversed(deliveries)):
        secret = (
            endpoint["secret"]
            if delivery["webhook_endpoint_id"] == endpoint["id"]
            else stripe.STRIPE_MOCK_WEBHOOK_SECRET
        )
        verify_signature(outgoing, secret)
        event = json.loads(outgoing["content"])
        assert event["data"]["object"]["metadata"] == metadata
        assert event["livemode"] is False
        assert "_token" not in outgoing["content"].decode()


def test_environment_listener_works_without_managed_endpoints(client):
    checkout(client)
    assert len(client.outgoing) == 2
    for delivery in client.outgoing:
        verify_signature(delivery, stripe.STRIPE_MOCK_WEBHOOK_SECRET)


def test_identical_listener_is_delivered_once(client, monkeypatch):
    endpoint = register_endpoint(client)
    monkeypatch.setattr(stripe, "STRIPE_MOCK_WEBHOOK_SECRET", endpoint["secret"])
    checkout(client)
    assert len(client.outgoing) == 2


def test_environment_listener_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(stripe, "STRIPE_MOCK_WEBHOOK_URL", "")
    endpoint = register_endpoint(client)
    checkout(client)
    assert len(client.outgoing) == 2
    for delivery in client.outgoing:
        verify_signature(delivery, endpoint["secret"])


def test_managed_subscriptions_remain_filtered(client):
    endpoint = register_endpoint(
        client, "https://managed.test/stripe", ["payment_intent.succeeded"]
    )
    disabled = register_endpoint(client, "https://disabled.test/stripe")
    assert (
        client.post(
            f"/v1/webhook_endpoints/{disabled['id']}", data={"disabled": "true"}
        ).status_code
        == 200
    )
    checkout(client)
    managed = [d for d in client.outgoing if d["url"] == endpoint["url"]]
    assert len(client.outgoing) == 3
    assert len(managed) == 1
    assert json.loads(managed[0]["content"])["type"] == "payment_intent.succeeded"
    verify_signature(managed[0], endpoint["secret"])
    assert all(d["url"] != disabled["url"] for d in client.outgoing)


@pytest.mark.parametrize(
    "operation, final_status", [("capture", "succeeded"), ("cancel", "canceled")]
)
def test_manual_authorization_and_terminal_callbacks(client, operation, final_status):
    register_endpoint(client)
    result, _ = checkout(client, "manual")
    intent = result["payment_intent"]
    assert intent["status"] == "requires_capture"
    assert intent["amount_received"] == 0
    assert intent["amount_capturable"] == 1050
    assert result["checkout_session"]["payment_status"] == "unpaid"
    assert {json.loads(d["content"])["type"] for d in client.outgoing} == {
        "payment_intent.amount_capturable_updated",
        "checkout.session.completed",
    }
    assert len(client.outgoing) == 4
    client.outgoing.clear()
    response = client.post(f"/v1/payment_intents/{intent['id']}/{operation}")
    assert response.status_code == 200
    assert response.json()["status"] == final_status
    assert response.json()["amount_received"] == (1050 if operation == "capture" else 0)
    assert len(client.outgoing) == 2
    assert {json.loads(d["content"])["type"] for d in client.outgoing} == {
        f"payment_intent.{final_status}"
    }
