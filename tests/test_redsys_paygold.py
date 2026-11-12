"""Redsys REST/form coexistence and durable hosted-link behavior without external HTTP."""

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app import app
from config import settings
from redsys import main as redsys_routes  # noqa: F401
from redsys import paygold
from redsys.paygold_storage import RedsysStorage
from redsys.services import RedsysSignatureGenerator as Signer

# Public sandbox test vector; never reads runtime credentials.
TEST_KEY = "sq7HjrUOBfKmC576ILgskD5srU870gJ7"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(paygold, "storage", RedsysStorage(tmp_path / "redsys.sqlite3"))
    monkeypatch.setenv("REDSYS_MOCK_MERCHANT_KEY", TEST_KEY)
    monkeypatch.setattr(settings, "REDSYS_TEST_MERCHANT_KEY", TEST_KEY)
    monkeypatch.setenv("REDSYS_MOCK_PUBLIC_URL", "http://mock.example")
    requests = []
    real_client = httpx.AsyncClient

    def receive(request):
        requests.append(request)
        return httpx.Response(201, json={"received": True})

    monkeypatch.setattr(
        paygold.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(receive), **kwargs),
    )
    instance = TestClient(app)
    instance.deliveries = requests
    return instance


def payload(**changes):
    return {
        "DS_MERCHANT_ORDER": "000100000015",
        "DS_MERCHANT_MERCHANTCODE": "999008881",
        "DS_MERCHANT_TERMINAL": "002",
        "DS_MERCHANT_AMOUNT": "300",
        "DS_MERCHANT_CURRENCY": "840",
        "DS_MERCHANT_TRANSACTIONTYPE": "F",
        "DS_MERCHANT_URLOK": "https://shop.example/return?q=ok",
        "DS_MERCHANT_URLKO": "https://shop.example/return?q=cancel",
        "DS_MERCHANT_MERCHANTURL": "https://callback.example/custom/path?source=fixture",
        **changes,
    }


def envelope(params):
    raw = Signer.build_merchant_parameters(params)
    return {
        "Ds_SignatureVersion": "HMAC_SHA256_V1",
        "Ds_MerchantParameters": raw,
        "Ds_Signature": Signer.generate_signature(
            params["DS_MERCHANT_ORDER"], raw, TEST_KEY
        ),
    }


def decoded(reply):
    raw = reply["Ds_MerchantParameters"]
    result = Signer.decode_merchant_parameters(raw)
    expected = Signer.generate_signature(result["Ds_Order"], raw, TEST_KEY)
    assert reply["Ds_Signature"] == expected
    return result


def create(client, **changes):
    result = client.post(
        "/sis/rest/trataPeticionREST", json=envelope(payload(**changes))
    )
    assert result.status_code == 200
    body = decoded(result.json())
    assert body["Ds_Response"] == "9998"
    return parse_qs(urlsplit(body["Ds_UrlPago2Fases"]).query)["t"][0], body


def links(client):
    return client.get("/mock/redsys/paygold/links").json()["links"]


def simulate(client, token, **changes):
    return client.post(f"/mock/redsys/paygold/links/{token}/simulate", json=changes)


def expire(client, value, **changes):
    params = payload(
        DS_MERCHANT_TRANSACTIONTYPE="51", DS_MERCHANT_P2F_EXPIRYDATE=value, **changes
    )
    return client.post("/sis/rest/trataPeticionREST", json=envelope(params))


@pytest.mark.parametrize("currency", ["840", "978"])
def test_signed_native_creation_default_expiry_and_restart(
    client, currency, monkeypatch
):
    token, reply = create(client, DS_MERCHANT_CURRENCY=currency)
    assert reply["Ds_TransactionType"] == "F" and reply["Ds_Amount"] == "300"
    assert reply["Ds_Currency"] == currency and reply["Ds_Terminal"] == "002"
    link = links(client)[0]
    assert (
        timedelta(hours=23, minutes=59)
        < paygold.datetime.fromisoformat(link["expires_at"]) - paygold.now()
        <= timedelta(hours=24)
    )
    monkeypatch.setattr(paygold, "storage", RedsysStorage(paygold.storage.path))
    assert links(client)[0] == link
    assert client.get("/sis/p2f", params={"t": token}).status_code == 200
    assert not client.deliveries


def test_exact_30_minute_and_absolute_expiration(client):
    start = paygold.now()
    token, _ = create(client, DS_MERCHANT_P2F_EXPIRYDATE="30")
    assert (
        timedelta(minutes=29, seconds=59)
        < paygold.datetime.fromisoformat(links(client)[0]["expires_at"]) - start
        < timedelta(minutes=31)
    )
    target = (paygold.now() + timedelta(hours=2)).strftime("%Y-%m-%d-%H.%M.%S.%f")[:-3]
    assert decoded(expire(client, target).json())["Ds_TransactionType"] == "51"
    with paygold.storage.transaction() as connection:
        assert paygold.storage.get(connection, "link:" + token)[
            "expires_at"
        ].startswith(target[:10])
    assert not client.deliveries


@pytest.mark.parametrize("variant", ["signature", "parameters", "version", "malformed"])
def test_invalid_envelopes_do_not_create_links(client, variant):
    body = envelope(payload())
    if variant == "signature":
        body["Ds_Signature"] = "invalid!"
    elif variant == "parameters":
        body["Ds_MerchantParameters"] = Signer.build_merchant_parameters(
            payload(DS_MERCHANT_AMOUNT="999")
        )
    elif variant == "version":
        body["Ds_SignatureVersion"] = "HMAC_SHA512_V2"
    else:
        body["Ds_MerchantParameters"] = "not JSON"
    assert client.post("/sis/rest/trataPeticionREST", json=body).json() == {
        "errorCode": "SIS0042"
    }
    assert links(client) == []


def test_duplicate_creation_does_not_replace_link_and_terminal_scopes_do_not_collide(
    client,
):
    token, _ = create(client)
    assert client.post(
        "/sis/rest/trataPeticionREST", json=envelope(payload())
    ).json() == {"errorCode": "SIS0051"}
    other, _ = create(client, DS_MERCHANT_TERMINAL="003")
    assert other != token and len(links(client)) == 2
    assert expire(client, "1", DS_MERCHANT_TERMINAL="999").json() == {
        "errorCode": "SIS0325"
    }


def test_payment_callback_exact_destination_signature_card_and_returns(client):
    token, _ = create(client)
    result = simulate(client, token, card_brand="2", last_four="1234")
    assert result.json()["return_url"] == "https://shop.example/return?q=ok"
    assert result.json()["state"] == "paid"
    assert len(client.deliveries) == 1
    request = client.deliveries[0]
    assert str(request.url) == "https://callback.example/custom/path?source=fixture"
    form = {
        key: values[0] for key, values in parse_qs(request.content.decode()).items()
    }
    response = decoded(form)
    assert response["Ds_Amount"] == "300" and response["Ds_Currency"] == "840"
    assert (
        response["Ds_Order"] == "000100000015" and response["Ds_TransactionType"] == "F"
    )
    assert response["Ds_Card_Brand"] == "2" and response["Ds_CardNumber"].endswith(
        "1234"
    )
    assert simulate(client, token).status_code == 409
    assert expire(client, "1").json() == {"errorCode": "SIS0325"}
    assert links(client)[0]["state"] == "paid" and len(client.deliveries) == 1


def test_decline_pending_and_later_success_are_distinct_from_link_creation(client):
    token, _ = create(client)
    failed = simulate(client, token, response_code="0116")
    assert failed.json()["state"] == "active"
    assert failed.json()["return_url"].endswith("q=cancel")
    assert simulate(client, token, response_code="9998").json()["state"] == "active"
    assert simulate(client, token).json()["state"] == "paid"
    assert len(client.deliveries) == 3


def test_remote_deactivation_and_native_expiry_block_payment_without_fabricating_callbacks(
    client, monkeypatch
):
    token, _ = create(client)
    past = (paygold.now() - timedelta(days=2)).strftime("%Y-%m-%d-%H.%M.%S.%f")[:-3]
    assert decoded(expire(client, past).json())["Ds_Response"] == "0000"
    assert simulate(client, token).status_code == 409
    assert links(client)[0]["state"] == "cancelled" and not client.deliveries
    other, _ = create(
        client, DS_MERCHANT_ORDER="000100000016", DS_MERCHANT_P2F_EXPIRYDATE="1"
    )
    future = paygold.now() + timedelta(minutes=2)
    monkeypatch.setattr(paygold, "now", lambda: future)
    assert links(client)[0]["expired"]
    assert simulate(client, other).status_code == 409


@pytest.mark.parametrize(
    "mode,count", [("omitted", 0), ("invalid_signature", 1), ("duplicate", 2)]
)
def test_callback_scenarios_and_replay_preserve_stored_event(client, mode, count):
    client.post("/mock/redsys/paygold/controls", json={"callback_mode": mode})
    token, _ = create(client)
    simulate(client, token)
    assert len(client.deliveries) == count
    if mode == "invalid_signature":
        assert b"INVALID_SIGNATURE_FOR_TESTING" in client.deliveries[0].content
    if mode == "duplicate":
        assert client.deliveries[0].content == client.deliveries[1].content
    client.post(
        "/mock/redsys/paygold/controls",
        json={"token": token, "callback_mode": "normal"},
    )
    assert client.post(f"/mock/redsys/paygold/links/{token}/replay").status_code == 200
    assert len(client.deliveries) == count + 1
    form = {
        key: values[0]
        for key, values in parse_qs(client.deliveries[-1].content.decode()).items()
    }
    assert decoded(form)["Ds_Response"] == "0000"


@pytest.mark.parametrize(
    "scenario", ["bad_signature", "http503_after_create", "malformed_after_create"]
)
def test_unknown_creation_response_keeps_durable_link(client, scenario):
    client.post("/mock/redsys/paygold/controls", json={"creation_response": scenario})
    result = client.post("/sis/rest/trataPeticionREST", json=envelope(payload()))
    assert len(links(client)) == 1 and not client.deliveries
    if scenario == "http503_after_create":
        assert result.status_code == 503
    elif scenario == "malformed_after_create":
        assert "text/html" in result.headers["content-type"]
    else:
        raw = result.json()
        params = Signer.decode_merchant_parameters(raw["Ds_MerchantParameters"])
        assert raw["Ds_Signature"] != Signer.generate_signature(
            params["Ds_Order"], raw["Ds_MerchantParameters"], TEST_KEY
        )


def test_creation_error_and_per_link_expiration_error_are_observable(client):
    client.post("/mock/redsys/paygold/controls", json={"creation_error": "SIS0487"})
    assert client.post(
        "/sis/rest/trataPeticionREST", json=envelope(payload())
    ).json() == {"errorCode": "SIS0487"}
    assert links(client) == []
    client.post("/mock/redsys/paygold/controls", json={})
    token, _ = create(client)
    client.post(
        "/mock/redsys/paygold/controls",
        json={"token": token, "expiration_error": "SIS0001"},
    )
    assert expire(client, "1").json() == {"errorCode": "SIS0001"}
    assert links(client)[0]["state"] == "active"


def test_legacy_form_and_manual_ui_remain_available(client):
    params = payload(
        DS_MERCHANT_ORDER="S00000015",
        DS_MERCHANT_TRANSACTIONTYPE="0",
        DS_MERCHANT_MERCHANTURL="http://consumer.example/webhook?con=S00000015",
    )
    result = client.post(
        "/sis/realizarPago", data=envelope(params), follow_redirects=False
    )
    assert result.status_code == 303
    token = parse_qs(urlsplit(result.headers["location"]).query)["t"][0]
    simulate(client, token, response_code="9915")
    form = {
        key: values[0]
        for key, values in parse_qs(client.deliveries[0].content.decode()).items()
    }
    assert decoded(form)["Ds_TransactionType"] == "0"
    assert str(client.deliveries[0].url).endswith("?con=S00000015")
    assert client.get("/redsys").status_code == 200
    assert client.get("/mock/redsys/paygold").status_code == 200


@pytest.mark.parametrize("body", [[], None, "invalid"])
def test_non_object_envelopes_return_provider_error_without_mutation(client, body):
    assert client.post("/sis/rest/trataPeticionREST", json=body).json() == {
        "errorCode": "SIS0042"
    }
    assert not links(client)


def test_distinct_native_orders_preserve_leading_zeros(client):
    first, _ = create(client, DS_MERCHANT_ORDER="0001")
    second, _ = create(client, DS_MERCHANT_ORDER="00001")
    assert first != second
    assert len(links(client)) == 2


def test_concurrent_checkout_records_only_one_successful_transition(client):
    from concurrent.futures import ThreadPoolExecutor

    token, _ = create(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: simulate(client, token).status_code, range(2))
        )
    assert sorted(results) == [200, 409]
    assert len(client.deliveries) == 1
    assert links(client)[0]["state"] == "paid"


def test_failed_callback_transport_preserves_paid_result_and_is_safe_to_replay(
    client, monkeypatch
):
    real_client = httpx.AsyncClient

    class Unavailable:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError(
                "Do not copy request exception strings into stored diagnostics"
            )

    token, _ = create(client)
    monkeypatch.setattr(paygold.httpx, "AsyncClient", lambda **kwargs: Unavailable())
    assert simulate(client, token).json()["state"] == "paid"
    delivery = links(client)[0]["deliveries"][0]
    assert (
        delivery["status"] is None and delivery["error"] == "Callback transport failed"
    )
    monkeypatch.setattr(paygold.httpx, "AsyncClient", real_client)
    assert client.post(f"/mock/redsys/paygold/links/{token}/replay").status_code == 200
    assert links(client)[0]["state"] == "paid" and len(client.deliveries) == 1


@pytest.mark.parametrize("terminal", ["0", "00", "000", "1000"])
def test_invalid_terminal_does_not_create_link(client, terminal):
    result = client.post(
        "/sis/rest/trataPeticionREST",
        json=envelope(payload(DS_MERCHANT_TERMINAL=terminal)),
    )
    assert result.json() == {"errorCode": "SIS0007"}
    assert not links(client)


def test_mock_settings_accept_dotenv_configuration_without_unknown_fields(tmp_path):
    from config import Settings

    env = tmp_path / "mock.env"
    env.write_text(
        "REDSYS_MOCK_PUBLIC_URL=http://fixture.example\nREDSYS_MOCK_DB_PATH=/tmp/fixture-redsys.sqlite3\nREDSYS_MOCK_MERCHANT_KEY=disposable-fixture\n"
    )
    configured = Settings(_env_file=env)
    assert configured.REDSYS_MOCK_PUBLIC_URL == "http://fixture.example"
    assert str(configured.REDSYS_MOCK_DB_PATH) == "/tmp/fixture-redsys.sqlite3"
