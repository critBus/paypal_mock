"""Noon protocol, state, persistence and signed callbacks; all storage/HTTP is isolated."""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app import app
from noon import noon
from noon.storage import NoonStorage

AUTH = (
    "Key " + base64.b64encode(b"mock-business.mock-app:mock-application-key").decode()
)


@pytest.fixture
def client(monkeypatch):
    for name in (
        "BUSINESS_IDENTIFIER",
        "APPLICATION_IDENTIFIER",
        "APPLICATION_KEY",
        "CATEGORIES",
        "CHECKOUT_BASE_URL",
        "WEBHOOK_URL",
        "WEBHOOK_SECRET",
        "WEBHOOK_KEY_ID",
    ):
        monkeypatch.delenv("NOON_MOCK_" + name, raising=False)
    monkeypatch.setenv("NOON_MOCK_CHECKOUT_BASE_URL", "https://checkout.test")
    outgoing = []

    class CallbackClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            outgoing.append({"url": url, **kwargs})
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(httpx, "AsyncClient", CallbackClient)
    with TestClient(app, headers={"Authorization": AUTH}) as http:
        http.outgoing = outgoing
        yield http


def create(client, manual=False, reference=None, **changes):
    payload = {
        "apiOperation": "INITIATE",
        "order": {
            "amount": "10.125",
            "currency": "KWD",
            "reference": reference or uuid4().hex,
            "name": "Mock order",
            "category": "pay",
            "channel": "WEB",
        },
        "configuration": {
            "returnUrl": "https://merchant.test/return",
            "webhookUrl": "https://callbacks.test/noon",
            "paymentAction": "AUTHORIZE" if manual else "SALE",
        },
    }
    for key, value in changes.items():
        payload[key].update(value)
    return client.post("/payment/v1/order", json=payload)


def order(client, **kwargs):
    response = create(client, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()["result"]["order"]


def simulate(client, remote, outcome="success", **kwargs):
    return client.post(
        f"/mock-noon/orders/{remote['id']}/simulate",
        json={"outcome": outcome, **kwargs},
    )


def capture(client, remote, reference="capture-1", **changes):
    transaction = {
        "amount": remote["amount"],
        "currency": remote["currency"],
        "transactionReference": reference,
        "finalCapture": True,
        **changes,
    }
    return client.post(
        "/payment/v1/order",
        json={
            "apiOperation": "CAPTURE",
            "order": {"id": remote["id"]},
            "transaction": transaction,
        },
    )


def get(client, remote):
    return client.get(f"/payment/v1/order/{remote['id']}")


def controls(client, scenario="normal", remote=None, **kwargs):
    response = client.post(
        "/mock-noon/controls",
        json={
            "scenario": scenario,
            "order_id": str(remote["id"]) if remote else None,
            **kwargs,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def decode_delivery(delivery):
    parts = delivery["json"]["data"].split(".")

    def decode(value):
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))

    return parts, decode(parts[0]), decode(parts[1])


def test_sale_checkout_exact_amount_webhook_and_get_by_reference(client):
    created = create(client)
    remote = created.json()["result"]["order"]
    checkout = created.json()["result"]["checkoutData"]["postUrl"]
    assert checkout.startswith("https://checkout.test/mock-noon/checkout/")
    assert client.get(checkout).status_code == 200
    assert remote["status"] == "INITIATED"
    assert not client.outgoing
    assert simulate(client, remote).json()["order"]["status"] == "CAPTURED"
    stored = get(client, remote).json()["result"]["order"]
    assert Decimal(stored["totalCapturedAmount"]) == Decimal("10.125")
    assert (
        client.get("/payment/v1/order/getbyreference/" + remote["reference"]).json()[
            "result"
        ]["order"]
        == stored
    )
    parts, header, event = decode_delivery(client.outgoing[0])
    assert header["alg"] == "HS256" and header["kid"] == "mock-noon-key"
    expected = (
        base64.urlsafe_b64encode(
            hmac.new(
                b"mock-noon-webhook-secret",
                f"{parts[0]}.{parts[1]}".encode(),
                hashlib.sha256,
            ).digest()
        )
        .decode()
        .rstrip("=")
    )
    assert hmac.compare_digest(expected, parts[2])
    assert event["orderId"] == remote["id"]
    assert event["merchantOrderRef"] == remote["reference"]
    assert event["orderStatus"] == "CAPTURED"
    assert client.get("/mock-noon/webhook-deliveries").json()[0]["status"] == "accepted"


def test_manual_authorization_capture_no_second_charge_and_delayed_replay(client):
    remote = order(client, manual=True)
    assert capture(client, remote).status_code == 409
    assert simulate(client, remote).json()["order"]["status"] == "AUTHORIZED"
    authorization = decode_delivery(client.outgoing[-1])[2]
    assert get(client, remote).json()["result"]["order"]["totalCapturedAmount"] == "0"
    assert capture(client, remote).json()["result"]["order"]["status"] == "CAPTURED"
    for reference in ("capture-1", "new-reference"):
        assert capture(client, remote, reference).json()["resultCode"] == 19069
    assert len(client.outgoing) == 2
    response = client.post(
        f"/mock-noon/orders/{remote['id']}/replay",
        json={"event_id": authorization["eventId"]},
    )
    assert response.json()["order_status"] == "AUTHORIZED"
    assert decode_delivery(client.outgoing[-1])[2] == authorization
    assert get(client, remote).json()["result"]["order"]["status"] == "CAPTURED"


def test_capture_rejects_wrong_amount_currency_and_non_final(client):
    remote = order(client, manual=True)
    simulate(client, remote)
    for changes in ({"amount": "10.124"}, {"currency": "USD"}, {"finalCapture": False}):
        assert capture(client, remote, **changes).json()["resultCode"] == 19068
    assert (
        capture(client, remote).json()["result"]["order"]["totalCapturedAmount"]
        == "10.125"
    )


@pytest.mark.parametrize(
    "status",
    [
        "INITIATED",
        "PAYMENT_INFO_ADDED",
        "3DS_ENROLL_CHECKED",
        "3DS_RESULT_VERIFIED",
        "AUTHENTICATED",
    ],
)
def test_cancel_eligible_states(client, status):
    remote = order(client)
    if status != "INITIATED":
        simulate(client, remote, status)
    result = client.post(
        "/payment/v1/order",
        json={"apiOperation": "CANCEL", "order": {"id": remote["id"]}},
    )
    assert result.json()["result"]["order"]["status"] == "CANCELLED"
    assert simulate(client, remote).status_code == 409


@pytest.mark.parametrize("status", ["AUTHORIZED", "CAPTURED", "PENDING", "TOKENIZED"])
def test_cancel_rejects_unsafe_states(client, status):
    remote = order(client)
    simulate(client, remote, status)
    result = client.post(
        "/payment/v1/order",
        json={"apiOperation": "CANCEL", "order": {"id": remote["id"]}},
    )
    assert result.json()["resultCode"] == 19019
    assert get(client, remote).json()["result"]["order"]["status"] == status


@pytest.mark.parametrize("offset", [None, 3, -3])
def test_native_expiry_blocks_checkout_and_emits_once(client, offset):
    instant = datetime.now(timezone.utc) - timedelta(seconds=1)
    past = (
        instant.strftime("%Y-%m-%dT%H:%M:%S")
        if offset is None
        else instant.astimezone(timezone(timedelta(hours=offset))).isoformat()
    )
    created = create(client, configuration={"initiationValidity": past})
    remote = created.json()["result"]["order"]
    assert (
        client.get(created.json()["result"]["checkoutData"]["postUrl"]).status_code
        == 200
    )
    assert get(client, remote).json()["result"]["order"]["status"] == "EXPIRED"
    assert simulate(client, remote).status_code == 409
    assert len(client.outgoing) == 1
    assert decode_delivery(client.outgoing[0])[2]["orderStatus"] == "EXPIRED"


@pytest.mark.parametrize(
    "status",
    [
        "FAILED",
        "REJECTED",
        "AUTHENTICATED",
        "PENDING",
        "MARKED_FOR_REVIEW",
        "PARTIALLY_CAPTURED",
        "UNKNOWN",
    ],
)
def test_all_consumed_remote_statuses(client, status):
    remote = order(client)
    assert simulate(client, remote, status).json()["order"]["status"] == status
    assert get(client, remote).json()["result"]["order"]["status"] == status


@pytest.mark.parametrize(
    "outcome,expected",
    [("pending", "PENDING"), ("declined", "AUTHORIZED"), ("lost_response", "CAPTURED")],
)
def test_unknown_capture_outcomes_are_durable_and_not_submitted_again(
    client, outcome, expected
):
    remote = order(client, manual=True)
    simulate(client, remote)
    controls(client, "capture_" + outcome, remote)
    response = capture(client, remote)
    assert response.status_code == (
        200 if outcome == "pending" else 503 if outcome == "lost_response" else 409
    )
    assert get(client, remote).json()["result"]["order"]["status"] == expected
    assert capture(client, remote, "another-reference").json()["resultCode"] == 19069
    if outcome == "pending":
        assert (
            simulate(client, remote, "CAPTURED").json()["order"]["status"] == "CAPTURED"
        )


def test_initiate_unknown_response_reference_recovery_and_duplicate_guard(client):
    controls(client, "initiate_lost_response")
    assert create(client, reference="stable-reference").status_code == 503
    recovered = client.get("/payment/v1/order/getbyreference/stable-reference")
    assert recovered.status_code == 200
    assert recovered.json()["result"]["order"]["status"] == "INITIATED"
    duplicate = create(client, reference="stable-reference")
    assert duplicate.json()["resultCode"] == 19012
    assert len(client.get("/mock-noon/orders").json()) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "wrong_amount",
        "wrong_currency",
        "wrong_id",
        "wrong_reference",
        "missing_order",
        "captured_amount_mismatch",
        "invalid_json",
        "api_error",
        "delay",
    ],
)
def test_faults_are_consumed_without_corrupting_stored_truth(client, kind):
    remote = order(client)
    controls(client, remote=remote, faults={"GET": {"kind": kind}})
    broken = get(client, remote)
    if kind == "api_error":
        assert broken.status_code == 503
    elif kind == "invalid_json":
        assert broken.text == "invalid-json"
    elif kind == "missing_order":
        assert "order" not in broken.json()["result"]
    elif kind != "delay":
        assert broken.json()["result"]["order"] != remote
    assert get(client, remote).json()["result"]["order"] == remote


def test_missing_checkout_recovers_on_get_and_defaults_do_not_change_existing_orders(
    client,
):
    old = order(client)
    controls(client, "missing_checkout")
    result = create(client)
    assert "checkoutData" not in result.json()["result"]
    remote = result.json()["result"]["order"]
    assert "checkoutData" in get(client, remote).json()["result"]
    assert noon.storage.get(old["id"])["_controls"] == {}


@pytest.mark.parametrize(
    "mode",
    ["suppress", "duplicate", "bad_signature", "wrong_kid", "wrong_alg", "malformed"],
)
def test_callback_modes_keep_order_truth_and_original_event(client, mode):
    remote = order(client)
    controls(client, remote=remote, webhook_mode=mode)
    simulate(client, remote)
    assert get(client, remote).json()["result"]["order"]["status"] == "CAPTURED"
    assert len(client.outgoing) == (
        0 if mode == "suppress" else 2 if mode == "duplicate" else 1
    )
    if mode == "duplicate":
        assert client.outgoing[0]["json"] == client.outgoing[1]["json"]
    elif mode not in {"suppress", "malformed"}:
        parts, header, _ = decode_delivery(client.outgoing[0])
        if mode == "wrong_kid":
            assert header["kid"] != "mock-noon-key"
        elif mode == "wrong_alg":
            assert header["alg"] != "HS256"
        else:
            signature = (
                base64.urlsafe_b64encode(
                    hmac.new(
                        b"mock-noon-webhook-secret",
                        f"{parts[0]}.{parts[1]}".encode(),
                        hashlib.sha256,
                    ).digest()
                )
                .decode()
                .rstrip("=")
            )
            assert signature != parts[2]
    assert noon.storage.get(remote["id"])["_events"][0]["orderStatus"] == "CAPTURED"


def test_storage_restart_preserves_defaults_orders_capture_and_events(
    client, monkeypatch
):
    controls(client, "no_webhook")
    remote = order(client, manual=True)
    simulate(client, remote)
    capture(client, remote)
    monkeypatch.setattr(noon, "storage", NoonStorage(noon.storage.path))
    assert get(client, remote).json()["result"]["order"]["status"] == "CAPTURED"
    assert capture(client, remote).json()["resultCode"] == 19069
    assert len(noon.storage.get(remote["id"])["_events"]) == 2
    assert noon.storage.defaults()["webhook_mode"] == "suppress"


def test_auth_category_errors_invalid_requests_and_dashboard(client):
    assert client.get("/mock-noon").status_code == 200
    assert (
        create(client, order={"category": "unconfigured"}).json()["resultCode"] == 19014
    )
    for header in ("", "Bearer wrong"):
        assert (
            client.get(
                "/payment/v1/order/123", headers={"Authorization": header}
            ).status_code
            == 401
        )
    for body in ("null", "[]", "{}", "{", '{"apiOperation":[],"order":{}}'):
        assert client.post("/payment/v1/order", content=body).status_code == 400
    for amount in ("NaN", "Infinity", "-1", "0", "10.1234"):
        assert create(client, order={"amount": amount}).status_code == 400
    assert client.get("/payment/v1/order/123").json()["resultCode"] == 19001
    assert (
        client.get("/payment/v1/order/getbyreference/absent").json()["resultCode"]
        == 19001
    )
    assert (
        client.post("/mock-noon/controls", json={"scenario": "unknown"}).status_code
        == 422
    )


def test_override_callback_destination_and_connection_errors_are_observable(
    client, monkeypatch
):
    monkeypatch.setenv("NOON_MOCK_WEBHOOK_URL", "http://127.0.0.1:8001/local-only")
    remote = order(client)
    simulate(client, remote)
    assert client.outgoing[0]["url"] == "http://127.0.0.1:8001/local-only"

    async def failed(*args, **kwargs):
        raise httpx.ConnectError("Do not log callback details")

    monkeypatch.setattr(httpx.AsyncClient, "post", failed)
    client.post(f"/mock-noon/orders/{remote['id']}/replay", json={})
    attempts = noon.storage.deliveries()
    assert attempts[0]["status"] == "connection_error"
    assert "Do not log callback details" not in json.dumps(attempts)


def test_definitive_initiate_error_does_not_create_remote_order(client):
    controls(
        client,
        faults={
            "INITIATE": {"kind": "api_error", "result_code": 1505, "http_status": 403}
        },
    )
    assert create(client).json()["resultCode"] == 1505
    assert client.get("/mock-noon/orders").json() == []


def test_concurrent_capture_commits_only_one_financial_transition(client):
    from concurrent.futures import ThreadPoolExecutor

    remote = order(client, manual=True)
    simulate(client, remote)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda ref: capture(client, remote, ref), ["first", "second"])
        )
    assert sorted(r.status_code for r in responses) == [200, 409]
    stored = noon.storage.get(remote["id"])
    assert (
        len(
            [event for event in stored["_events"] if event["orderStatus"] == "CAPTURED"]
        )
        == 1
    )
    assert Decimal(stored["totalCapturedAmount"]) == Decimal("10.125")


@pytest.mark.parametrize(
    "currency,amount,status",
    [
        ("USD", "10.12", 200),
        ("USD", "10.125", 400),
        ("JPY", "10", 200),
        ("JPY", "10.1", 400),
        ("IQD", "10.125", 200),
        ("LYD", "10.125", 200),
    ],
)
def test_zero_two_and_three_decimal_currencies(client, currency, amount, status):
    assert (
        create(client, order={"currency": currency, "amount": amount}).status_code
        == status
    )
