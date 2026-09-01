"""Mock local de Revolut Merchant API para el servidor FastAPI existente.

Importar este modulo desde ``main.py`` con::

    from revolut.revolut import *  # noqa: F403

El estado vive en memoria y se pierde al reiniciar el servidor.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app import app


REVOLUT_MOCK_SECRET_KEY = os.getenv("REVOLUT_MOCK_SECRET_KEY","fake-secret")
REVOLUT_WEBHOOK_TARGET_URL = os.getenv("REVOLUT_WEBHOOK_TARGET_URL", "http://localhost:8001/api/revolut/webhooks/notifications/")
REVOLUT_WEBHOOK_SIGNING_SECRET = os.getenv("REVOLUT_WEBHOOK_SIGNING_SECRET", "public-key" )

# Se admiten las dos composiciones habituales de merchant_api_url + url_path.
API_PREFIXES = ("/api", "/api/1.0")
EXPIRE_PENDING_AFTER_PATTERN = re.compile(r"^(?:P[1-9]\d*D|PT[1-9]\d*[HM])$")

orders: dict[str, dict[str, Any]] = {}
order_ids_by_token: dict[str, str] = {}
webhooks: dict[str, dict[str, Any]] = {}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _require_authorization(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    if REVOLUT_MOCK_SECRET_KEY and authorization != f"Bearer {REVOLUT_MOCK_SECRET_KEY}":
        print(f"REVOLUT_MOCK_SECRET_KEY {REVOLUT_MOCK_SECRET_KEY} !!!!!!!")
        print(f"authorization {authorization} !!!!!!!!!")
        raise HTTPException(status_code=401, detail="Invalid API secret key")


def _validate_expire_pending_after(body: dict[str, Any]) -> str | None:
    """Valida la duracion opcional aceptada por la API de ordenes de Revolut."""
    if "expire_pending_after" not in body:
        return None

    value = body["expire_pending_after"]
    if not isinstance(value, str) or not EXPIRE_PENDING_AFTER_PATTERN.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail=(
                "expire_pending_after must be a non-empty positive ISO 8601 "
                "duration in minutes, hours, or days"
            ),
        )
    return value


def _public_order(order: dict[str, Any]) -> dict[str, Any]:
    """Devuelve una copia para evitar exponer campos internos del mock."""
    return {key: value for key, value in order.items() if not key.startswith("_")}


def _find_order(order_id: str) -> dict[str, Any]:
    order = orders.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def _find_order_by_token(token: str) -> dict[str, Any]:
    order_id = order_ids_by_token.get(token)
    if not order_id:
        raise HTTPException(status_code=404, detail="Order not found")
    return _find_order(order_id)


def _webhook_signature(payload: dict[str, Any], timestamp: str, signing_secret: str) -> str:
    # Django reconstruye el cuerpo con json.dumps(...).replace(" ", "").
    compact_payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    value_to_sign = f"v1.{timestamp}.{compact_payload}".encode()
    digest = hmac.new(signing_secret.encode(), value_to_sign, hashlib.sha256).hexdigest()
    return f"v1={digest}"


async def _send_order_event(order: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    payload = {
        "event": event_type,
        "order_id": order["id"],
        "merchant_order_ext_ref": str(order.get("metadata", {}).get("order_number", "")),
    }
    targets = [
        item
        for item in webhooks.values()
        if event_type in item.get("events", []) and item.get("url")
    ]

    # Permite probar aunque el webhook de Django ya existiera antes de iniciar el mock.
    if not targets and REVOLUT_WEBHOOK_TARGET_URL:
        targets = [
            {
                "id": "environment-webhook",
                "url": REVOLUT_WEBHOOK_TARGET_URL,
                "signing_secret": REVOLUT_WEBHOOK_SIGNING_SECRET,
            }
        ]

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for webhook in targets:
            timestamp = str(int(datetime.now(UTC).timestamp() * 1000))
            raw_body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
            headers = {
                "Content-Type": "application/json",
                "Revolut-Request-Timestamp": timestamp,
                "Revolut-Signature": _webhook_signature(
                    payload,
                    timestamp,
                    webhook.get("signing_secret") or REVOLUT_WEBHOOK_SIGNING_SECRET,
                ),
                "User-Agent": "mock-revolut/1.0",
            }
            result = {"webhook_id": webhook["id"], "target": webhook["url"], "sent": False}
            try:
                response = await client.post(webhook["url"], content=raw_body, headers=headers)
                result.update(
                    sent=True,
                    status_code=response.status_code,
                    response_text=response.text,
                )
            except httpx.HTTPError as exc:
                result["error"] = str(exc)
            results.append(result)
    return results


async def create_order(request: Request, authorization: str | None = Header(None)):
    _require_authorization(authorization)
    body = await request.json()
    try:
        amount = int(body["amount"])
        currency = str(body["currency"]).upper()
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="amount and currency are required") from exc
    if amount < 1:
        raise HTTPException(status_code=400, detail="amount must be greater than zero")
    expire_pending_after = _validate_expire_pending_after(body)

    order_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(24)[:36]
    checkout_url = f"{str(request.base_url).rstrip('/')}/mock-revolut/checkout/{token}"
    now = _utc_now()
    order = {
        "id": order_id,
        "token": token,
        "type": "PAYMENT",
        "state": "PENDING",
        "capture_mode": str(body.get("capture_mode", "automatic")).upper(),
        "enforce_challenge": str(body.get("enforce_challenge", "automatic")).upper(),
        "amount": amount,
        "outstanding_amount": amount,
        "refunded_amount": 0,
        "currency": currency,
        "settlement_currency": str(body.get("settlement_currency") or currency).upper(),
        "description": body.get("description", ""),
        "metadata": body.get("metadata") or {},
        "checkout_url": checkout_url,
        "created_at": now,
        "updated_at": now,
        "payments": [],
    }
    if expire_pending_after is not None:
        order["_expire_pending_after"] = expire_pending_after
    orders[order_id] = order
    order_ids_by_token[token] = order_id
    return JSONResponse(status_code=201, content=_public_order(order))


async def retrieve_order(order_id: str, authorization: str | None = Header(None)):
    _require_authorization(authorization)
    return _public_order(_find_order(order_id))


async def capture_order(order_id: str, authorization: str | None = Header(None)):
    _require_authorization(authorization)
    order = _find_order(order_id)
    order.update(state="COMPLETED", outstanding_amount=0, updated_at=_utc_now())
    order["payments"].append(
        {
            "id": str(uuid.uuid4()),
            "state": "COMPLETED",
            "amount": order["amount"],
            "created_at": order["updated_at"],
        }
    )
    webhook_results = await _send_order_event(order, "ORDER_COMPLETED")
    response = _public_order(order)
    response["mock_webhook_results"] = webhook_results
    return response


async def cancel_order(order_id: str, authorization: str | None = Header(None)):
    _require_authorization(authorization)
    order = _find_order(order_id)
    order.update(state="CANCELLED", updated_at=_utc_now())
    return _public_order(order)


async def refund_order(order_id: str, request: Request, authorization: str | None = Header(None)):
    _require_authorization(authorization)
    order = _find_order(order_id)
    body = await request.json()
    raw_amount = body.get("amount", order["amount"])
    if isinstance(raw_amount, dict):
        raw_amount = raw_amount.get("value", order["amount"])
    try:
        amount = int(raw_amount)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid refund amount") from exc
    order["refunded_amount"] = min(order["amount"], order["refunded_amount"] + amount)
    order["updated_at"] = _utc_now()
    return {
        "id": str(uuid.uuid4()),
        "state": "COMPLETED",
        "amount": amount,
        "currency": order["currency"],
        "order_id": order_id,
        "created_at": order["updated_at"],
    }


async def create_webhook(request: Request, authorization: str | None = Header(None)):
    _require_authorization(authorization)
    body = await request.json()
    if not body.get("url") or not body.get("events"):
        raise HTTPException(status_code=400, detail="url and events are required")
    webhook_id = str(uuid.uuid4())
    webhook = {
        "id": webhook_id,
        "url": body["url"],
        "events": body["events"],
        "signing_secret": secrets.token_urlsafe(32),
    }
    webhooks[webhook_id] = webhook
    return JSONResponse(status_code=201, content=webhook)


async def list_webhooks(authorization: str | None = Header(None)):
    _require_authorization(authorization)
    return {"webhooks": list(webhooks.values())}


async def retrieve_webhook(webhook_id: str, authorization: str | None = Header(None)):
    _require_authorization(authorization)
    webhook = webhooks.get(webhook_id)
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return webhook


async def update_webhook(
    webhook_id: str,
    request: Request,
    authorization: str | None = Header(None),
):
    _require_authorization(authorization)
    webhook = webhooks.get(webhook_id)
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    body = await request.json()
    webhook.update({key: body[key] for key in ("url", "events") if key in body})
    return webhook


async def delete_webhook(webhook_id: str, authorization: str | None = Header(None)):
    _require_authorization(authorization)
    if webhooks.pop(webhook_id, None) is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return Response(status_code=204)


async def rotate_webhook_secret(webhook_id: str, authorization: str | None = Header(None)):
    _require_authorization(authorization)
    webhook = webhooks.get(webhook_id)
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    webhook["signing_secret"] = secrets.token_urlsafe(32)
    return {"signing_secret": webhook["signing_secret"]}


@app.get("/mock-revolut/checkout/{token}", response_class=HTMLResponse)
async def revolut_checkout(token: str):
    order = _find_order_by_token(token)
    amount = order["amount"] / 100
    return HTMLResponse(
        f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Mock Revolut Checkout</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background:#f5f5f7; margin:0; padding:32px; }}
    main {{ max-width:520px; margin:auto; background:white; padding:32px; border-radius:18px; box-shadow:0 8px 30px #0002; }}
    button {{ border:0; border-radius:10px; padding:13px 18px; margin:5px; cursor:pointer; font-weight:700; }}
    .success {{ background:#111; color:white; }} .failure {{ background:#fee2e2; color:#991b1b; }}
    pre {{ white-space:pre-wrap; background:#f4f4f5; padding:12px; border-radius:8px; }}
  </style>
</head>
<body><main>
  <h1>Revolut Mock</h1>
  <p>{html.escape(order.get('description') or 'Solicitud de pago')}</p>
  <h2>{amount:.2f} {html.escape(order['currency'])}</h2>
  <button class="success" onclick="pay('success')">Simular pago exitoso</button>
  <button class="failure" onclick="pay('failure')">Simular pago fallido</button>
  <pre id="result">Esperando una acción…</pre>
  <script>
    async function pay(outcome) {{
      const response = await fetch(location.pathname + '/simulate', {{
        method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{outcome}})
      }});
      document.getElementById('result').textContent = JSON.stringify(await response.json(), null, 2);
    }}
  </script>
</main></body></html>"""
    )


@app.post("/mock-revolut/checkout/{token}/simulate")
async def simulate_revolut_checkout(token: str, request: Request):
    order = _find_order_by_token(token)
    body = await request.json()
    outcome = str(body.get("outcome", "success")).lower()
    event_by_outcome = {
        "success": ("COMPLETED", "ORDER_COMPLETED"),
        "failure": ("FAILED", "ORDER_PAYMENT_FAILED"),
        "declined": ("DECLINED", "ORDER_PAYMENT_DECLINED"),
        "authorised": ("AUTHORISED", "ORDER_AUTHORISED"),
    }
    if outcome not in event_by_outcome:
        raise HTTPException(status_code=400, detail=f"Unsupported outcome: {outcome}")
    state, event_type = event_by_outcome[outcome]
    now = _utc_now()
    order.update(
        state=state,
        outstanding_amount=0 if state == "COMPLETED" else order["amount"],
        updated_at=now,
    )
    order["payments"].append(
        {"id": str(uuid.uuid4()), "state": state, "amount": order["amount"], "created_at": now}
    )
    results = await _send_order_event(order, event_type)
    return {"order": _public_order(order), "webhook_results": results}


# Registro explícito de alias. Todos ejecutan los mismos handlers y contrato.
for prefix in API_PREFIXES:
    app.add_api_route(f"{prefix}/orders", create_order, methods=["POST"], include_in_schema=prefix == "/api")
    app.add_api_route(
        f"{prefix}/orders/{{order_id}}",
        retrieve_order,
        methods=["GET"],
        include_in_schema=prefix == "/api",
    )
    app.add_api_route(f"{prefix}/orders/{{order_id}}/capture", capture_order, methods=["POST"])
    app.add_api_route(f"{prefix}/orders/{{order_id}}/cancel", cancel_order, methods=["POST"])
    app.add_api_route(f"{prefix}/orders/{{order_id}}/refund", refund_order, methods=["POST"])
    app.add_api_route(f"{prefix}/webhooks", create_webhook, methods=["POST"])
    app.add_api_route(f"{prefix}/webhooks", list_webhooks, methods=["GET"])
    app.add_api_route(f"{prefix}/webhooks/{{webhook_id}}", retrieve_webhook, methods=["GET"])
    app.add_api_route(f"{prefix}/webhooks/{{webhook_id}}", update_webhook, methods=["PATCH", "PUT"])
    app.add_api_route(f"{prefix}/webhooks/{{webhook_id}}", delete_webhook, methods=["DELETE"])
    app.add_api_route(
        f"{prefix}/webhooks/{{webhook_id}}/rotate-signing-secret",
        rotate_webhook_secret,
        methods=["POST"],
    )
