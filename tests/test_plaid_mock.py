import base64
import hashlib
import json
from datetime import date, timedelta
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

from app import app
from plaid import plaid as mock
from plaid.signing import public_key
from plaid.storage import PlaidStorage


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(mock, "storage", PlaidStorage(tmp_path / "plaid.sqlite3"))
    monkeypatch.setattr(mock, "STRICT_AUTH", False)
    monkeypatch.setattr(mock, "PUBLIC_BASE_URL", "")
    return TestClient(
        app, headers={"PLAID-CLIENT-ID": "test-client", "PLAID-SECRET": "test-secret"}
    )


def create_link(client, **extra):
    response = client.post(
        "/link/token/create",
        json={
            "client_name": "Test",
            "products": ["transactions"],
            "country_codes": ["US"],
            "language": "es",
            "user": {"client_user_id": "customer-1"},
            "hosted_link": {},
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def authorize(client, link):
    path = urlsplit(link["hosted_link_url"]).path
    response = client.post(
        f"{path}/simulate", data={"outcome": "success"}, follow_redirects=False
    )
    assert response.status_code in {200, 303}, response.text
    return client.post(
        "/link/token/get", json={"link_token": link["link_token"]}
    ).json()["link_sessions"][0]


def linked_account(client, **extra):
    link = create_link(client, **extra)
    session = authorize(client, link)
    public = session["results"]["item_add_results"][0]["public_token"]
    response = client.post("/item/public_token/exchange", json={"public_token": public})
    assert response.status_code == 200
    return link, public, response.json()


def test_bank_flow_and_persistence(client, monkeypatch):
    link, public, account = linked_account(client)
    monkeypatch.setattr(mock, "storage", PlaidStorage(mock.storage.path))
    data = {"access_token": account["access_token"]}
    item = client.post("/item/get", json=data).json()["item"]
    assert item["item_id"] == account["item_id"]
    assert item["consented_products"] == ["transactions"]
    assert client.get(urlsplit(link["hosted_link_url"]).path).status_code == 200
    response = client.post("/accounts/balance/get", json=data).json()
    assert response["accounts"][0]["balances"]["available"] == 3500.0
    assert (
        client.post("/accounts/get", json=data).json()["item"]["item_id"]
        == account["item_id"]
    )
    assert (
        client.post(
            "/item/public_token/exchange", json={"public_token": public}
        ).json()["error_code"]
        == "INVALID_PUBLIC_TOKEN"
    )


def test_transactions_pagination_dates_and_consent(client):
    _, _, account = linked_account(client)
    today = date.today()
    data = {
        "access_token": account["access_token"],
        "start_date": (today - timedelta(days=10)).isoformat(),
        "end_date": today.isoformat(),
        "options": {"count": 1, "offset": 1},
    }
    response = client.post("/transactions/get", json=data)
    assert response.status_code == 200
    assert response.json()["total_transactions"] == 3
    assert len(response.json()["transactions"]) == 1
    assert response.json()["transactions"][0]["name"] == "Grocery Store"
    data["start_date"] = data["end_date"]
    assert client.post("/transactions/get", json=data).json()["total_transactions"] == 1
    _, _, auth_account = linked_account(client, products=["auth"])
    data["access_token"] = auth_account["access_token"]
    assert (
        client.post("/transactions/get", json=data).json()["error_code"]
        == "PRODUCTS_NOT_SUPPORTED"
    )


def test_item_login_required_update_mode_and_revoke(client):
    _, _, account = linked_account(client)
    data = {"access_token": account["access_token"]}
    assert (
        client.post(
            f"/mock-plaid/items/{account['item_id']}/simulate",
            json={"outcome": "login_required"},
        ).status_code
        == 200
    )
    assert (
        client.post("/accounts/get", json=data).json()["error_code"]
        == "ITEM_LOGIN_REQUIRED"
    )
    assert (
        client.post("/item/get", json=data).json()["item"]["error"]["error_code"]
        == "ITEM_LOGIN_REQUIRED"
    )
    link = create_link(client, products=[], **data)
    session = authorize(client, link)
    assert session["results"]["item_add_results"] == []
    assert session["events"][0]["event_name"] == "HANDOFF"
    assert client.post("/accounts/get", json=data).status_code == 200
    assert client.post("/item/remove", json=data).status_code == 200
    assert (
        client.post("/accounts/get", json=data).json()["error_code"] == "ITEM_NOT_FOUND"
    )
    assert (
        client.post(
            "/link/token/create",
            json={"country_codes": ["US"], "hosted_link": {}, **data},
        ).json()["error_code"]
        == "ITEM_NOT_FOUND"
    )


def test_authorization_exit_has_no_public_token(client):
    link = create_link(client)
    path = urlsplit(link["hosted_link_url"]).path
    result = client.post(f"{path}/simulate", data={"outcome": "exit"}).json()
    assert result["status"] == "EXITED"
    session = client.post(
        "/link/token/get", json={"link_token": link["link_token"]}
    ).json()["link_sessions"][0]
    assert not session["results"]["item_add_results"]


def test_repeated_authorization_creates_only_one_item(client):
    link = create_link(client)
    first = authorize(client, link)
    second = authorize(client, link)
    assert first["results"] == second["results"]
    with mock.storage.transaction() as db:
        assert (
            db.db.execute(
                "SELECT count(*) FROM plaid_records WHERE kind='item'"
            ).fetchone()[0]
            == 1
        )


def test_expired_link(client):
    link = create_link(client)
    with mock.storage.transaction() as db:
        session = db.get("link_token", link["link_token"])
        session["expires"] = 0
        owner = db.db.execute(
            "SELECT owner FROM plaid_records WHERE kind='link_token'"
        ).fetchone()[0]
        db.put("link_token", link["link_token"], owner, session)
    path = urlsplit(link["hosted_link_url"]).path
    assert client.get(path).status_code == 410
    assert (
        client.post(f"{path}/simulate", data={"outcome": "success"}).status_code == 410
    )


def test_tenant_isolation(client):
    link, public, account = linked_account(client)
    headers = {"PLAID-CLIENT-ID": "another-client", "PLAID-SECRET": "another-secret"}
    for path, data in [
        ("/link/token/get", {"link_token": link["link_token"]}),
        ("/item/public_token/exchange", {"public_token": public}),
        ("/accounts/get", {"access_token": account["access_token"]}),
    ]:
        response = client.post(path, json=data, headers=headers)
        assert response.status_code == 400
        assert response.json()["error_code"] == "INVALID_TOKEN"


def test_credentials_and_strict_auth(client, monkeypatch):
    with TestClient(app) as anonymous:
        response = anonymous.post("/institutions/get", json={"country_codes": ["US"]})
    assert response.json()["error_code"] == "INVALID_API_KEYS"
    monkeypatch.setattr(mock, "STRICT_AUTH", True)
    assert (
        client.post("/institutions/get", json={"country_codes": ["US"]}).json()[
            "error_code"
        ]
        == "INVALID_API_KEYS"
    )
    response = client.post(
        "/institutions/get",
        json={"country_codes": ["US"]},
        headers={"PLAID-CLIENT-ID": mock.CLIENT_ID, "PLAID-SECRET": mock.SECRET},
    )
    assert response.status_code == 200
    assert response.json()["institutions"][0]["oauth"] is False


def test_statements(client):
    _, _, account = linked_account(
        client,
        products=["transactions", "statements"],
        statements={"start_date": "2026-01-01", "end_date": "2026-02-01"},
    )
    response = client.post(
        "/statements/list", json={"access_token": account["access_token"]}
    )
    assert response.status_code == 200
    assert response.json()["accounts"][0]["statements"][0]["month"] == 2
    response = client.post(
        "/link/token/create",
        json={"products": ["statements"], "country_codes": ["US"], "hosted_link": {}},
    )
    assert response.status_code == 400


def decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_webhook_es256_signature_retries_and_persistent_key(client, monkeypatch):
    sent = []

    async def post(url, **kwargs):
        sent.append((url, kwargs))
        return type("Response", (), {"status_code": 503 if len(sent) == 1 else 200})()

    http = AsyncMock()
    http.__aenter__.return_value = http
    http.post.side_effect = post
    monkeypatch.setattr(mock.httpx, "AsyncClient", lambda **kwargs: http)
    link = create_link(client, webhook="http://receiver.test/plaid/")
    path = urlsplit(link["hosted_link_url"]).path
    result = client.post(f"{path}/simulate", data={"outcome": "success"}).json()
    key = result["webhook_id"]
    assert (
        client.post(f"/mock-plaid/webhooks/{key}/get", json={}).json()["webhook"][
            "last_status"
        ]
        == 503
    )
    client.post(f"/mock-plaid/webhooks/{key}/retry", json={})
    receipt = client.post(f"/mock-plaid/webhooks/{key}/get", json={}).json()["webhook"]
    assert receipt["delivered"] and receipt["attempts"] == 2
    raw = sent[0][1]["content"]
    token = sent[0][1]["headers"]["Plaid-Verification"]
    header, claims, signature = token.split(".")
    kid = json.loads(decode(header))["kid"]
    jwk = client.post("/webhook_verification_key/get", json={"key_id": kid}).json()[
        "key"
    ]
    numbers = ec.EllipticCurvePublicNumbers(
        int.from_bytes(decode(jwk["x"]), "big"),
        int.from_bytes(decode(jwk["y"]), "big"),
        ec.SECP256R1(),
    )
    sig = decode(signature)
    numbers.public_key().verify(
        encode_dss_signature(
            int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
        ),
        f"{header}.{claims}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )
    assert (
        json.loads(decode(claims))["request_body_sha256"]
        == hashlib.sha256(raw).hexdigest()
    )
    assert json.loads(raw)["status"] == "SUCCESS"
    assert json.loads(raw)["public_tokens"]
    monkeypatch.setattr(mock, "storage", PlaidStorage(mock.storage.path))
    assert public_key(mock.storage) == jwk


def test_transactions_not_ready(client):
    _, _, account = linked_account(client)
    client.post(
        f"/mock-plaid/items/{account['item_id']}/simulate",
        json={"outcome": "transactions_pending"},
    )
    data = {
        "access_token": account["access_token"],
        "start_date": "2020-01-01",
        "end_date": "2030-01-01",
    }
    assert (
        client.post("/transactions/get", json=data).json()["error_code"]
        == "PRODUCT_NOT_READY"
    )
    client.post(
        f"/mock-plaid/items/{account['item_id']}/simulate",
        json={"outcome": "transactions_ready"},
    )
    assert client.post("/transactions/get", json=data).status_code == 200


def test_transfer_lifecycle_and_event_cursor(client):
    intent = client.post(
        "/transfer/intent/create",
        json={
            "mode": "PAYMENT",
            "amount": "12.50",
            "iso_currency_code": "USD",
            "metadata": {"order_request_id": "local-order"},
            "funding_account_id": "funding-test",
        },
    ).json()["transfer_intent"]
    link = create_link(
        client, products=["transfer"], transfer={"intent_id": intent["id"]}
    )
    authorize(client, link)
    intent = client.post(
        "/transfer/intent/get", json={"transfer_intent_id": intent["id"]}
    ).json()["transfer_intent"]
    transfer_id = intent["transfer_id"]
    for state in ["posted", "settled", "funds_available", "returned"]:
        assert (
            client.post(
                "/sandbox/transfer/simulate",
                json={"transfer_id": transfer_id, "event_type": state},
            ).status_code
            == 200
        )
    remote = client.post("/transfer/get", json={"transfer_id": transfer_id}).json()[
        "transfer"
    ]
    assert remote["status"] == "returned"
    assert remote["amount"] == "12.50" and remote["type"] == "debit"
    assert remote["metadata"]["order_request_id"] == "local-order"
    assert remote["funding_account_id"] == "funding-test"
    first = client.post("/transfer/event/sync", json={"after_id": 0, "count": 2}).json()
    assert first["has_more"] and len(first["transfer_events"]) == 2
    last = client.post(
        "/transfer/event/sync",
        json={"after_id": first["transfer_events"][-1]["event_id"], "count": 100},
    ).json()
    assert not last["has_more"]
    assert [event["event_type"] for event in last["transfer_events"]] == [
        "settled",
        "funds_available",
        "returned",
    ]


@pytest.mark.parametrize(
    "outcome,status",
    [
        ("success", "PAYMENT_STATUS_SETTLED"),
        ("failed", "PAYMENT_STATUS_FAILED"),
        ("exit", "PAYMENT_STATUS_CANCELLED"),
    ],
)
def test_europe_payment_outcomes(client, outcome, status):
    user = client.post("/user/create", json={"client_user_id": "local-client"}).json()[
        "user_id"
    ]
    payment = client.post(
        "/payment_initiation/payment/create",
        json={
            "user_id": user,
            "recipient_id": "recipient-mock",
            "reference": "ORDER-1",
            "amount": {"value": 20.5, "currency": "EUR"},
        },
    ).json()
    link = create_link(
        client,
        products=["payment_initiation"],
        country_codes=["ES"],
        payment_initiation={"payment_id": payment["payment_id"]},
    )
    client.post(
        urlsplit(link["hosted_link_url"]).path + "/simulate", data={"outcome": outcome}
    )
    response = client.post(
        "/payment_initiation/payment/get", json={"payment_id": payment["payment_id"]}
    ).json()
    assert response["status"] == status
    assert response["recipient_id"] == "recipient-mock"
    assert response["reference"] == "ORDER-1" and response["amount"]["value"] == 20.5


def test_completion_redirect(client):
    link = create_link(
        client, hosted_link={"completion_redirect_uri": "http://merchant.test/complete"}
    )
    response = client.post(
        urlsplit(link["hosted_link_url"]).path + "/simulate",
        data={"outcome": "success"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "http://merchant.test/complete"


@pytest.mark.parametrize(
    "data", [{"access_token": "invalid"}, {"access_token": {"invalid": "value"}}]
)
def test_invalid_tokens_are_provider_errors(client, data):
    response = client.post("/accounts/get", json=data)
    assert response.status_code == 400
    assert "error_code" in response.json() and "request_id" in response.json()
    assert "detail" not in response.json()
