"""Redsys V1 REST, hosted checkout and explicit local simulation controls."""

import base64
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app import app, templates
from config import settings
from .paygold_storage import RedsysStorage
from .services import RedsysSignatureGenerator

storage = RedsysStorage(
    os.environ.get("REDSYS_MOCK_DB_PATH", settings.REDSYS_MOCK_DB_PATH)
)
DEFAULT_CONTROLS = {
    "creation_error": "",
    "creation_response": "normal",
    "expiration_error": "",
    "callback_mode": "normal",
}


def now():
    return datetime.now(timezone.utc)


def key():
    return (
        os.environ.get("REDSYS_MOCK_MERCHANT_KEY")
        or settings.REDSYS_MOCK_MERCHANT_KEY
        or settings.REDSYS_TEST_MERCHANT_KEY
    )


def fields(payload):
    return {str(name).upper(): value for name, value in payload.items()}


def unpack(envelope):
    if not isinstance(envelope, dict):
        raise ValueError("Invalid envelope")
    if envelope.get("Ds_SignatureVersion") != "HMAC_SHA256_V1":
        raise ValueError("Unsupported signature version")
    raw = envelope["Ds_MerchantParameters"]
    if not isinstance(raw, str) or len(raw) > 65536:
        raise ValueError("Invalid parameters")
    payload = json.loads(
        base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
    )
    if not isinstance(payload, dict):
        raise ValueError("Invalid parameters")
    values = fields(payload)
    order = values["DS_MERCHANT_ORDER"]
    if not isinstance(order, str) or not re.fullmatch(r"[A-Za-z0-9]{1,12}", order):
        raise ValueError("Invalid order")
    expected = RedsysSignatureGenerator.generate_signature(order, raw, key())
    signature = envelope["Ds_Signature"]
    if not isinstance(signature, str):
        raise ValueError("Invalid signature")
    supplied = base64.b64decode(
        signature + "=" * (-len(signature) % 4), altchars=b"-_", validate=True
    )
    if not hmac.compare_digest(supplied, base64.b64decode(expected)):
        raise ValueError("Invalid signature")
    return values


def signed(payload):
    raw = RedsysSignatureGenerator.build_merchant_parameters(payload)
    return {
        "Ds_SignatureVersion": "HMAC_SHA256_V1",
        "Ds_MerchantParameters": raw,
        "Ds_Signature": RedsysSignatureGenerator.generate_signature(
            payload["Ds_Order"], raw, key()
        ),
    }


def valid_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    parts = urlsplit(value)
    return (
        parts.scheme in {"http", "https"}
        and bool(parts.netloc)
        and not parts.username
        and not parts.password
    )


def deadline(value, start):
    if value is None:
        return start + timedelta(hours=24)
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,7}", value):
        return start + timedelta(minutes=int(value))
    return datetime.strptime(str(value), "%Y-%m-%d-%H.%M.%S.%f").replace(
        tzinfo=timezone.utc
    )


def provider_reply(link, operation, response):
    params = link["parameters"]
    return {
        "Ds_Order": params["DS_MERCHANT_ORDER"],
        "Ds_MerchantCode": params["DS_MERCHANT_MERCHANTCODE"],
        "Ds_Terminal": params["DS_MERCHANT_TERMINAL"],
        "Ds_Amount": params["DS_MERCHANT_AMOUNT"],
        "Ds_Currency": params["DS_MERCHANT_CURRENCY"],
        "Ds_TransactionType": operation,
        "Ds_Response": response,
    }


def order_id(params):
    merchant = str(params["DS_MERCHANT_MERCHANTCODE"]).lstrip("0") or "0"
    terminal = str(params["DS_MERCHANT_TERMINAL"]).lstrip("0") or "0"
    return f"order:{merchant}:{terminal}:{params['DS_MERCHANT_ORDER']}"


def expired(link):
    return datetime.fromisoformat(link["expires_at"]) <= now()


def public_link(link):
    params = link["parameters"]
    return {
        name: link[name]
        for name in (
            "token",
            "flow",
            "state",
            "created_at",
            "expires_at",
            "url",
            "controls",
            "deliveries",
        )
    } | {
        "order": params["DS_MERCHANT_ORDER"],
        "merchant": params["DS_MERCHANT_MERCHANTCODE"],
        "terminal": params["DS_MERCHANT_TERMINAL"],
        "amount": params["DS_MERCHANT_AMOUNT"],
        "currency": params["DS_MERCHANT_CURRENCY"],
        "expired": expired(link),
    }


def create_link(params, request, flow):
    for name, pattern in {
        "DS_MERCHANT_MERCHANTCODE": r"[0-9]{9}",
        "DS_MERCHANT_TERMINAL": r"(?!0+$)[0-9]{1,3}",
        "DS_MERCHANT_CURRENCY": r"[0-9]{3}",
        "DS_MERCHANT_AMOUNT": r"[1-9][0-9]{0,11}",
    }.items():
        if not re.fullmatch(pattern, str(params.get(name, ""))):
            return {"errorCode": "SIS0007"}
    if flow == "paygold" and not re.fullmatch(
        r"[0-9]{4}[A-Za-z0-9]{0,8}", params["DS_MERCHANT_ORDER"]
    ):
        return {"errorCode": "SIS0014"}
    for name in ("DS_MERCHANT_MERCHANTURL", "DS_MERCHANT_URLOK", "DS_MERCHANT_URLKO"):
        if params.get(name) and not valid_url(params[name]):
            return {"errorCode": "SIS0007"}
    start = now()
    expiry = deadline(params.get("DS_MERCHANT_P2F_EXPIRYDATE"), start)
    if expiry <= start:
        return {"errorCode": "SIS0007"}
    origin = (
        os.environ.get("REDSYS_MOCK_PUBLIC_URL")
        or settings.REDSYS_MOCK_PUBLIC_URL
        or str(request.base_url)
    ).rstrip("/")
    if not valid_url(origin):
        raise ValueError("Invalid public origin")
    with storage.transaction() as connection:
        controls = storage.get(connection, "controls") or DEFAULT_CONTROLS.copy()
        if controls["creation_error"]:
            return {"errorCode": controls["creation_error"]}
        if storage.get(connection, order_id(params)):
            return {"errorCode": "SIS0051"}
        token = secrets.token_urlsafe(24)
        link = {
            "token": token,
            "flow": flow,
            "state": "active",
            "created_at": start.isoformat(),
            "expires_at": expiry.isoformat(),
            "url": f"{origin}/sis/p2f?t={token}",
            "parameters": params,
            "controls": controls,
            "deliveries": [],
            "event": None,
        }
        # Signing must succeed before persisting any provider-side creation.
        payload = provider_reply(link, "F" if flow == "paygold" else "0", "9998")
        payload["Ds_UrlPago2Fases"] = link["url"]
        reply = signed(payload)
        storage.save(connection, "link:" + token, link)
        storage.save(connection, order_id(params), {"token": token})
    if controls["creation_response"] == "bad_signature":
        reply["Ds_Signature"] = base64.b64encode(bytes(32)).decode()
    elif controls["creation_response"] == "http503_after_create":
        return JSONResponse({"errorCode": "SIS0001"}, status_code=503)
    elif controls["creation_response"] == "malformed_after_create":
        return HTMLResponse("Mock response lost after link creation", status_code=200)
    return reply


def change_expiration(params):
    with storage.transaction() as connection:
        identity = storage.get(connection, order_id(params))
        link = (
            storage.get(connection, "link:" + identity["token"]) if identity else None
        )
        if not link or link["flow"] != "paygold" or link["state"] == "paid":
            return {"errorCode": "SIS0325"}
        if link["controls"]["expiration_error"]:
            return {"errorCode": link["controls"]["expiration_error"]}
        expiry = deadline(
            params["DS_MERCHANT_P2F_EXPIRYDATE"],
            datetime.fromisoformat(link["created_at"]),
        )
        reply = signed(provider_reply(link, "51", "0000"))
        link["expires_at"] = expiry.isoformat()
        link["state"] = "cancelled" if expiry <= now() else "active"
        storage.save(connection, "link:" + link["token"], link)
        return reply


@app.post("/sis/rest/trataPeticionREST", tags=["Redsys PayGold"])
async def redsys_rest(request: Request):
    try:
        params = unpack(await request.json())
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return {"errorCode": "SIS0042"}
    try:
        operation = params.get("DS_MERCHANT_TRANSACTIONTYPE")
        if operation == "F":
            return create_link(params, request, "paygold")
        if operation == "51":
            return change_expiration(params)
        return {"errorCode": "SIS0023"}
    except (ValueError, TypeError, KeyError, OverflowError):
        return {"errorCode": "SIS0007"}


@app.post("/sis/realizarPago", include_in_schema=False)
async def redsys_legacy_checkout(request: Request):
    try:
        params = unpack(dict(await request.form()))
        if params.get("DS_MERCHANT_TRANSACTIONTYPE") != "0":
            return JSONResponse({"errorCode": "SIS0023"}, status_code=400)
        result = create_link(params, request, "legacy_form")
        if isinstance(result, dict) and "Ds_MerchantParameters" in result:
            raw = RedsysSignatureGenerator.decode_merchant_parameters(
                result["Ds_MerchantParameters"]
            )
            return RedirectResponse(raw["Ds_UrlPago2Fases"], status_code=303)
        return result
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        return JSONResponse({"errorCode": "SIS0042"}, status_code=400)


@app.get("/sis/p2f", response_class=HTMLResponse, include_in_schema=False)
async def redsys_hosted_checkout(request: Request, t: str):
    with storage.transaction() as connection:
        link = storage.get(connection, "link:" + t)
    if not link:
        raise HTTPException(404, "Unknown mock link")
    return templates.TemplateResponse(
        request=request,
        name="redsys_checkout.html",
        context={"link": public_link(link)},
    )


class Controls(BaseModel):
    token: str | None = None
    creation_error: str = Field(default="", pattern=r"^(?:SIS[0-9]{4})?$")
    creation_response: Literal[
        "normal", "bad_signature", "http503_after_create", "malformed_after_create"
    ] = "normal"
    expiration_error: str = Field(default="", pattern=r"^(?:SIS[0-9]{4})?$")
    callback_mode: Literal["normal", "omitted", "invalid_signature", "duplicate"] = (
        "normal"
    )


@app.post("/mock/redsys/paygold/controls", tags=["Redsys PayGold"])
async def redsys_controls(controls: Controls):
    values = controls.model_dump(exclude={"token"})
    with storage.transaction() as connection:
        if controls.token:
            link = storage.get(connection, "link:" + controls.token)
            if not link:
                raise HTTPException(404, "Unknown mock link")
            link["controls"] = values
            storage.save(connection, "link:" + controls.token, link)
        else:
            storage.save(connection, "controls", values)
    return values


async def deliver(token, envelope, url, mode):
    if mode == "omitted" or not url:
        return
    if mode == "invalid_signature":
        envelope = {**envelope, "Ds_Signature": "INVALID_SIGNATURE_FOR_TESTING"}
    for _ in range(2 if mode == "duplicate" else 1):
        try:
            async with httpx.AsyncClient(follow_redirects=False) as client:
                response = await client.post(url, data=envelope, timeout=10)
            delivery = {"at": now().isoformat(), "status": response.status_code}
        except httpx.HTTPError:
            delivery = {
                "at": now().isoformat(),
                "status": None,
                "error": "Callback transport failed",
            }
        with storage.transaction() as connection:
            link = storage.get(connection, "link:" + token)
            link["deliveries"].append(delivery)
            storage.save(connection, "link:" + token, link)


class CheckoutResult(BaseModel):
    response_code: str = Field(default="0000", pattern=r"^[0-9]{4}$")
    card_brand: Literal["1", "2", "8"] = "1"
    last_four: str = Field(default="0004", pattern=r"^[0-9]{4}$")


@app.post("/mock/redsys/paygold/links/{token}/simulate", tags=["Redsys PayGold"])
async def redsys_checkout_result(token: str, result: CheckoutResult):
    with storage.transaction() as connection:
        link = storage.get(connection, "link:" + token)
        if not link:
            raise HTTPException(404, "Unknown mock link")
        if link["state"] != "active" or expired(link):
            raise HTTPException(409, "The link is paid, deactivated or expired")
        code = int(result.response_code)
        paid = 0 <= code <= 99
        payload = provider_reply(
            link, "F" if link["flow"] == "paygold" else "0", result.response_code
        )
        payload.update(
            Ds_Card_Brand=result.card_brand,
            Ds_CardNumber="************" + result.last_four,
            Ds_SecurePayment="1",
            Ds_AuthorisationCode="123456" if paid else "",
        )
        envelope = signed(payload)
        link["event"] = envelope
        link["state"] = "paid" if paid else "cancelled" if code == 9915 else "active"
        storage.save(connection, "link:" + token, link)
        params = link["parameters"]
        url = params.get("DS_MERCHANT_MERCHANTURL", "")
        return_url = params.get(
            "DS_MERCHANT_URLOK" if paid else "DS_MERCHANT_URLKO", ""
        )
    await deliver(token, envelope, url, link["controls"]["callback_mode"])
    return {
        "state": link["state"],
        "response_code": result.response_code,
        "return_url": return_url,
    }


@app.post("/mock/redsys/paygold/links/{token}/replay", tags=["Redsys PayGold"])
async def redsys_replay(token: str):
    with storage.transaction() as connection:
        link = storage.get(connection, "link:" + token)
    if not link or not link["event"]:
        raise HTTPException(404, "No recorded callback to replay")
    await deliver(
        token,
        link["event"],
        link["parameters"].get("DS_MERCHANT_MERCHANTURL", ""),
        link["controls"]["callback_mode"],
    )
    return {"replayed": True}


@app.get("/mock/redsys/paygold/links", tags=["Redsys PayGold"])
async def redsys_links():
    with storage.transaction() as connection:
        return {
            "controls": storage.get(connection, "controls") or DEFAULT_CONTROLS,
            "links": [public_link(link) for link in storage.links(connection)],
        }


@app.get("/mock/redsys/paygold", response_class=HTMLResponse, include_in_schema=False)
async def redsys_dashboard(request: Request):
    return templates.TemplateResponse(
        request=request, name="redsys_paygold.html", context={}
    )
