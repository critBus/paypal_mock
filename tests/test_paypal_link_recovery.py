"""Hosted links remain queryable when callbacks are omitted, late, duplicated or lost."""

import asyncio
import base64
import json
import subprocess
import sys
import zlib
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi.testclient import TestClient

from app import app
from paypal import paypal as core


@pytest.fixture
def client(monkeypatch):
    deliveries = []

    class CallbackClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            deliveries.append({"url": url, **kwargs})
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(httpx, "AsyncClient", CallbackClient)
    monkeypatch.setattr(core, "PAYPAL_MOCK_WEBHOOK_DELAY_SECONDS", 0)
    monkeypatch.setattr(
        core, "PAYPAL_MOCK_WEBHOOK_URL", "http://callback.invalid/paypal"
    )
    monkeypatch.setattr(core, "PAYMENT_RESOURCE_LOCK", asyncio.Lock())
    with TestClient(app) as client:
        client.deliveries = deliveries
        yield client


def configure(client, **config):
    response = client.post("/mock/paypal/controls", json=config)
    assert response.status_code == 200, response.text
    return response.json()


def create_link(client, reference=None, value="80.02", currency="USD"):
    reference = reference or str(uuid4())
    response = client.post(
        "/v1/checkout/payment-resources",
        json={
            "integration_mode": "LINK",
            "type": "BUY_NOW",
            "reusable": "MULTIPLE",
            "return_url": "http://localhost/success?con=S00000116",
            "line_items": [
                {
                    "name": "Payment request S00000116",
                    "description": "SHOP-123",
                    "product_id": reference,
                    "unit_amount": {"value": value, "currency_code": currency},
                    "adjustable_quantity": {"maximum": 1},
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], reference


def pay(client, identifier):
    response = client.post(f"/ncp/payment/{identifier}/complete")
    assert response.status_code == 200, response.text


def events(client, **params):
    response = client.get(
        "/v1/notifications/webhooks-events",
        params={
            "event_type": "CHECKOUT.PAYMENT-RESOURCE.PAYMENT-COMPLETED",
            **params,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def transactions(client, **params):
    now = core.utc_now()
    response = client.get(
        "/v1/reporting/transactions",
        params={
            "start_date": core.paypal_datetime(now - timedelta(days=1)),
            "end_date": core.paypal_datetime(now + timedelta(seconds=1)),
            "fields": "transaction_info,cart_info",
            "page_size": 500,
            **params,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_payment_without_webhook_is_recoverable_by_events_and_reporting(client):
    configure(client, scenario="no_webhook", payer_email="actual-payer@example.com")
    identifier, reference = create_link(client)
    pay(client, identifier)
    assert client.deliveries == []
    event = events(client)["events"][0]
    resource = event["resource"]
    assert resource["payment_resource_id"] == identifier
    assert resource["transaction_status"] == "COMPLETED"
    assert resource["gross_amount"] == {"value": "80.02", "currency_code": "USD"}
    assert resource["payer"]["email_address"] == "actual-payer@example.com"
    assert (
        client.get(f"/v1/notifications/webhooks-events/{event['id']}").json() == event
    )
    detail = transactions(client)["transaction_details"][0]
    assert detail["cart_info"]["item_details"][0]["item_code"] == reference
    assert detail["transaction_info"]["transaction_id"] == resource["transaction_id"]
    assert detail["transaction_info"]["transaction_status"] == "S"
    # A paid resource remains ACTIVE until the integration cleans it up.
    assert (
        client.get(f"/v1/checkout/payment-resources/{identifier}").json()["status"]
        == "ACTIVE"
    )


def test_event_and_transaction_delays_can_exercise_bounded_reconciliation_retries(
    client, monkeypatch
):
    configure(
        client,
        scenario="no_webhook",
        event_list_delay_seconds=60,
        transaction_search_delay_seconds=180,
    )
    identifier, _ = create_link(client)
    pay(client, identifier)
    paid_at = core.utc_now()
    assert events(client)["events"] == []
    assert transactions(client)["transaction_details"] == []
    monkeypatch.setattr(core, "utc_now", lambda: paid_at + timedelta(seconds=61))
    assert len(events(client)["events"]) == 1
    assert transactions(client)["transaction_details"] == []
    monkeypatch.setattr(core, "utc_now", lambda: paid_at + timedelta(seconds=181))
    assert len(transactions(client)["transaction_details"]) == 1


def test_pending_resource_is_not_mistaken_for_a_paid_transaction(client):
    configure(client, scenario="no_webhook")
    create_link(client)
    assert events(client)["events"] == []
    assert transactions(client)["transaction_details"] == []


def test_paid_history_survives_link_deletion_and_repeated_payment_is_blocked(client):
    configure(client, scenario="no_webhook")
    identifier, _ = create_link(client)
    pay(client, identifier)
    original_event = events(client)["events"][0]
    assert client.post(f"/ncp/payment/{identifier}/complete").status_code == 409
    assert (
        client.delete(f"/v1/checkout/payment-resources/{identifier}").status_code == 204
    )
    assert (
        client.delete(f"/v1/checkout/payment-resources/{identifier}").status_code == 204
    )
    assert client.get(f"/v1/checkout/payment-resources/{identifier}").status_code == 404
    assert client.post(f"/ncp/payment/{identifier}/complete").status_code == 410
    assert events(client)["events"] == [original_event]
    assert len(transactions(client)["transaction_details"]) == 1
    all_events = client.get("/v1/notifications/webhooks-events").json()["events"]
    assert (
        len([event for event in all_events if event["event_type"].endswith(".DELETED")])
        == 1
    )


def test_late_webhook_reuses_queried_event_id_and_valid_signature(client):
    configure(client, scenario="no_webhook")
    identifier, _ = create_link(client)
    pay(client, identifier)
    original = events(client)["events"][0]
    configure(client, resource_id=identifier, scenario="normal")
    response = client.post(
        f"/mock/paypal/payment-resources/{identifier}/replay",
        json={"event": "completed"},
    )
    assert response.status_code == 200
    assert response.json()["event_id"] == original["id"]
    delivery = client.deliveries[-1]
    assert json.loads(delivery["content"]) == original
    assert events(client)["events"] == [original]
    headers, body = delivery["headers"], delivery["content"]
    message = f"{headers['Paypal-Transmission-Id']}|{headers['Paypal-Transmission-Time']}|{core.PAYPAL_MOCK_WEBHOOK_ID}|{zlib.crc32(body)}".encode()
    core._WEBHOOK_CERTIFICATE.public_key().verify(
        base64.b64decode(headers["Paypal-Transmission-Sig"]),
        message,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_event_and_transaction_pagination_preserve_query_filters(client):
    configure(client, scenario="no_webhook")
    for _ in range(3):
        identifier, _ = create_link(client)
        pay(client, identifier)
    first = events(client, page_size=2)
    assert first["total_pages"] == 2
    next_url = next(link["href"] for link in first["links"] if link["rel"] == "next")
    second = client.get(next_url).json()
    assert len(first["events"]) == 2 and len(second["events"]) == 1
    assert set(event["id"] for event in first["events"]).isdisjoint(
        event["id"] for event in second["events"]
    )
    first = transactions(client, page_size=2)
    next_url = next(link["href"] for link in first["links"] if link["rel"] == "next")
    assert len(client.get(next_url).json()["transaction_details"]) == 1
    assert transactions(client, transaction_currency="EUR")["transaction_details"] == []


@pytest.mark.parametrize(
    ("operation", "path", "params"),
    [
        ("events", "/v1/notifications/webhooks-events", {}),
        (
            "transactions",
            "/v1/reporting/transactions",
            {"start_date": "2026-01-01T00:00:00Z", "end_date": "2026-01-02T00:00:00Z"},
        ),
    ],
)
def test_global_search_api_errors_are_configurable_and_one_shot(
    client, operation, path, params
):
    configure(
        client,
        faults={
            operation: {
                "name": "NOT_AUTHORIZED",
                "issue": "PERMISSION_DENIED",
                "http_status": 403,
                "times": 1,
            }
        },
    )
    assert client.get(path, params=params).status_code == 403
    assert client.get(path, params=params).status_code == 200


def test_exact_reporting_not_ready_response_used_by_crinnopayments(client):
    message = "Data for the given start date is not available."
    configure(
        client,
        faults={
            "transactions": {
                "name": "INVALID_REQUEST",
                "issue": "INVALID_REQUEST",
                "message": message,
                "http_status": 404,
                "times": 1,
            }
        },
    )
    response = client.get(
        "/v1/reporting/transactions",
        params={
            "start_date": "2026-01-01T00:00:00Z",
            "end_date": "2026-01-02T00:00:00Z",
        },
    )
    assert response.status_code == 404
    assert response.json()["message"] == message


def test_resource_get_and_delete_faults_do_not_invent_payment_or_remove_link(client):
    configure(client, scenario="no_webhook")
    identifier, _ = create_link(client)
    configure(
        client,
        resource_id=identifier,
        faults={
            "delete": {"http_status": 503, "times": 1},
            "get": {"http_status": 500, "times": 1},
        },
    )
    assert client.get(f"/v1/checkout/payment-resources/{identifier}").status_code == 500
    assert client.get(f"/v1/checkout/payment-resources/{identifier}").status_code == 200
    assert (
        client.delete(f"/v1/checkout/payment-resources/{identifier}").status_code == 503
    )
    assert core.PAYMENT_RESOURCES[identifier]["status"] == "ACTIVE"
    assert transactions(client)["transaction_details"] == []


def test_paid_resources_and_remote_event_ids_survive_process_restart(client):
    configure(client, scenario="no_webhook")
    identifier, _ = create_link(client)
    pay(client, identifier)
    event_id = events(client)["events"][0]["id"]
    result = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; from mock_storage import ResourceStore; import sys; "
            "links=ResourceStore('paypal','payment_resources'); links.path=Path(sys.argv[1]); "
            "events=ResourceStore('paypal','webhook_events'); events.path=links.path; "
            "assert links[sys.argv[2]]['payment_completed']; assert events[sys.argv[3]]['event']['id']==sys.argv[3]; print('persisted')",
            str(core.PAYMENT_RESOURCES.path),
            identifier,
            event_id,
        ],
        text=True,
    )
    assert result.strip() == "persisted"


def test_controls_panel_lists_links_and_distinguishes_hosted_checkout(client):
    configure(client, scenario="no_webhook")
    identifier, _ = create_link(client)
    resources = client.get("/mock/paypal/controls").json()["resources"]
    assert resources[0]["id"] == identifier
    assert (
        client.post(
            "/mock/paypal/controls",
            json={"resource_id": identifier, "status": "COMPLETED"},
        ).status_code
        == 422
    )
    page = client.get("/mock/scenarios").text
    assert "event-delay" in page and "report-delay" in page
    assert "/ncp/payment/" in page


def test_invalid_time_ranges_are_rejected_instead_of_returning_other_payments(client):
    assert (
        client.get(
            "/v1/notifications/webhooks-events", params={"start_time": "invalid"}
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/v1/notifications/webhooks-events", params={"page_size": 0}
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/v1/reporting/transactions",
            params={
                "start_date": "2026-02-01T00:00:00Z",
                "end_date": "2026-01-01T00:00:00Z",
            },
        ).status_code
        == 400
    )


def test_duplicate_webhook_deliveries_share_one_queryable_remote_event(client):
    configure(client, scenario="duplicate_webhook")
    identifier, _ = create_link(client)
    pay(client, identifier)
    saved = events(client)["events"]
    completed = [
        json.loads(delivery["content"])
        for delivery in client.deliveries
        if json.loads(delivery["content"])["event_type"].endswith(".PAYMENT-COMPLETED")
    ]
    assert len(saved) == 1
    assert len(completed) == 2
    assert completed[0]["id"] == completed[1]["id"] == saved[0]["id"]
