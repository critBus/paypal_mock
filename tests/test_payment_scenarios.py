"""Deterministic provider errors, durable financial truth and callback delivery contracts."""

import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app import app
from mock_controls import DEFAULTS, SCENARIOS
from mock_storage import ResourceStore
from paypal import paypal as paypal
from virtualpos import virtualpos as virtualpos


@pytest.fixture
def client(monkeypatch):
    """All outgoing callbacks are intercepted; no request can notify a running local project."""
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
            return SimpleNamespace(status_code=200, text="OK", is_success=True)

    monkeypatch.setattr(httpx, "AsyncClient", CallbackClient)
    scheduled = Mock()
    monkeypatch.setattr(paypal, "schedule_webhook", scheduled)
    with TestClient(app) as http:
        http.outgoing = outgoing
        http.scheduled = scheduled
        yield http


def paypal_order(client, reference="MERC-TEST-1234"):
    response = client.post(
        "/v2/checkout/orders",
        json={
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": reference,
                    "amount": {"currency_code": "USD", "value": "80.02"},
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "return_url": "http://shop.test/success",
                        "cancel_url": "http://shop.test/cancel",
                    }
                }
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def configure(client, provider, resource_id=None, **kwargs):
    data = {**kwargs, **({"resource_id": resource_id} if resource_id else {})}
    response = client.post(f"/mock/{provider}/controls", json=data)
    assert response.status_code == 200, response.text
    return response.json()


def capture(client, order_id, request_id="capture-1"):
    return client.post(
        f"/v2/checkout/orders/{order_id}/capture",
        headers={"Authorization": "Bearer mock", "PayPal-Request-Id": request_id},
    )


def virtualpos_payment(client):
    response = client.post(
        "/v3/payment",
        json={
            "amount": 18523,
            "email": "client@example.com",
            "social_id": "12345678-5",
            "first_name": "Test",
            "last_name": "Customer",
            "phone": "56912345678",
            "description": "MERC-TEST-1234",
            "merchant_internal_code": "S00000123",
            "merchant_internal_channel": "crinnopayments",
            "return_url": "http://shop.test/return",
            "callback_url": "http://callbacks.test/api/virtualpos/?con=S00000123",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["payment"]["order"]["uuid"]


def test_dashboard_loads_and_provider_controls_are_separate(client):
    response = client.get("/mock/scenarios")
    assert response.status_code == 200
    assert "Escenarios de pagos" in response.text
    assert "capture_lost_response" in response.text
    assert "reject_authentication" in response.text
    configure(client, "paypal", scenario="capture_declined")
    assert (
        client.get("/mock/virtualpos/controls").json()["defaults"]["scenario"]
        == "normal"
    )


def test_paypal_approval_without_webhook_can_be_recovered_and_get_contains_the_capture(
    client,
):
    configure(client, "paypal", scenario="approved_no_webhook")
    order = paypal_order(client)
    client.post(f"/mock/paypal/orders/{order}/approve")
    assert client.scheduled.call_args.kwargs["mode"] == "suppress"
    assert client.get(f"/v2/checkout/orders/{order}").json()["status"] == "APPROVED"
    response = capture(client, order)
    assert response.status_code == 201
    result = client.get(f"/v2/checkout/orders/{order}").json()
    assert result["status"] == "COMPLETED"
    assert (
        result["purchase_units"][0]["payments"]["captures"][0]["status"] == "COMPLETED"
    )
    assert result["purchase_units"][0]["payments"]["captures"][0]["amount"] == {
        "currency_code": "USD",
        "value": "80.02",
    }
    assert (
        client.scheduled.call_args.kwargs["event_type"] == "PAYMENT.CAPTURE.COMPLETED"
    )


def test_unapproved_order_is_not_captured_and_idempotency_never_creates_two_charges(
    client,
):
    order = paypal_order(client)
    denied = capture(client, order)
    assert denied.status_code == 422
    assert denied.json()["details"][0]["issue"] == "ORDER_NOT_APPROVED"
    configure(client, "paypal", order, status="APPROVED")
    first = capture(client, order)
    repeat = capture(client, order)
    assert first.status_code == 201
    assert repeat.status_code == 200
    assert first.json() == repeat.json()
    duplicate = capture(client, order, "new-request-id")
    assert duplicate.status_code == 422
    assert duplicate.json()["details"][0]["issue"] == "ORDER_ALREADY_CAPTURED"
    stored = paypal.ORDERS_PAYPAL[order]
    assert stored["mock"]["financial_captures"] == 1
    assert stored["mock"]["calls"]["capture"] == 4
    assert client.scheduled.call_count == 1


def test_concurrent_captures_with_different_keys_charge_once(client):
    order = paypal_order(client)
    configure(client, "paypal", order, status="APPROVED")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda key: capture(client, order, key).status_code, ["one", "two"]
            )
        )
    assert sorted(responses) == [201, 422]
    assert paypal.ORDERS_PAYPAL[order]["mock"]["financial_captures"] == 1


@pytest.mark.parametrize(
    "scenario,name",
    [
        ("unavailable", "RESOURCE_NOT_FOUND"),
        ("invalid_resource", "INVALID_RESOURCE_ID"),
    ],
)
def test_structured_404_is_distinct_from_html_and_preserves_diagnostic_fields(
    client, scenario, name
):
    order = paypal_order(client)
    configure(client, "paypal", order, scenario=scenario)
    response = client.get(f"/v2/checkout/orders/{order}")
    assert response.status_code == 404
    assert response.json()["name"] == name
    assert response.json()["debug_id"] == response.headers["Paypal-Debug-Id"]
    assert response.json()["details"][0]["issue"] == "INVALID_RESOURCE_ID"
    assert capture(client, order).status_code == 404
    assert "capture_id" in paypal.ORDERS_PAYPAL[order]
    assert not paypal.ORDERS_PAYPAL[order]["capture_id"]
    configure(client, "paypal", order, scenario="html404")
    response = client.get(f"/v2/checkout/orders/{order}")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")


def test_unknown_paypal_order_returns_real_error_envelope(client):
    response = client.get("/v2/checkout/orders/missing")
    assert response.status_code == 404
    assert response.json()["name"] == "RESOURCE_NOT_FOUND"
    assert "detail" not in response.json()


def test_one_shot_fault_is_isolated_per_order_and_reset_restores_normal_behavior(
    client,
):
    configure(
        client,
        "paypal",
        scenario="normal",
        faults={
            "get": {
                "kind": "error",
                "name": "SERVICE_UNAVAILABLE",
                "issue": "SERVICE_UNAVAILABLE",
                "http_status": 503,
                "times": 1,
            }
        },
    )
    first, second = paypal_order(client), paypal_order(client)
    for order in (first, second):
        assert client.get(f"/v2/checkout/orders/{order}").status_code == 503
        assert client.get(f"/v2/checkout/orders/{order}").status_code == 200
    configure(client, "paypal", first, scenario="unavailable")
    assert client.get(f"/v2/checkout/orders/{first}").status_code == 404
    configure(client, "paypal", first, scenario="normal")
    assert client.get(f"/v2/checkout/orders/{first}").status_code == 200


@pytest.mark.parametrize(
    "scenario,issue",
    [
        ("capture_declined", "INSTRUMENT_DECLINED"),
        ("capture_card_expired", "CARD_EXPIRED"),
    ],
)
def test_capture_business_errors_are_structured_without_creating_a_capture(
    client, scenario, issue
):
    order = paypal_order(client)
    configure(client, "paypal", order, scenario=scenario, status="APPROVED")
    response = capture(client, order)
    assert response.status_code == 422
    assert response.json()["details"][0]["issue"] == issue
    assert "capture" not in paypal.ORDERS_PAYPAL[order]


def test_lost_capture_response_preserves_financial_truth_and_replay_does_not_charge_again(
    client,
):
    order = paypal_order(client)
    configure(
        client, "paypal", order, scenario="capture_lost_response", status="APPROVED"
    )
    assert capture(client, order).status_code == 503
    assert client.scheduled.call_args.kwargs["mode"] == "suppress"
    response = client.get(f"/v2/checkout/orders/{order}").json()
    assert (
        response["purchase_units"][0]["payments"]["captures"][0]["status"]
        == "COMPLETED"
    )
    assert capture(client, order).status_code == 200
    configure(client, "paypal", order, scenario="normal")
    assert (
        client.post(
            f"/mock/paypal/orders/{order}/replay", json={"event": "approval"}
        ).status_code
        == 200
    )
    assert client.scheduled.call_args.kwargs["event_type"] == "CHECKOUT.ORDER.APPROVED"
    assert paypal.ORDERS_PAYPAL[order]["mock"]["financial_captures"] == 1


def test_pending_capture_is_resolved_without_another_capture_post(client):
    order = paypal_order(client)
    configure(client, "paypal", order, scenario="capture_pending", status="APPROVED")
    response = capture(client, order)
    assert (
        response.json()["purchase_units"][0]["payments"]["captures"][0]["status"]
        == "PENDING"
    )
    assert client.scheduled.call_args.kwargs["event_type"] == "PAYMENT.CAPTURE.PENDING"
    response = client.post(
        f"/mock/paypal/orders/{order}/resolve-capture", json={"status": "COMPLETED"}
    )
    assert response.status_code == 200
    stored = paypal.ORDERS_PAYPAL[order]
    assert stored["capture"]["status"] == "COMPLETED"
    assert stored["mock"]["calls"]["capture"] == 1
    assert stored["mock"]["financial_captures"] == 1
    assert (
        client.scheduled.call_args.kwargs["event_type"] == "PAYMENT.CAPTURE.COMPLETED"
    )


def test_processor_and_three_ds_failures_expose_the_documented_response_paths(client):
    order = paypal_order(client)
    configure(client, "paypal", order, scenario="processor_declined", status="APPROVED")
    response = capture(client, order).json()
    assert (
        response["purchase_units"][0]["payments"]["captures"][0]["processor_response"][
            "response_code"
        ]
        == "5120"
    )
    order = paypal_order(client)
    configure(client, "paypal", order, scenario="three_ds_failed")
    client.post(f"/mock/paypal/orders/{order}/approve")
    response = client.get(f"/v2/checkout/orders/{order}").json()
    assert response["status"] == "PAYER_ACTION_REQUIRED"
    assert (
        response["payment_source"]["card"]["authentication_result"]["three_d_secure"][
            "authentication_status"
        ]
        == "N"
    )
    assert capture(client, order).status_code == 422


def test_mismatched_capture_does_not_corrupt_the_persisted_financial_amount(client):
    order = paypal_order(client)
    configure(client, "paypal", order, scenario="wrong_amount", status="APPROVED")
    response = capture(client, order).json()
    assert (
        response["purchase_units"][0]["payments"]["captures"][0]["amount"]["value"]
        == "81.02"
    )
    assert paypal.ORDERS_PAYPAL[order]["capture"]["amount"]["value"] == "80.02"
    assert capture(client, order).json() == response
    configure(client, "paypal", order, scenario="normal")
    assert (
        client.get(f"/v2/checkout/orders/{order}").json()["purchase_units"][0][
            "payments"
        ]["captures"][0]["amount"]["value"]
        == "80.02"
    )


def test_oauth_and_order_creation_faults_can_be_forced_before_an_id_exists(client):
    configure(client, "paypal", scenario="oauth_error")
    response = client.post(
        "/v1/oauth2/token", data={"grant_type": "client_credentials"}
    )
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"
    configure(client, "paypal", scenario="create_error")
    response = client.post(
        "/v2/checkout/orders",
        json={
            "purchase_units": [
                {
                    "reference_id": "R",
                    "amount": {"currency_code": "USD", "value": "10.00"},
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "return_url": "http://shop.test/r",
                        "cancel_url": "http://shop.test/c",
                    }
                }
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["details"][0]["issue"] == "PAYEE_ACCOUNT_RESTRICTED"
    assert len(paypal.ORDERS_PAYPAL) == 0


@pytest.mark.parametrize(
    "scenario,code",
    [
        ("reject_authentication", "-5"),
        ("reject_blocked_card", "-7"),
        ("reject_expired_card", "-8"),
        ("reject_security", "-1017"),
        ("reject_back", "-1018"),
    ],
)
def test_virtualpos_financial_rejections_expose_documented_object_and_notify_uuid(
    client, scenario, code
):
    payment = virtualpos_payment(client)
    configure(client, "virtualpos", payment, scenario=scenario)
    response = client.post(f"/virtualpos/checkout/{payment}/success")
    assert response.status_code == 200
    remote = client.get(f"/v3/payment/{payment}").json()["payment"]["order"]
    assert remote["status"] == "rechazado"
    assert remote["rejected_object"]["error_code"] == code
    assert remote["rejected_object"]["doc_url"].endswith("/objeto-rechazo")
    assert client.outgoing[-1]["data"] == {"uuid": payment}
    assert not remote["auth_code"]


@pytest.mark.parametrize(
    "scenario,state",
    [
        ("pending", "pendiente"),
        ("retrying", "reintentando"),
        ("expired", "expirado"),
        ("cancelled", "cancelado"),
    ],
)
def test_virtualpos_intermediate_and_terminal_outcomes_are_not_payments(
    client, scenario, state
):
    payment = virtualpos_payment(client)
    configure(client, "virtualpos", payment, scenario=scenario)
    client.post(f"/virtualpos/checkout/{payment}/success")
    record = virtualpos.PAYMENTS[payment]
    assert record["status"] == state
    assert not record["auth_code"]
    assert record["mock"].get("financial_payments", 0) == 0
    if state in {"expirado", "cancelado"}:
        configure(client, "virtualpos", payment, scenario="normal")
        assert (
            client.post(f"/virtualpos/checkout/{payment}/success").json()["error"][
                "error_code"
            ]
            == "E-036"
        )


def test_virtualpos_can_retry_a_rejected_uuid_and_paid_cannot_be_charged_again(client):
    payment = virtualpos_payment(client)
    client.post(f"/virtualpos/checkout/{payment}/failure")
    client.post(f"/virtualpos/checkout/{payment}/success")
    record = virtualpos.PAYMENTS[payment]
    assert len(record["attempts"]) == 2
    assert record["status"] == "pagado"
    assert "rejected_object" not in record
    outgoing = len(client.outgoing)
    client.post(f"/virtualpos/checkout/{payment}/success")
    assert len(client.outgoing) == outgoing
    assert virtualpos.PAYMENTS[payment]["mock"]["financial_payments"] == 1


def test_virtualpos_missing_and_forced_api_errors_have_the_api_error_envelope(client):
    assert client.get("/v3/payment/missing").json()["error"]["error_code"] == "E-020"
    configure(client, "virtualpos", scenario="create_error")
    response = client.post("/v3/payment", json={})
    assert response.status_code == 400
    assert response.json()["error"]["error_code"] == "E-003"
    configure(client, "virtualpos", scenario="normal")
    payment = virtualpos_payment(client)
    configure(client, "virtualpos", payment, scenario="verify_error")
    assert client.get(f"/v3/payment/{payment}").json()["error"]["error_code"] == "E-021"
    configure(client, "virtualpos", payment, scenario="checkout_error")
    response = client.post(f"/v3/payment/{payment}/webcheckout", json={})
    assert response.json()["error"]["error_code"] == "E-027"


def test_virtualpos_callback_faults_and_replay_do_not_repeat_the_financial_attempt(
    client,
):
    payment = virtualpos_payment(client)
    configure(client, "virtualpos", payment, scenario="no_webhook")
    client.post(f"/virtualpos/checkout/{payment}/success")
    assert client.outgoing == []
    assert virtualpos.WEBHOOK_DELIVERIES[-1]["skipped"] is True
    configure(client, "virtualpos", payment, scenario="duplicate_webhook")
    assert client.post(f"/mock/virtualpos/payments/{payment}/replay").status_code == 200
    assert len(client.outgoing) == 2
    configure(client, "virtualpos", payment, scenario="wrong_callback_uuid")
    client.post(f"/mock/virtualpos/payments/{payment}/replay")
    assert client.outgoing[-1]["data"]["uuid"] != payment
    assert len(virtualpos.PAYMENTS[payment]["attempts"]) == 1


def test_virtualpos_response_mismatch_does_not_change_real_uuid_or_amount(client):
    payment = virtualpos_payment(client)
    configure(client, "virtualpos", payment, scenario="wrong_amount")
    assert (
        client.get(f"/v3/payment/{payment}").json()["payment"]["order"]["amount"]
        == 18524
    )
    configure(client, "virtualpos", payment, scenario="wrong_uuid")
    assert (
        client.get(f"/v3/payment/{payment}").json()["payment"]["order"]["uuid"]
        != payment
    )
    assert virtualpos.PAYMENTS[payment]["amount"] == 18523
    assert virtualpos.PAYMENTS[payment]["uuid"] == payment


def test_scenario_and_financial_results_survive_a_new_storage_instance(
    client, monkeypatch
):
    configure(client, "paypal", scenario="approved_no_webhook")
    order = paypal_order(client)
    configure(client, "paypal", order, status="APPROVED")
    capture(client, order)
    payment = virtualpos_payment(client)
    client.post(f"/virtualpos/checkout/{payment}/failure")
    for module, name, provider, collection in [
        (paypal, "ORDERS_PAYPAL", "paypal", "orders"),
        (virtualpos, "PAYMENTS", "virtualpos", "payments"),
    ]:
        store = ResourceStore(provider, collection)
        store.path = getattr(module, name).path
        monkeypatch.setattr(module, name, store)
    assert (
        client.get(f"/v2/checkout/orders/{order}").json()["purchase_units"][0][
            "payments"
        ]["captures"][0]["status"]
        == "COMPLETED"
    )
    assert (
        client.get(f"/v3/payment/{payment}").json()["payment"]["order"][
            "rejected_object"
        ]["error_code"]
        == "-6"
    )
    store = ResourceStore("paypal", "controls")
    store.path = DEFAULTS["paypal"].path
    assert store["defaults"]["scenario"] == "approved_no_webhook"


@pytest.mark.asyncio
async def test_signed_duplicate_and_invalid_webhooks_use_real_signatures_and_same_event_id(
    client,
):
    await paypal._deliver_webhook(
        event_type="CHECKOUT.ORDER.APPROVED",
        resource_type="checkout-order",
        summary="Test",
        resource={"id": "order-test"},
        mode="duplicate",
    )
    assert len(client.outgoing) == 2
    assert (
        json.loads(client.outgoing[0]["content"])["id"]
        == json.loads(client.outgoing[1]["content"])["id"]
    )
    assert base64.b64decode(client.outgoing[0]["headers"]["Paypal-Transmission-Sig"])
    await paypal._deliver_webhook(
        event_type="CHECKOUT.ORDER.APPROVED",
        resource_type="checkout-order",
        summary="Test",
        resource={"id": "order-test"},
        mode="bad_signature",
    )
    assert client.outgoing[-1]["headers"]["Paypal-Transmission-Sig"] == "AAAA"
    await paypal._deliver_webhook(
        event_type="CHECKOUT.ORDER.APPROVED",
        resource_type="checkout-order",
        summary="Test",
        resource={},
        mode="suppress",
    )
    assert len(client.outgoing) == 3
    assert paypal.WEBHOOK_DELIVERIES[-1]["skipped"] is True


@pytest.mark.asyncio
async def test_injected_delay_is_bounded_and_awaited_without_sleeping_the_test(
    client, monkeypatch
):
    from mock_controls import fault_response

    sleep = Mock(return_value=None)

    async def delay(seconds):
        sleep(seconds)

    monkeypatch.setattr(asyncio, "sleep", delay)
    assert (
        await fault_response("paypal", {"kind": "delay", "delay_seconds": 12}) is None
    )
    sleep.assert_called_once_with(12)
    response = client.post("/mock/paypal/controls", json={"webhook_delay_seconds": 21})
    assert response.status_code == 422


@pytest.mark.parametrize("provider", ["paypal", "virtualpos"])
def test_invalid_controls_do_not_replace_previous_defaults(client, provider):
    configure(client, provider, scenario="normal")
    for body in (
        {"scenario": "undefined"},
        {"faults": {"typo": {}}},
        {"webhook_delay_seconds": 21},
        {"faults": {"get": {"times": -2}}},
    ):
        assert client.post(f"/mock/{provider}/controls", json=body).status_code == 422
    assert (
        client.get(f"/mock/{provider}/controls").json()["defaults"]["scenario"]
        == "normal"
    )


def test_paid_resources_cannot_be_reopened_by_controls(client):
    payment = virtualpos_payment(client)
    client.post(f"/virtualpos/checkout/{payment}/success")
    assert (
        client.post(
            "/mock/virtualpos/controls",
            json={"resource_id": payment, "status": "pendiente"},
        ).status_code
        == 409
    )
    order = paypal_order(client)
    configure(client, "paypal", order, status="APPROVED")
    capture(client, order)
    assert (
        client.post(
            "/mock/paypal/controls", json={"resource_id": order, "status": "APPROVED"}
        ).status_code
        == 409
    )


def test_invalid_json_responses_are_distinct_from_business_errors(client):
    for provider, resource, path in [
        ("paypal", paypal_order(client), "/v2/checkout/orders/"),
        ("virtualpos", virtualpos_payment(client), "/v3/payment/"),
    ]:
        configure(client, provider, resource, scenario="invalid_json")
        response = client.get(path + resource)
        assert response.status_code == 200
        with pytest.raises(ValueError):
            response.json()


@pytest.mark.parametrize("provider", ["paypal", "virtualpos"])
def test_every_catalog_scenario_can_be_configured_without_mutating_payment_results(
    client, provider
):
    resource = (
        paypal_order(client) if provider == "paypal" else virtualpos_payment(client)
    )
    for scenario in SCENARIOS[provider]:
        configure(client, provider, resource, scenario=scenario)
    store = paypal.ORDERS_PAYPAL if provider == "paypal" else virtualpos.PAYMENTS
    assert store[resource]["status"] in {"PAYER_ACTION_REQUIRED", "pendiente"}


def test_resource_storage_is_readable_from_a_fresh_process(client):
    import subprocess
    import sys

    order = paypal_order(client)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; from pathlib import Path; from mock_storage import ResourceStore; "
            "store=ResourceStore('paypal','orders'); store.path=Path(sys.argv[1]); "
            "print(json.dumps(store[sys.argv[2]]))",
            str(paypal.ORDERS_PAYPAL.path),
            order,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(result.stdout)["id"] == order


@pytest.mark.parametrize(
    "amount,code",
    [(999, "E-011"), (1000.25, "E-011"), ("invalid", "E-010"), (True, "E-010")],
)
def test_virtualpos_validates_whole_clp_and_returns_documented_errors(
    client, amount, code
):
    response = client.post(
        "/v3/payment",
        json={
            "amount": amount,
            "email": "client@example.com",
            "social_id": "12345678-5",
            "first_name": "Test",
            "last_name": "User",
            "phone": "56912345678",
            "description": "Test",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["error_code"] == code
    assert not virtualpos.PAYMENTS


def test_wrong_provider_controls_are_rejected_without_modifying_defaults(client):
    for provider, data in [
        ("paypal", {"webhook_mode": "wrong_uuid"}),
        ("virtualpos", {"webhook_mode": "bad_signature"}),
        ("virtualpos", {"capture_result": "completed"}),
    ]:
        response = client.post(f"/mock/{provider}/controls", json=data)
        assert response.status_code == 422
        assert (
            client.get(f"/mock/{provider}/controls").json()["defaults"]["scenario"]
            == "normal"
        )
