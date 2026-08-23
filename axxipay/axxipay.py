"""Mock local de Axxipay Checkout para el servidor FastAPI existente.

Copiar esta carpeta al proyecto del mock e importar el modulo desde ``main.py``::

    from axxipay.axxipay import *  # noqa: F403

El estado se guarda en memoria y se pierde al reiniciar el servidor.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import app


AXXIPAY_MERCHANT_KEY = os.getenv("AXXIPAY_MERCHANT_KEY", "merchant-key")
AXXIPAY_MERCHANT_PASSWORD = os.getenv("AXXIPAY_MERCHANT_PASSWORD", "merchant-pass")
AXXIPAY_WEBHOOK_TARGET_URL = os.getenv(
    "AXXIPAY_WEBHOOK_TARGET_URL",
    "http://localhost:8001/api/axxipay/webhooks/notifications/",
)
AXXIPAY_VALIDATE_REQUEST_HASH = os.getenv(
    "AXXIPAY_VALIDATE_REQUEST_HASH", "false"
).lower() in {"1", "true", "yes"}

payments: dict[str, dict[str, Any]] = {}
payment_ids_by_token: dict[str, str] = {}
webhook_deliveries: list[dict[str, Any]] = []


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _string_amount(value: Any) -> str:
    try:
        return f"{Decimal(str(value)):.2f}"
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="order.amount must be a valid amount") from exc


def _payment_hash(order: dict[str, Any]) -> str:
    """Firma de creación usada por PaymentHashGenerator en Django."""
    value = "".join(
        str(order.get(field, ""))
        for field in ("number", "amount", "currency", "description")
    )
    value += AXXIPAY_MERCHANT_PASSWORD
    md5_digest = hashlib.md5(value.upper().encode()).hexdigest()  # noqa: S324
    return hashlib.sha1(md5_digest.encode()).hexdigest()  # noqa: S324


def _callback_hash(payload: dict[str, Any]) -> str:
    """Firma exacta usada por CallbackHashGenerator en Django."""
    value = "".join(
        str(payload.get(field, ""))
        for field in (
            "id",
            "order_number",
            "order_amount",
            "order_currency",
            "order_description",
        )
    )
    value += AXXIPAY_MERCHANT_PASSWORD
    md5_digest = hashlib.md5(value.upper().encode()).hexdigest()  # noqa: S324
    return hashlib.sha1(md5_digest.encode()).hexdigest()  # noqa: S324


def _find_payment(payment_id: str) -> dict[str, Any]:
    payment = payments.get(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


def _find_payment_by_token(token: str) -> dict[str, Any]:
    payment_id = payment_ids_by_token.get(token)
    if not payment_id:
        raise HTTPException(status_code=404, detail="Payment not found")
    return _find_payment(payment_id)


def _error_response(message: str, field: str | None = None) -> JSONResponse:
    error = {"error_code": 100000, "error_message": message}
    if field:
        error["field"] = field
    return JSONResponse(
        status_code=400,
        content={
            "error_code": 0,
            "error_message": "Request data is invalid.",
            "errors": [error],
        },
    )


def _build_callback(payment: dict[str, Any], outcome: str) -> dict[str, Any]:
    order = payment["order"]
    outcome_values = {
        "success": ("success", "settled", ""),
        "failure": ("fail", "decline", "205005"),
        "declined": ("fail", "decline", "205005"),
        "cancelled": ("fail", "void", "Cancelled by customer"),
    }
    if outcome not in outcome_values:
        raise HTTPException(status_code=400, detail=f"Unsupported outcome: {outcome}")

    status, order_status, reason = outcome_values[outcome]
    payload: dict[str, Any] = {
        "id": payment["id"],
        "type": "sale",
        "date": _utc_now(),
        "status": status,
        "order_status": order_status,
        "order_number": order["number"],
        "order_amount": order["amount"],
        "order_currency": order["currency"],
        "order_description": order.get("description", ""),
        "reason": reason,
        "rrn": secrets.token_hex(6).upper() if outcome == "success" else "",
        "approval_code": secrets.token_hex(3).upper() if outcome == "success" else "",
        "card": "411111******1111",
        "card_expiration_date": "12/2030",
        "customer_ip": "127.0.0.1",
        "exchange_rate": "1.00",
        "exchange_rate_base": "1.00",
        "exchange_currency": order["currency"],
        "exchange_amount": order["amount"],
        "vat_amount": "0.00",
    }

    customer = payment.get("customer") or {}
    billing = payment.get("billing_address") or {}
    payload.update(
        {
            "customer_name": customer.get("name", ""),
            "customer_email": customer.get("email", ""),
            "customer_country": billing.get("country", ""),
            "customer_state": billing.get("state", ""),
            "customer_city": billing.get("city", ""),
            "customer_address": billing.get("address", ""),
        }
    )
    if payment.get("req_token"):
        payload["card_token"] = f"mock-card-{payment['id'][:12]}"

    payload["hash"] = _callback_hash(payload)
    return payload


async def _send_callback(
    payment: dict[str, Any],
    outcome: str,
    target_url: str | None = None,
) -> dict[str, Any]:
    payload = _build_callback(payment, outcome)
    target = target_url or AXXIPAY_WEBHOOK_TARGET_URL
    delivery: dict[str, Any] = {
        "payment_id": payment["id"],
        "order_number": payment["order"]["number"],
        "outcome": outcome,
        "target": target,
        "payload": payload,
        "sent": False,
        "created_at": _utc_now(),
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                target,
                json=payload,
                headers={"User-Agent": "mock-axxipay/1.0"},
            )
        delivery.update(
            sent=response.is_success,
            status_code=response.status_code,
            response_text=response.text,
        )
    except httpx.HTTPError as exc:
        delivery["error"] = str(exc)

    webhook_deliveries.append(delivery)
    payment["last_callback"] = payload
    payment["last_delivery"] = delivery
    payment["status"] = payload["status"]
    payment["order_status"] = payload["order_status"]
    payment["updated_at"] = _utc_now()
    return delivery


@app.post("/api/v1/session", tags=["Axxipay"])
async def create_axxipay_session(request: Request):
    """Crea la sesión solicitada por ``AxxipayPaymentService.create_payment``."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error_response("The request body must be valid JSON.")

    order = body.get("order")
    if not isinstance(order, dict):
        return _error_response("order: This field is required.", "order")
    for field in ("number", "amount", "currency", "description"):
        if field not in order:
            return _error_response(f"order.{field}: This field is required.", f"order.{field}")

    order = order.copy()
    order["number"] = str(order["number"])
    order["amount"] = _string_amount(order["amount"])
    order["currency"] = str(order["currency"]).upper()
    order["description"] = str(order.get("description", ""))

    if AXXIPAY_MERCHANT_KEY and body.get("merchant_key") != AXXIPAY_MERCHANT_KEY:
        return _error_response("merchant_key: Merchant key is not valid.", "merchant_key")
    if AXXIPAY_VALIDATE_REQUEST_HASH and body.get("hash") != _payment_hash(order):
        return _error_response("hash: Hash is not valid.", "hash")

    payment_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(24)
    redirect_url = f"{str(request.base_url).rstrip('/')}/mock-axxipay/checkout/{token}"
    now = _utc_now()
    payment = {
        **body,
        "id": payment_id,
        "order": order,
        "redirect_url": redirect_url,
        "status": "waiting",
        "order_status": "prepare",
        "created_at": now,
        "updated_at": now,
    }
    payments[payment_id] = payment
    payment_ids_by_token[token] = payment_id
    return JSONResponse(
        status_code=201,
        content={
            "id": payment_id,
            "redirect_url": redirect_url,
            "status": "waiting",
            "order": order,
        },
    )


@app.get("/mock-axxipay/checkout/{token}", response_class=HTMLResponse, include_in_schema=False)
async def axxipay_checkout(token: str):
    payment = _find_payment_by_token(token)
    order = payment["order"]
    return HTMLResponse(
        f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Mock Axxipay Checkout</title>
  <style>
    body {{font-family:system-ui,sans-serif;background:#f4f6f8;margin:0;padding:32px}}
    main {{max-width:580px;margin:auto;background:white;padding:32px;border-radius:18px;box-shadow:0 8px 30px #0002}}
    button {{border:0;border-radius:10px;padding:13px 18px;margin:5px;cursor:pointer;font-weight:700}}
    .success {{background:#16794b;color:white}} .failure {{background:#fee2e2;color:#991b1b}}
    .cancel {{background:#e5e7eb;color:#27272a}} pre {{white-space:pre-wrap;background:#f4f4f5;padding:12px;border-radius:8px}}
  </style>
</head>
<body><main>
  <h1>Axxipay Mock</h1>
  <p>Orden <strong>{html.escape(order['number'])}</strong></p>
  <p>{html.escape(order.get('description', 'Solicitud de pago'))}</p>
  <h2>{html.escape(order['amount'])} {html.escape(order['currency'])}</h2>
  <button class="success" onclick="finish('success')">Pago exitoso</button>
  <button class="failure" onclick="finish('declined')">Pago rechazado</button>
  <button class="cancel" onclick="finish('cancelled')">Cancelar pago</button>
  <p><label><input id="redirect" type="checkbox" checked> Redirigir al finalizar, como Axxipay real</label></p>
  <pre id="result">Esperando una acción…</pre>
  <script>
    async function finish(outcome) {{
      const response = await fetch(location.pathname + '/simulate', {{
        method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify({{outcome}})
      }});
      const data = await response.json();
      document.getElementById('result').textContent = JSON.stringify(data, null, 2);
      if (document.getElementById('redirect').checked && data.redirect_url) {{
        window.setTimeout(() => window.location.assign(data.redirect_url), 700);
      }}
    }}
  </script>
</main></body></html>"""
    )


@app.post("/mock-axxipay/checkout/{token}/simulate", tags=["Axxipay mock controls"])
async def simulate_axxipay_checkout(token: str, request: Request):
    payment = _find_payment_by_token(token)
    body = await request.json()
    outcome = str(body.get("outcome", "success")).lower()
    delivery = await _send_callback(payment, outcome)
    redirect_url = (
        payment.get("success_url")
        if outcome == "success"
        else payment.get("cancel_url")
    )
    return {
        "payment_id": payment["id"],
        "status": payment["status"],
        "order_status": payment["order_status"],
        "redirect_url": redirect_url,
        "webhook_delivery": delivery,
    }


@app.get("/mock-axxipay/payments", tags=["Axxipay mock controls"])
async def list_axxipay_payments():
    return {"payments": list(payments.values())}


@app.get("/mock-axxipay/payments/{payment_id}", tags=["Axxipay mock controls"])
async def get_axxipay_payment(payment_id: str):
    return _find_payment(payment_id)


@app.post("/mock-axxipay/payments/{payment_id}/webhook", tags=["Axxipay mock controls"])
async def send_axxipay_webhook(payment_id: str, request: Request):
    payment = _find_payment(payment_id)
    body = await request.json()
    delivery = await _send_callback(
        payment,
        str(body.get("outcome", "success")).lower(),
        body.get("target_url"),
    )
    return {"webhook_delivery": delivery}


@app.get("/mock-axxipay/webhook-deliveries", tags=["Axxipay mock controls"])
async def list_axxipay_webhook_deliveries():
    return {"deliveries": webhook_deliveries}


@app.post("/mock-axxipay/reset", tags=["Axxipay mock controls"])
async def reset_axxipay_mock():
    payments.clear()
    payment_ids_by_token.clear()
    webhook_deliveries.clear()
    return {"reset": True}
