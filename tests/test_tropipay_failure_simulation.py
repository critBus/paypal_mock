import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import app
from tropipay import tropipay as mock_tropipay


@pytest.fixture
def paymentcard():
    mock_tropipay.TEST_STATE.reset()
    card = {
        "id": "mock-card-1",
        "amount": 12500,
        "currency": "USD",
        "userId": "mock-user",
        "concept": "MERC-TEST-1234",
        "reference": "S00000115",
        "state": 1,
        "urlNotification": "http://crinnopayments.test/tropipay/webhook/",
        "client": {
            "name": "Ana",
            "lastName": "Test",
            "email": "ana@example.com",
            "phone": "+15550000000",
            "address": "Test address",
            "city": "Miami",
            "countryId": 840,
            "state": "FL",
        },
    }
    mock_tropipay.TEST_STATE.paymentcards[card["id"]] = card
    mock_tropipay.TEST_STATE.paymentcard_meta[card["id"]] = {
        "client_id": "client_001",
        "webhook_secret": "secret_001",
        "charges": [],
    }
    yield card
    mock_tropipay.TEST_STATE.reset()


def test_failed_payment_form_accumulates_realistic_charges(monkeypatch, paymentcard):
    delivered_payloads = []

    async def capture_webhook(card, target_url, status="OK", state=3):
        payload = mock_tropipay.build_payment_webhook(card, status=status, state=state)
        delivered_payloads.append((target_url, payload))
        return {"success": True, "payload": payload}

    monkeypatch.setattr(mock_tropipay, "send_tropipay_webhook", capture_webhook)
    client = TestClient(app)

    page = client.get(f"/pay/{paymentcard['id']}")
    assert page.status_code == 200
    assert 'name="card_brand"' in page.text
    assert "60022:Unauthenticated" in page.text
    assert "Continuar sin enviar webhook" in page.text

    first_failure = client.post(
        f"/pay/{paymentcard['id']}/fail",
        data={
            "card_brand": "VISA",
            "bank": "BANK OF AMERICA NATIONAL ASSO",
            "card_pan": "4242",
            "error_reason": "60022:Unauthenticated",
        },
        follow_redirects=False,
    )

    assert first_failure.status_code == 303
    first_payload = delivered_payloads[-1][1]
    assert first_payload["status"] == "KO"
    assert first_payload["data"]["state"] == 4
    assert first_payload["data"]["errorReason"] == "60022:Unauthenticated"
    first_charge = first_payload["data"]["charges"][0]
    assert first_charge["state"] == 4
    assert first_charge["cardBrand"] == "VISA"
    assert first_charge["bank"] == "BANK OF AMERICA NATIONAL ASSO"
    assert first_charge["cardPan"] == "4242"
    assert first_charge["errorReason"] == "60022:Unauthenticated"

    second_failure = client.post(
        f"/pay/{paymentcard['id']}/fail",
        data={
            "card_brand": "MASTERCARD",
            "bank": "CITIBANK N.A.",
            "card_pan": "6699",
            "error_reason": "89:Security Violation",
        },
        follow_redirects=False,
    )

    assert second_failure.status_code == 303
    second_payload = delivered_payloads[-1][1]
    assert len(second_payload["data"]["charges"]) == 2
    assert second_payload["data"]["errorReason"] == "89:Security Violation"
    assert second_payload["data"]["charges"][-1]["cardBrand"] == "MASTERCARD"
    assert second_payload["data"]["charges"][-1]["cardPan"] == "6699"

    successful_payment = client.post(f"/pay/{paymentcard['id']}/pay", follow_redirects=False)
    assert successful_payment.status_code == 303
    success_payload = delivered_payloads[-1][1]
    assert success_payload["status"] == "OK"
    assert success_payload["data"]["state"] == 3
    assert len(success_payload["data"]["charges"]) == 2


def test_no_webhook_action_does_not_queue_a_delivery(monkeypatch, paymentcard):
    delivered_payloads = []

    async def capture_webhook(*args, **kwargs):
        delivered_payloads.append((args, kwargs))

    monkeypatch.setattr(mock_tropipay, "send_tropipay_webhook", capture_webhook)
    client = TestClient(app)

    response = client.post(f"/pay/{paymentcard['id']}/no-webhook", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith("?result=no_webhook")
    assert delivered_payloads == []
    assert mock_tropipay.TEST_STATE.paymentcard_meta[paymentcard["id"]]["charges"] == []
