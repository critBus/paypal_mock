"""Mock local de Stripe para probar la integración de crinnopayments.

Cubre el subconjunto usado por el módulo ``stripe`` de crinnopayments:

* Payment Links: create/retrieve/update(active=false)
* Checkout Sessions: listadas por Payment Link
* PaymentIntents: retrieve/capture/cancel
* Webhook Endpoints: create/retrieve/update/delete
* firma ``Stripe-Signature`` con el signing secret del endpoint
* checkout HTML manual para simular pago correcto, fallo, requires_action,
  processing y expiración de sesión
* captura manual: un pago correcto deja el PaymentIntent en ``requires_capture``
  para que crinnopayments lo capture o cancele posteriormente.

No realiza cobros reales.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
from fastapi import BackgroundTasks, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from app import app

from .storage import StripeMockStorage


__all__ = []

BASE_DIR = Path(__file__).resolve().parents[1]
storage = StripeMockStorage(
    os.getenv("STRIPE_MOCK_DB_PATH", str(BASE_DIR / "data" / "stripe.sqlite3"))
)

STRIPE_MOCK_SECRET_KEY = os.getenv("STRIPE_MOCK_SECRET_KEY", "sk_test_mock_crinnopayments")
STRIPE_MOCK_WEBHOOK_URL = os.getenv(
    "STRIPE_MOCK_WEBHOOK_URL",
    "http://localhost:8001/api/stripe/webhooks/notifications/",
)
STRIPE_MOCK_WEBHOOK_SECRET = os.getenv(
    "STRIPE_MOCK_WEBHOOK_SECRET",
    "whsec_mock_crinnopayments",
)
STRIPE_MOCK_STRICT_AUTH = os.getenv("STRIPE_MOCK_STRICT_AUTH", "false").lower() in {
    "1",
    "true",
    "yes",
}
STRIPE_MOCK_LIVEMODE = os.getenv("STRIPE_MOCK_LIVEMODE", "false").lower() in {
    "1",
    "true",
    "yes",
}
STRIPE_MOCK_API_VERSION = os.getenv("STRIPE_MOCK_API_VERSION", "2025-07-30.basil")


def _unix() -> int:
    return int(datetime.now(UTC).timestamp())


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18).replace('-', '').replace('_', '')[:24]}"


def _request_id() -> str:
    return _id("req")


def _public_url(request: Request) -> str:
    configured = os.getenv("STRIPE_MOCK_PUBLIC_URL", "").strip().rstrip("/")
    return configured or str(request.base_url).rstrip("/")


def _require_auth(authorization: str | None) -> None:
    if not STRIPE_MOCK_STRICT_AUTH:
        return
    expected = f"Bearer {STRIPE_MOCK_SECRET_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid API Key provided")


def _clean_internal(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _clean_internal(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_clean_internal(item) for item in value]
    return value


def _response(content: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_clean_internal(content),
        headers={"Request-Id": _request_id()},
    )


def _error(
    message: str,
    *,
    status_code: int = 400,
    code: str = "parameter_invalid",
    param: str | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "code": code,
        }
    }
    if param:
        body["error"]["param"] = param
    return _response(body, status_code=status_code)


async def _form_pairs(request: Request) -> list[tuple[str, str]]:
    raw = (await request.body()).decode("utf-8", errors="replace")
    return parse_qsl(raw, keep_blank_values=True)


def _first(pairs: list[tuple[str, str]], key: str, default: str = "") -> str:
    for name, value in pairs:
        if name == key:
            return value
    return default


def _all(pairs: list[tuple[str, str]], key: str) -> list[str]:
    return [value for name, value in pairs if name == key]


def _bool(value: str | bool | None, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).lower() in {"1", "true", "yes", "on"}


def _metadata(pairs: list[tuple[str, str]], prefix: str) -> dict[str, str]:
    start = prefix + "["
    result: dict[str, str] = {}
    for key, value in pairs:
        if key.startswith(start) and key.endswith("]"):
            inner = key[len(start) : -1]
            if "[" not in inner and "]" not in inner:
                result[inner] = value
    return result


def _get_link(identifier: str) -> dict[str, Any]:
    value = storage.get_payment_link(identifier)
    if not value:
        raise HTTPException(status_code=404, detail="No such payment_link")
    return value


def _get_session(identifier: str) -> dict[str, Any]:
    value = storage.get_checkout_session(identifier)
    if not value:
        raise HTTPException(status_code=404, detail="No such checkout.session")
    return value


def _get_intent(identifier: str) -> dict[str, Any]:
    value = storage.get_payment_intent(identifier)
    if not value:
        raise HTTPException(status_code=404, detail="No such payment_intent")
    return value


def _payment_link_response(link: dict[str, Any]) -> dict[str, Any]:
    return _clean_internal(link)


def _payment_intent_response(intent: dict[str, Any]) -> dict[str, Any]:
    return _clean_internal(intent)


def _session_response(session: dict[str, Any]) -> dict[str, Any]:
    return _clean_internal(session)


def _webhook_endpoint_response(endpoint: dict[str, Any], *, include_secret: bool = False) -> dict[str, Any]:
    result = _clean_internal(endpoint)
    if not include_secret:
        result.pop("secret", None)
    return result


def _new_session(link: dict[str, Any], request: Request) -> dict[str, Any]:
    session_id = _id("cs_test")
    now = _unix()
    session = {
        "id": session_id,
        "object": "checkout.session",
        "after_expiration": None,
        "allow_promotion_codes": False,
        "amount_subtotal": link["amount"],
        "amount_total": link["amount"],
        "cancel_url": None,
        "client_reference_id": link.get("metadata", {}).get("order_number"),
        "created": now,
        "currency": link["currency"],
        "customer": None,
        "customer_details": None,
        "customer_email": None,
        "expires_at": now + 24 * 60 * 60,
        "livemode": STRIPE_MOCK_LIVEMODE,
        "metadata": dict(link.get("metadata") or {}),
        "mode": "payment",
        "payment_intent": None,
        "payment_link": link["id"],
        "payment_method_types": list(link.get("payment_method_types") or ["card"]),
        "payment_status": "unpaid",
        "status": "open",
        "success_url": (link.get("after_completion") or {}).get("redirect", {}).get("url"),
        "url": f"{_public_url(request)}/mock-stripe/checkout/{session_id}",
    }
    storage.save_checkout_session(session)
    return session


def _current_or_new_session(link: dict[str, Any], request: Request) -> dict[str, Any]:
    sessions = storage.list_checkout_sessions(payment_link_id=link["id"], limit=1)
    if sessions and sessions[0].get("status") == "open":
        return sessions[0]
    return _new_session(link, request)


def _new_or_current_intent(link: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    if session.get("payment_intent"):
        current = storage.get_payment_intent(session["payment_intent"])
        if current:
            return current

    intent_id = _id("pi")
    capture_method = (link.get("payment_intent_data") or {}).get("capture_method") or "automatic"
    intent = {
        "id": intent_id,
        "object": "payment_intent",
        "amount": int(link["amount"]),
        "amount_capturable": 0,
        "amount_received": 0,
        "canceled_at": None,
        "cancellation_reason": None,
        "capture_method": capture_method,
        "client_secret": f"{intent_id}_secret_{secrets.token_urlsafe(16)}",
        "confirmation_method": "automatic",
        "created": _unix(),
        "currency": link["currency"],
        "description": link.get("description") or "",
        "last_payment_error": None,
        "latest_charge": None,
        "livemode": STRIPE_MOCK_LIVEMODE,
        "metadata": dict((link.get("payment_intent_data") or {}).get("metadata") or link.get("metadata") or {}),
        "next_action": None,
        "payment_method": _id("pm_card"),
        "payment_method_types": list(link.get("payment_method_types") or ["card"]),
        "status": "requires_payment_method",
        "_payment_link_id": link["id"],
        "_checkout_session_id": session["id"],
    }
    storage.save_payment_intent(intent)
    session["payment_intent"] = intent_id
    storage.save_checkout_session(session)
    return intent


def _new_event(event_type: str, data_object: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _id("evt"),
        "object": "event",
        "api_version": STRIPE_MOCK_API_VERSION,
        "created": _unix(),
        "data": {"object": _clean_internal(data_object)},
        "livemode": STRIPE_MOCK_LIVEMODE,
        "pending_webhooks": 1,
        "request": {"id": _request_id(), "idempotency_key": None},
        "type": event_type,
    }


def _stripe_signature(raw_body: str, signing_secret: str, timestamp: int) -> str:
    signed_payload = f"{timestamp}.{raw_body}".encode("utf-8")
    digest = hmac.new(signing_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _event_targets(event_type: str) -> list[dict[str, Any]]:
    targets = []
    for endpoint in storage.list_webhook_endpoints():
        if endpoint.get("status") != "enabled" or endpoint.get("disabled") is True:
            continue
        enabled = endpoint.get("enabled_events") or []
        if "*" in enabled or event_type in enabled:
            targets.append(endpoint)

    if not targets and STRIPE_MOCK_WEBHOOK_URL:
        targets.append(
            {
                "id": "we_environment",
                "object": "webhook_endpoint",
                "url": STRIPE_MOCK_WEBHOOK_URL,
                "enabled_events": ["*"],
                "secret": STRIPE_MOCK_WEBHOOK_SECRET,
                "status": "enabled",
                "livemode": STRIPE_MOCK_LIVEMODE,
            }
        )
    return targets


async def _deliver_event(event: dict[str, Any], *, bad_signature: bool = False) -> None:
    raw_body = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    targets = _event_targets(event["type"])
    try:
        attempts = max(1, min(10, int(os.getenv("STRIPE_MOCK_WEBHOOK_ATTEMPTS", "3"))))
    except ValueError:
        attempts = 3

    for endpoint in targets:
        for attempt in range(1, attempts + 1):
            timestamp = int(time.time())
            secret = endpoint.get("secret") or STRIPE_MOCK_WEBHOOK_SECRET
            signature = _stripe_signature(raw_body, secret, timestamp)
            if bad_signature:
                signature = f"t={timestamp},v1={'0' * 64}"

            delivery: dict[str, Any] = {
                "event_id": event["id"],
                "event_type": event["type"],
                "webhook_endpoint_id": endpoint.get("id"),
                "target_url": endpoint.get("url"),
                "attempt": attempt,
                "created_at": _iso_now(),
                "status_code": None,
                "success": False,
                "error": None,
                "response": None,
                "stripe_signature": signature,
                "payload": event,
            }
            try:
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                    response = await client.post(
                        endpoint["url"],
                        content=raw_body.encode("utf-8"),
                        headers={
                            "Content-Type": "application/json",
                            "Stripe-Signature": signature,
                            "User-Agent": "Stripe/1.0 (+https://stripe.com/docs/webhooks) mock-crinnopayments",
                        },
                    )
                delivery["status_code"] = response.status_code
                delivery["response"] = response.text[:2000]
                delivery["success"] = 200 <= response.status_code < 300
            except httpx.HTTPError as exc:
                delivery["error"] = type(exc).__name__

            storage.add_webhook_delivery(event["id"], delivery)
            if delivery["success"]:
                break
            if attempt < attempts:
                await asyncio.sleep(min(2 ** (attempt - 1), 4))


async def _queue_events(background_tasks: BackgroundTasks, events: list[dict[str, Any]]) -> None:
    for event in events:
        background_tasks.add_task(_deliver_event, event)


@app.post("/v1/payment_links", tags=["Stripe"])
async def stripe_create_payment_link(
    request: Request,
    authorization: str | None = Header(None),
):
    _require_auth(authorization)
    pairs = await _form_pairs(request)

    currency = _first(pairs, "line_items[0][price_data][currency]").lower()
    raw_amount = _first(pairs, "line_items[0][price_data][unit_amount]")
    if not currency:
        return _error("Missing required param: line_items[0][price_data][currency]", param="line_items")
    try:
        amount = int(raw_amount)
    except (TypeError, ValueError):
        return _error("Invalid integer: line_items[0][price_data][unit_amount]", param="line_items")
    if amount <= 0:
        return _error("Amount must be greater than zero", param="line_items")

    capture_method = _first(pairs, "payment_intent_data[capture_method]", "automatic").lower()
    if capture_method not in {"automatic", "manual"}:
        return _error("Invalid capture_method", param="payment_intent_data[capture_method]")

    metadata = _metadata(pairs, "metadata")
    payment_intent_metadata = _metadata(pairs, "payment_intent_data[metadata]") or dict(metadata)
    product_name = _first(pairs, "line_items[0][price_data][product_data][name]", "Mock Stripe payment")
    description = _first(pairs, "line_items[0][price_data][product_data][description]", product_name)
    redirect_url = _first(pairs, "after_completion[redirect][url]")
    after_completion_type = _first(pairs, "after_completion[type]", "hosted_confirmation")
    payment_method_types = _all(pairs, "payment_method_types[]") or ["card"]
    completed_limit_raw = _first(pairs, "restrictions[completed_sessions][limit]", "1")
    try:
        completed_limit = max(1, int(completed_limit_raw))
    except ValueError:
        completed_limit = 1

    payment_link_id = _id("plink")
    token = secrets.token_urlsafe(24)
    link = {
        "id": payment_link_id,
        "object": "payment_link",
        "active": True,
        "after_completion": (
            {"type": "redirect", "redirect": {"url": redirect_url}}
            if redirect_url and after_completion_type == "redirect"
            else {"type": "hosted_confirmation", "hosted_confirmation": {"custom_message": None}}
        ),
        "allow_promotion_codes": False,
        "created": _unix(),
        "currency": currency,
        "livemode": STRIPE_MOCK_LIVEMODE,
        "metadata": metadata,
        "payment_intent_data": {
            "capture_method": capture_method,
            "metadata": payment_intent_metadata,
        },
        "payment_method_types": payment_method_types,
        "restrictions": {"completed_sessions": {"limit": completed_limit}},
        "url": f"{_public_url(request)}/mock-stripe/payment-link/{token}",
        "amount": amount,
        "description": description,
        "product_name": product_name,
        "_token": token,
        "_completed_sessions": 0,
    }
    storage.save_payment_link(link)
    return _response(_payment_link_response(link), status_code=200)


@app.get("/v1/payment_links/{payment_link_id}", tags=["Stripe"])
async def stripe_retrieve_payment_link(
    payment_link_id: str,
    authorization: str | None = Header(None),
):
    _require_auth(authorization)
    link = storage.get_payment_link(payment_link_id)
    if not link:
        return _error(
            f"No such payment_link: '{payment_link_id}'",
            status_code=404,
            code="resource_missing",
            param="payment_link",
        )
    return _response(_payment_link_response(link))


@app.post("/v1/payment_links/{payment_link_id}", tags=["Stripe"])
async def stripe_update_payment_link(
    payment_link_id: str,
    request: Request,
    authorization: str | None = Header(None),
):
    _require_auth(authorization)
    pairs = await _form_pairs(request)
    link = storage.get_payment_link(payment_link_id)
    if not link:
        return _error(
            f"No such payment_link: '{payment_link_id}'",
            status_code=404,
            code="resource_missing",
        )

    active_raw = _first(pairs, "active", "")
    if active_raw != "":
        link["active"] = _bool(active_raw, default=link.get("active", True))
    redirect_url = _first(pairs, "after_completion[redirect][url]")
    if redirect_url:
        link["after_completion"] = {"type": "redirect", "redirect": {"url": redirect_url}}
    storage.save_payment_link(link)
    return _response(_payment_link_response(link))


@app.get("/v1/checkout/sessions", tags=["Stripe"])
async def stripe_list_checkout_sessions(
    request: Request,
    authorization: str | None = Header(None),
):
    _require_auth(authorization)
    payment_link_id = request.query_params.get("payment_link")
    payment_intent_id = request.query_params.get("payment_intent")
    try:
        limit = max(1, min(100, int(request.query_params.get("limit", "10"))))
    except ValueError:
        limit = 10
    sessions = storage.list_checkout_sessions(
        payment_link_id=payment_link_id,
        payment_intent_id=payment_intent_id,
        limit=limit,
    )
    return _response({"object": "list", "data": [_session_response(item) for item in sessions], "has_more": False})


@app.get("/v1/payment_intents/{payment_intent_id}", tags=["Stripe"])
async def stripe_retrieve_payment_intent(
    payment_intent_id: str,
    authorization: str | None = Header(None),
):
    _require_auth(authorization)
    intent = storage.get_payment_intent(payment_intent_id)
    if not intent:
        return _error(
            f"No such payment_intent: '{payment_intent_id}'",
            status_code=404,
            code="resource_missing",
            param="intent",
        )
    return _response(_payment_intent_response(intent))


@app.post("/v1/payment_intents/{payment_intent_id}/capture", tags=["Stripe"])
async def stripe_capture_payment_intent(
    payment_intent_id: str,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(None),
):
    _require_auth(authorization)
    intent = storage.get_payment_intent(payment_intent_id)
    if not intent:
        return _error(
            f"No such payment_intent: '{payment_intent_id}'",
            status_code=404,
            code="resource_missing",
        )
    if intent.get("status") != "requires_capture":
        return _error(
            "This PaymentIntent could not be captured because it is not in requires_capture status.",
            code="payment_intent_unexpected_state",
        )

    intent.update(
        status="succeeded",
        amount_received=int(intent["amount"]),
        amount_capturable=0,
        latest_charge=_id("ch"),
        last_payment_error=None,
    )
    storage.save_payment_intent(intent)
    background_tasks.add_task(_deliver_event, _new_event("payment_intent.succeeded", intent))
    return _response(_payment_intent_response(intent))


@app.post("/v1/payment_intents/{payment_intent_id}/cancel", tags=["Stripe"])
async def stripe_cancel_payment_intent(
    payment_intent_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(None),
):
    _require_auth(authorization)
    intent = storage.get_payment_intent(payment_intent_id)
    if not intent:
        return _error(
            f"No such payment_intent: '{payment_intent_id}'",
            status_code=404,
            code="resource_missing",
        )
    if intent.get("status") not in {
        "requires_payment_method",
        "requires_capture",
        "requires_confirmation",
        "requires_action",
        "processing",
    }:
        return _error(
            "This PaymentIntent could not be canceled because it has already reached a terminal state.",
            code="payment_intent_unexpected_state",
        )
    pairs = await _form_pairs(request)
    cancellation_reason = _first(pairs, "cancellation_reason", "requested_by_customer")
    intent.update(
        status="canceled",
        amount_capturable=0,
        canceled_at=_unix(),
        cancellation_reason=cancellation_reason,
        next_action=None,
    )
    storage.save_payment_intent(intent)
    background_tasks.add_task(_deliver_event, _new_event("payment_intent.canceled", intent))
    return _response(_payment_intent_response(intent))


@app.post("/v1/webhook_endpoints", tags=["Stripe"])
async def stripe_create_webhook_endpoint(
    request: Request,
    authorization: str | None = Header(None),
):
    _require_auth(authorization)
    pairs = await _form_pairs(request)
    url = _first(pairs, "url")
    enabled_events = _all(pairs, "enabled_events[]")
    if not url or not enabled_events:
        return _error("url and enabled_events are required", param="url")

    endpoint_id = _id("we")
    endpoint = {
        "id": endpoint_id,
        "object": "webhook_endpoint",
        "api_version": STRIPE_MOCK_API_VERSION,
        "application": None,
        "created": _unix(),
        "description": _first(pairs, "description") or None,
        "enabled_events": enabled_events,
        "livemode": STRIPE_MOCK_LIVEMODE,
        "metadata": _metadata(pairs, "metadata"),
        "secret": "whsec_" + secrets.token_urlsafe(30),
        "status": "enabled",
        "disabled": False,
        "url": url,
    }
    storage.save_webhook_endpoint(endpoint)
    return _response(_webhook_endpoint_response(endpoint, include_secret=True), status_code=200)


@app.get("/v1/webhook_endpoints/{webhook_endpoint_id}", tags=["Stripe"])
async def stripe_retrieve_webhook_endpoint(
    webhook_endpoint_id: str,
    authorization: str | None = Header(None),
):
    _require_auth(authorization)
    endpoint = storage.get_webhook_endpoint(webhook_endpoint_id)
    if not endpoint:
        return _error(
            f"No such webhook_endpoint: '{webhook_endpoint_id}'",
            status_code=404,
            code="resource_missing",
        )
    return _response(_webhook_endpoint_response(endpoint))


@app.post("/v1/webhook_endpoints/{webhook_endpoint_id}", tags=["Stripe"])
async def stripe_update_webhook_endpoint(
    webhook_endpoint_id: str,
    request: Request,
    authorization: str | None = Header(None),
):
    _require_auth(authorization)
    endpoint = storage.get_webhook_endpoint(webhook_endpoint_id)
    if not endpoint:
        return _error(
            f"No such webhook_endpoint: '{webhook_endpoint_id}'",
            status_code=404,
            code="resource_missing",
        )
    pairs = await _form_pairs(request)
    if url := _first(pairs, "url"):
        endpoint["url"] = url
    events = _all(pairs, "enabled_events[]")
    if events:
        endpoint["enabled_events"] = events
    if any(key == "description" for key, _ in pairs):
        endpoint["description"] = _first(pairs, "description") or None
    if any(key == "disabled" for key, _ in pairs):
        endpoint["disabled"] = _bool(_first(pairs, "disabled"))
        endpoint["status"] = "disabled" if endpoint["disabled"] else "enabled"
    storage.save_webhook_endpoint(endpoint)
    return _response(_webhook_endpoint_response(endpoint))


@app.delete("/v1/webhook_endpoints/{webhook_endpoint_id}", tags=["Stripe"])
async def stripe_delete_webhook_endpoint(
    webhook_endpoint_id: str,
    authorization: str | None = Header(None),
):
    _require_auth(authorization)
    if not storage.delete_webhook_endpoint(webhook_endpoint_id):
        return _error(
            f"No such webhook_endpoint: '{webhook_endpoint_id}'",
            status_code=404,
            code="resource_missing",
        )
    return Response(status_code=204, headers={"Request-Id": _request_id()})


def _checkout_html(link: dict[str, Any], session: dict[str, Any]) -> str:
    amount = int(link["amount"])
    currency = str(link["currency"]).upper()
    capture_method = (link.get("payment_intent_data") or {}).get("capture_method", "automatic")
    amount_text = f"{amount:,} {currency}" if currency in {"CLP", "JPY", "KRW"} else f"{amount / 100:.2f} {currency}"
    success_label = "Autorizar fondos" if capture_method == "manual" else "Pago correcto"
    success_help = (
        "El PaymentIntent quedará en requires_capture; luego captura o cancela desde crinnopayments."
        if capture_method == "manual"
        else "El PaymentIntent quedará en succeeded."
    )
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Stripe Mock Checkout</title>
  <style>
    * {{ box-sizing:border-box; }}
    body {{ margin:0; padding:32px 18px; font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif; background:#f6f9fc; color:#1f2937; }}
    main {{ max-width:680px; margin:auto; background:white; border-radius:18px; box-shadow:0 14px 45px rgba(50,50,93,.14); overflow:hidden; }}
    header {{ background:#635bff; color:#fff; padding:22px 28px; font-size:21px; font-weight:800; }}
    section {{ padding:28px; }}
    h1 {{ margin:0 0 8px; }}
    .amount {{ font-size:42px; font-weight:850; margin:24px 0; color:#111827; }}
    .badge {{ display:inline-block; border-radius:999px; padding:5px 10px; background:#ede9fe; color:#5b21b6; font-weight:700; }}
    .meta {{ display:grid; gap:10px; margin:22px 0; }}
    .row {{ background:#f8fafc; padding:11px 13px; border-radius:10px; overflow-wrap:anywhere; }}
    .row b {{ display:block; color:#64748b; text-transform:uppercase; font-size:11px; margin-bottom:3px; }}
    .actions {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
    button {{ border:0; border-radius:11px; padding:13px 14px; cursor:pointer; font-weight:800; }}
    .ok {{ background:#16a34a; color:white; }}
    .fail {{ background:#dc2626; color:white; }}
    .action {{ background:#f59e0b; color:#3f2a00; }}
    .processing {{ background:#0ea5e9; color:white; }}
    .expire {{ background:#e5e7eb; color:#374151; }}
    pre {{ white-space:pre-wrap; overflow-wrap:anywhere; background:#111827; color:#d1fae5; padding:14px; border-radius:10px; min-height:70px; }}
    .hint {{ color:#64748b; line-height:1.45; }}
    @media (max-width:600px) {{ .actions {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body><main>
  <header>Stripe · Checkout de prueba</header>
  <section>
    <h1>{html.escape(link.get('product_name') or 'Solicitud de pago')}</h1>
    <span class="badge">capture_method={html.escape(capture_method)}</span>
    <div class="amount">{html.escape(amount_text)}</div>
    <p>{html.escape(link.get('description') or '')}</p>
    <p class="hint">{html.escape(success_help)}</p>
    <div class="meta">
      <div class="row"><b>Payment Link</b><code>{html.escape(link['id'])}</code></div>
      <div class="row"><b>Checkout Session</b><code>{html.escape(session['id'])}</code></div>
      <div class="row"><b>Order</b>{html.escape(str((link.get('metadata') or {}).get('order_number') or '-'))}</div>
      <div class="row"><b>Return URL</b>{html.escape(str((link.get('after_completion') or {}).get('redirect', {}).get('url') or 'Hosted confirmation'))}</div>
    </div>
    <div class="actions">
      <button class="ok" onclick="simulate('success')">{html.escape(success_label)}</button>
      <button class="fail" onclick="simulate('failure')">Pago fallido</button>
      <button class="action" onclick="simulate('requires_action')">Requiere acción</button>
      <button class="processing" onclick="simulate('processing')">Processing</button>
      <button class="expire" onclick="simulate('expired')">Expirar esta sesión</button>
    </div>
    <p><label><input id="redirect" type="checkbox" checked> Redirigir al frontend después de éxito si existe redirect_url</label></p>
    <pre id="result">Esperando una acción…</pre>
  </section>
</main>
<script>
async function simulate(outcome) {{
  const box = document.getElementById('result');
  box.textContent = 'Procesando ' + outcome + '...';
  const response = await fetch('/mock-stripe/checkout/{html.escape(session['id'])}/simulate', {{
    method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{outcome}})
  }});
  const data = await response.json();
  box.textContent = JSON.stringify(data, null, 2);
  if (response.ok && outcome === 'success' && document.getElementById('redirect').checked && data.redirect_url) {{
    window.setTimeout(() => window.location.assign(data.redirect_url), 700);
  }}
}}
</script></body></html>"""


@app.get("/mock-stripe/payment-link/{token}", response_class=HTMLResponse, include_in_schema=False)
async def stripe_mock_payment_link(token: str, request: Request):
    link = storage.get_payment_link_by_token(token)
    if not link:
        return HTMLResponse("<h1>Payment Link no encontrado</h1>", status_code=404)
    if not link.get("active", True):
        return HTMLResponse(
            "<html><body style='font-family:system-ui;padding:40px'><h1>Este Payment Link está desactivado</h1>"
            "<p>El mock está reproduciendo <code>active=false</code>.</p></body></html>",
            status_code=200,
        )
    if int(link.get("_completed_sessions", 0)) >= int(
        (link.get("restrictions") or {}).get("completed_sessions", {}).get("limit", 1)
    ):
        return HTMLResponse(
            "<html><body style='font-family:system-ui;padding:40px'><h1>Este Payment Link ya fue utilizado</h1></body></html>",
            status_code=200,
        )
    session = _current_or_new_session(link, request)
    return HTMLResponse(_checkout_html(link, session))


@app.get("/mock-stripe/checkout/{session_id}", response_class=HTMLResponse, include_in_schema=False)
async def stripe_mock_checkout(session_id: str):
    session = _get_session(session_id)
    link = _get_link(session["payment_link"])
    return HTMLResponse(_checkout_html(link, session))


@app.post("/mock-stripe/checkout/{session_id}/simulate", tags=["Stripe mock controls"])
async def stripe_simulate_checkout(
    session_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    session = _get_session(session_id)
    link = _get_link(session["payment_link"])
    if not link.get("active", True):
        return _error("This payment link is inactive.", status_code=409, code="payment_link_inactive")

    try:
        body = await request.json()
    except ValueError:
        body = {}
    outcome = str(body.get("outcome") or "success").lower()
    if outcome not in {"success", "failure", "requires_action", "processing", "expired"}:
        return _error("Unsupported mock outcome", param="outcome")

    if outcome == "expired":
        session.update(status="expired", payment_status="unpaid")
        storage.save_checkout_session(session)
        event = _new_event("checkout.session.expired", session)
        background_tasks.add_task(_deliver_event, event)
        return _response({"outcome": outcome, "session": _session_response(session), "event_ids": [event["id"]]})

    intent = _new_or_current_intent(link, session)
    events: list[dict[str, Any]] = []

    if outcome == "failure":
        intent.update(
            status="requires_payment_method",
            amount_received=0,
            amount_capturable=0,
            next_action=None,
            last_payment_error={
                "code": "card_declined",
                "decline_code": "generic_decline",
                "message": "Your card was declined. Simulated by Stripe mock.",
                "type": "card_error",
            },
        )
        session.update(status="open", payment_status="unpaid")
        events.append(_new_event("payment_intent.payment_failed", intent))
    elif outcome == "requires_action":
        intent.update(
            status="requires_action",
            amount_received=0,
            amount_capturable=0,
            last_payment_error=None,
            next_action={"type": "use_stripe_sdk", "use_stripe_sdk": {"type": "three_d_secure_redirect"}},
        )
        session.update(status="open", payment_status="unpaid")
        events.append(_new_event("payment_intent.requires_action", intent))
    elif outcome == "processing":
        intent.update(
            status="processing",
            amount_received=0,
            amount_capturable=0,
            last_payment_error=None,
            next_action=None,
        )
        session.update(status="open", payment_status="unpaid")
        events.append(_new_event("payment_intent.processing", intent))
    else:
        capture_method = intent.get("capture_method", "automatic")
        if capture_method == "manual":
            intent.update(
                status="requires_capture",
                amount_capturable=int(intent["amount"]),
                amount_received=0,
                latest_charge=_id("ch"),
                last_payment_error=None,
                next_action=None,
            )
            session.update(status="complete", payment_status="unpaid")
            events.append(_new_event("payment_intent.amount_capturable_updated", intent))
        else:
            intent.update(
                status="succeeded",
                amount_capturable=0,
                amount_received=int(intent["amount"]),
                latest_charge=_id("ch"),
                last_payment_error=None,
                next_action=None,
            )
            session.update(status="complete", payment_status="paid")
            events.append(_new_event("payment_intent.succeeded", intent))

        events.append(_new_event("checkout.session.completed", session))

        def increment_completed(current: dict[str, Any]):
            current["_completed_sessions"] = int(current.get("_completed_sessions", 0)) + 1
            return current

        storage.update_payment_link(link["id"], increment_completed)

    storage.save_payment_intent(intent)
    storage.save_checkout_session(session)
    await _queue_events(background_tasks, events)

    redirect_url = None
    if outcome == "success" and (link.get("after_completion") or {}).get("type") == "redirect":
        redirect_url = (link.get("after_completion") or {}).get("redirect", {}).get("url")
        if redirect_url:
            redirect_url = redirect_url.replace("{CHECKOUT_SESSION_ID}", session["id"])

    return _response(
        {
            "outcome": outcome,
            "payment_link": _payment_link_response(storage.get_payment_link(link["id"])),
            "checkout_session": _session_response(session),
            "payment_intent": _payment_intent_response(intent),
            "event_ids": [event["id"] for event in events],
            "redirect_url": redirect_url,
        }
    )


@app.get("/mock-stripe/payment-links", tags=["Stripe mock controls"])
async def stripe_mock_list_payment_links():
    return {"data": [_payment_link_response(item) for item in storage.list_payment_links()]}


@app.get("/mock-stripe/payment-links/{payment_link_id}", tags=["Stripe mock controls"])
async def stripe_mock_get_payment_link(payment_link_id: str):
    return _payment_link_response(_get_link(payment_link_id))


@app.get("/mock-stripe/payment-intents", tags=["Stripe mock controls"])
async def stripe_mock_list_payment_intents():
    return {"data": [_payment_intent_response(item) for item in storage.list_payment_intents()]}


@app.get("/mock-stripe/webhook-endpoints", tags=["Stripe mock controls"])
async def stripe_mock_list_webhook_endpoints():
    return {
        "data": [
            _webhook_endpoint_response(item, include_secret=True)
            for item in storage.list_webhook_endpoints()
        ]
    }


@app.get("/mock-stripe/webhook-deliveries", tags=["Stripe mock controls"])
async def stripe_mock_webhook_deliveries():
    return {"data": storage.list_webhook_deliveries()}


@app.post("/mock-stripe/payment-intents/{payment_intent_id}/webhook", tags=["Stripe mock controls"])
async def stripe_mock_send_payment_intent_webhook(
    payment_intent_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    intent = _get_intent(payment_intent_id)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    event_type = str(body.get("event_type") or "payment_intent.succeeded")
    bad_signature = bool(body.get("bad_signature", False))
    event = _new_event(event_type, intent)
    background_tasks.add_task(_deliver_event, event, bad_signature=bad_signature)
    return _response({"queued": True, "event": event, "bad_signature": bad_signature}, status_code=202)


@app.post("/mock-stripe/reset", tags=["Stripe mock controls"])
async def stripe_mock_reset():
    storage.reset()
    return {"reset": True}
