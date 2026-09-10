from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import app
from bms import main as bms_routes  # noqa: F401
from bms import payment_links as bms
from bms.config import BMS_CONFIG
from bms.payment_link_storage import PaymentLinkStorage


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("BMS_MOCK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("BMS_MOCK_WEBHOOK_TOKEN", raising=False)
    monkeypatch.delenv("BMS_MOCK_WEBHOOK_VERSION", raising=False)
    monkeypatch.setattr(bms, "storage", PaymentLinkStorage(tmp_path / "bms.sqlite3"))
    monkeypatch.setenv("BMS_MOCK_PUBLIC_URL", "https://mock.example")
    return TestClient(app)


@pytest.fixture
def credentials():
    return {
        "mid": BMS_CONFIG.mid,
        "cid": BMS_CONFIG.cid,
        "AppKey": BMS_CONFIG.app_key,
        "AppType": "1",
        "UserName": BMS_CONFIG.username,
        "Password": BMS_CONFIG.password,
    }


def create(client, credentials, **overrides):
    response = client.post(
        "/api/PaymentLinks/AddPaymentLink",
        json={
            **credentials,
            "PaymentLink": {
                "Amount": "10.50",
                "InvoiceNumber": "ORDER-1",
                "Description": "Test checkout",
                **overrides,
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["ResponseCode"] == 200
    return response.json()["PaymentLink"]


def get(client, credentials, identifier):
    return client.get(
        "/api/PaymentLinks/GetPaymentLink",
        params={
            "id": identifier,
            **{
                f"request.{key[0].lower()}{key[1:]}": value
                for key, value in credentials.items()
            },
        },
    ).json()


def disable(client, credentials, identifier):
    return client.put(
        "/api/PaymentLinks/DisablePaymentLink",
        json={**credentials, "PaymentLink": {"Id": identifier}},
    ).json()


def test_create_and_read_contract_and_persistence(client, credentials, monkeypatch):
    link = create(client, credentials)
    assert link["Status"] == 0 and link["Active"] is True
    assert link["Amount"] == "10.50"
    assert link["PaidOn"] is None and link["DisabledOn"] is None
    assert link["Link"] == f"https://mock.example/payments/link/{link['Id']}"
    monkeypatch.setattr(bms, "storage", PaymentLinkStorage(bms.storage.path))
    assert get(client, credentials, link["Id"])["PaymentLink"] == link
    assert get(client, credentials, "ORDER-1")["PaymentLink"] == link
    assert client.get("/api/PaymentLinks", params=credentials).json()[
        "PaymentLinks"
    ] == [link]


@pytest.mark.parametrize("path", ["GetPaymentLink", "GetPaymentLinkById", "{id}"])
def test_get_aliases_and_legacy_get_body(client, credentials, path):
    link = create(client, credentials)
    response = client.request(
        "GET",
        f"/api/PaymentLinks/{path.format(id=link['Id'])}",
        params={"id": link["Id"]},
        json=credentials,
    )
    assert response.json()["PaymentLink"] == link


def test_integer_credentials_and_amount(client, credentials):
    for key in ("mid", "cid", "AppType", "AppKey"):
        credentials[key] = int(credentials[key])
    assert create(client, credentials, Amount=12)["Amount"] == "12.00"


@pytest.mark.parametrize(
    "amount", [None, "0", "-1", "NaN", "Infinity", "invalid", "0.001", "1e100000"]
)
def test_invalid_amounts_do_not_create_links(client, credentials, amount):
    response = client.post(
        "/api/PaymentLinks/AddPaymentLink",
        json={**credentials, "PaymentLink": {"Amount": amount}},
    )
    assert response.json()["ResponseCode"] == 506
    assert bms.storage.list(BMS_CONFIG.mid) == []


def test_invalid_credentials_and_unknown_link_use_flat_bms_envelope(
    client, credentials
):
    link = create(client, credentials)
    wrong = {**credentials, "Password": "wrong"}
    assert get(client, wrong, link["Id"])["ResponseCode"] == 1
    assert disable(client, wrong, link["Id"])["ResponseCode"] == 1
    response = get(client, credentials, str(uuid4()))
    assert response["ResponseCode"] == 404 and response["PaymentLink"] is None
    assert "detail" not in response
    assert bms.storage.get(link["Id"])["Active"] is True


def test_checkout_payment_is_idempotent_and_protected_from_disable(client, credentials):
    link = create(client, credentials)
    page = client.get(
        f"/payments/link/{link['Id']}",
        params={"returnUrl": "https://shop.example/return"},
    )
    assert page.status_code == 200
    assert "Simular pago" in page.text and "10.50" in page.text
    paid = client.post(
        f"/mock-bms/payment-links/{link['Id']}/simulate", json={"outcome": "success"}
    ).json()["PaymentLink"]
    assert paid["Status"] == 1 and paid["PaidOn"] is not None
    assert paid["ServiceReferenceNumber"]
    assert disable(client, credentials, link["Id"])["PaymentLink"] == paid
    assert (
        client.post(
            f"/mock-bms/payment-links/{link['Id']}/simulate",
            json={"outcome": "success"},
        ).json()["PaymentLink"]
        == paid
    )
    assert get(client, credentials, link["Id"])["PaymentLink"] == paid


def test_disabled_link_cannot_be_paid_even_from_previously_opened_checkout(
    client, credentials
):
    link = create(client, credentials)
    assert client.get(f"/payments/link/{link['Id']}").status_code == 200
    disabled = disable(client, credentials, link["Id"])["PaymentLink"]
    assert not disabled["Active"] and disabled["DisabledOn"]
    assert disabled["Status"] == 0
    assert disable(client, credentials, link["Id"])["PaymentLink"] == disabled
    result = client.post(
        f"/mock-bms/payment-links/{link['Id']}/simulate", json={"outcome": "success"}
    ).json()
    assert result["ResponseCode"] == 506
    assert result["PaymentLink"] == disabled
    assert (
        "Este enlace está desactivado"
        in client.get(f"/payments/link/{link['Id']}").text
    )


@pytest.mark.parametrize("outcome", ["decline", "cancel"])
def test_failed_attempt_or_leaving_checkout_keeps_link_payable(
    client, credentials, outcome
):
    link = create(client, credentials)
    response = client.post(
        f"/mock-bms/payment-links/{link['Id']}/simulate", json={"outcome": outcome}
    ).json()
    assert response["PaymentLink"] == link


def test_failure_control_exercises_retries_without_changing_state(client, credentials):
    link = create(client, credentials)
    client.post(
        f"/mock-bms/payment-links/{link['Id']}/failure",
        json={"operation": "disable", "remaining": 2},
    )
    for _ in range(2):
        assert disable(client, credentials, link["Id"])["ResponseCode"] == 300
        assert get(client, credentials, link["Id"])["PaymentLink"]["Active"]
    assert not disable(client, credentials, link["Id"])["PaymentLink"]["Active"]
    client.post(
        f"/mock-bms/payment-links/{link['Id']}/failure",
        json={"operation": "retrieve", "response_code": 404},
    )
    assert get(client, credentials, link["Id"])["ResponseCode"] == 404
    assert get(client, credentials, link["Id"])["ResponseCode"] == 200
    assert "_failures" not in get(client, credentials, link["Id"])["PaymentLink"]


def test_no_native_expiration_without_backend_disable(client, credentials):
    link = create(client, credentials)

    def old(current):
        current["CreatedOn"] = "2000-01-01T00:00:00+00:00"

    bms.storage.update(link["Id"], old)
    assert get(client, credentials, link["Id"])["PaymentLink"]["Active"]


def test_pay_and_disable_race_has_one_terminal_result(client, credentials):
    link = create(client, credentials)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pay = pool.submit(
            client.post,
            f"/mock-bms/payment-links/{link['Id']}/simulate",
            json={"outcome": "success"},
        )
        cancel = pool.submit(disable, client, credentials, link["Id"])
        pay.result()
        cancel.result()
    saved = get(client, credentials, link["Id"])["PaymentLink"]
    assert (saved["Status"] == 1 and saved["DisabledOn"] is None) or (
        saved["Status"] == 0 and not saved["Active"] and saved["PaidOn"] is None
    )


def test_legacy_sale_and_threeds_still_work(client, credentials):
    assert (
        client.post("/api/Auth/TokenThreeDS", json=credentials).json()["ResponseCode"]
        == 200
    )
    response = client.post(
        "/api/Transactions/Sale",
        json={
            **credentials,
            "Amount": "10.50",
            "CardNumber": "4111111111111111",
            "ExpDate": "1299",
            "CVN": "123",
            "NameOnCard": "TEST",
            "ZipCode": "12345",
            "OrderReference": "legacy",
            "UserTransactionNumber": str(uuid4()),
        },
    )
    assert response.json()["ResponseCode"] == 200
    reference = response.json()["ServiceReferenceNumber"]
    assert (
        client.get(
            "/api/Transactions/GetTransaction", params={"service_reference": reference}
        ).json()["ServiceReferenceNumber"]
        == reference
    )


@pytest.fixture
def webhook_transport(monkeypatch):
    import httpx

    from bms import payment_link_webhooks

    calls = []
    responses = [200]

    def receive(request):
        calls.append(request)
        code = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(code, Exception):
            raise code
        return httpx.Response(code)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        payment_link_webhooks.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(receive), **kw),
    )
    monkeypatch.setenv(
        "BMS_MOCK_WEBHOOK_URL", "http://backend.test/api/bms/webhooks/notifications/"
    )
    monkeypatch.setenv("BMS_MOCK_WEBHOOK_TOKEN", "test-webhook-secret")
    monkeypatch.setenv("BMS_MOCK_WEBHOOK_ATTEMPTS", "2")

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(payment_link_webhooks.asyncio, "sleep", no_sleep)
    return calls, responses


def events(client, link):
    return client.get(f"/mock-bms/payment-links/{link['Id']}/webhooks").json()["events"]


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("outcome", ["success", "decline"])
def test_documented_webhook_payload_and_signature(
    client, credentials, webhook_transport, monkeypatch, version, outcome
):
    import json

    monkeypatch.setenv("BMS_MOCK_WEBHOOK_VERSION", str(version))
    link = create(client, {**credentials, "IsTest": True})
    response = client.post(
        f"/mock-bms/payment-links/{link['Id']}/simulate", json={"outcome": outcome}
    )
    assert response.status_code == 200
    calls, _ = webhook_transport
    assert len(calls) == 1
    assert calls[0].headers["x-signature"] == "test-webhook-secret"
    payload = json.loads(calls[0].content)
    assert payload["Request"]["PaymentLinkId"] == link["Id"]
    assert payload["Request"]["InvoiceNumber"] == link["InvoiceNumber"]
    assert payload["Request"]["Amount"] == 10.5
    assert payload["Response"]["ResponseCode"] == (200 if outcome == "success" else 3)
    if version == 1:
        assert set(payload) == {"Request", "Response"}
    else:
        assert payload["SchemaVersion"] == 2
        assert payload["EventType"] == "Sale"
        assert payload["MerchantId"] == str(BMS_CONFIG.mid)
        assert payload["IsTest"] is True
        assert payload["Outcome"] == ("Succeeded" if outcome == "success" else "Failed")
    saved = events(client, link)
    assert len(saved) == 1 and saved[0]["status"] == "delivered"
    assert saved[0]["payload"] == payload
    assert "test-webhook-secret" not in str(saved)
    assert not any(
        key.startswith("_")
        for key in get(client, credentials, link["Id"])["PaymentLink"]
    )


def test_delivery_retry_reuses_event_and_paid_state_survives_failure(
    client, credentials, webhook_transport
):
    calls, responses = webhook_transport
    responses[:] = [503, 500]
    link = create(client, credentials)
    client.post(
        f"/mock-bms/payment-links/{link['Id']}/simulate", json={"outcome": "success"}
    )
    assert len(calls) == 2 and calls[0].content == calls[1].content
    saved = events(client, link)[0]
    assert saved["status"] == "failed" and saved["attempts"] == 2
    assert get(client, credentials, link["Id"])["PaymentLink"]["Status"] == 1
    responses[:] = [204]
    path = f"/mock-bms/payment-links/{link['Id']}/webhooks/{saved['id']}/send"
    assert client.post(path).status_code == 202
    assert events(client, link)[0]["status"] == "delivered"
    # Explicit replay intentionally sends the same receipt again to test backend deduplication.
    assert client.post(path).status_code == 202
    assert len(calls) == 4
    assert all(call.content == calls[0].content for call in calls)


def test_repeated_payment_and_cancellation_do_not_generate_extra_events(
    client, credentials, webhook_transport
):
    link = create(client, credentials)
    client.post(
        f"/mock-bms/payment-links/{link['Id']}/simulate", json={"outcome": "cancel"}
    )
    assert events(client, link) == []
    for _ in range(2):
        client.post(
            f"/mock-bms/payment-links/{link['Id']}/simulate",
            json={"outcome": "success"},
        )
    assert len(webhook_transport[0]) == 1
    disabled = create(client, credentials, InvoiceNumber="ORDER-2")
    disable(client, credentials, disabled["Id"])
    client.post(
        f"/mock-bms/payment-links/{disabled['Id']}/simulate",
        json={"outcome": "success"},
    )
    assert events(client, disabled) == []


def test_unconfigured_event_persists_and_can_be_sent_later(
    client, credentials, monkeypatch
):
    link = create(client, credentials)
    client.post(
        f"/mock-bms/payment-links/{link['Id']}/simulate", json={"outcome": "decline"}
    )
    monkeypatch.setattr(bms, "storage", PaymentLinkStorage(bms.storage.path))
    saved = events(client, link)[0]
    assert saved["status"] == "unconfigured" and saved["attempts"] == 0
    assert saved["payload"]["Response"]["ResponseCode"] == 3


def test_network_failure_is_recorded_without_sensitive_exception(
    client, credentials, webhook_transport
):
    import httpx

    calls, responses = webhook_transport
    responses[:] = [httpx.ConnectError("sensitive-url"), 200]
    link = create(client, credentials)
    client.post(
        f"/mock-bms/payment-links/{link['Id']}/simulate", json={"outcome": "success"}
    )
    assert len(calls) == 2
    saved = events(client, link)[0]
    assert saved["status"] == "delivered" and saved["attempts"] == 2
    assert "sensitive-url" not in str(saved)


def test_v2_uses_creation_environment(
    client, credentials, webhook_transport, monkeypatch
):
    monkeypatch.setenv("BMS_MOCK_WEBHOOK_VERSION", "2")
    link = create(client, {**credentials, "IsTest": False})
    client.post(
        f"/mock-bms/payment-links/{link['Id']}/simulate", json={"outcome": "success"}
    )
    assert events(client, link)[0]["payload"]["IsTest"] is False
