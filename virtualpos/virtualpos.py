"""Mock local del flujo Web Checkout de VirtualPOS v3.

Implementa el recorrido usado por la integración:

* crear un Payment;
* generar su enlace Web Checkout;
* aprobar o rechazar el pago desde una página web;
* notificar el ``uuid`` al ``callback_url``;
* recuperar el Payment actualizado.

El estado y los escenarios se conservan en SQLite entre reinicios.
"""

from __future__ import annotations

import base64
import html
import os
import uuid as uuid_module
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

import httpx
from fastapi import Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import app
from mock_storage import ResourceStore
from mock_controls import virtualpos_error


# El proyecto carga cada proveedor con ``import *`` únicamente para registrar
# rutas. Evita contaminar el namespace de main.py con helpers de este módulo.
__all__ = []


PAYMENTS = ResourceStore("virtualpos", "payments")
WEBHOOK_DELIVERIES = []
ALLOWED_PAYMENT_METHODS = {"webpay", "khipu", "fintoc", "mach", "redpay", "all"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _decode_url(value: Optional[str]) -> Optional[str]:
    """Decodifica una URL Base64 de VirtualPOS; acepta texto plano para desarrollo."""
    if not value:
        return None
    value = str(value).strip()
    if value.startswith(("http://", "https://")):
        return value
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, validate=False).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if decoded.startswith(("http://", "https://")) else None


def _request_base_url(request: Request) -> str:
    configured = os.getenv("MOCK_VIRTUALPOS_PUBLIC_URL", "").strip().rstrip("/")
    return configured or str(request.base_url).rstrip("/")


class VirtualposMockError(HTTPException):
    """Expose documented API error envelopes rather than FastAPI's generic detail format."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(status_code=status, detail=code)


@app.exception_handler(VirtualposMockError)
async def virtualpos_mock_error_handler(request: Request, exc: VirtualposMockError):
    return virtualpos_error(str(exc.detail), exc.status_code)


def _validate_headers(authorization: Optional[str], signature: Optional[str]) -> None:
    """En modo estricto exige los headers reales, sin validar el secreto JWT."""
    strict = os.getenv("MOCK_VIRTUALPOS_STRICT_AUTH", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    if strict and (not authorization or not signature):
        raise VirtualposMockError("E-001" if not authorization else "E-002", 401)


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise VirtualposMockError("E-040") from exc
    if not isinstance(body, dict):
        raise VirtualposMockError("E-040")
    return body


def _required(body: Dict[str, Any], *fields: str) -> None:
    missing = [field for field in fields if body.get(field) in (None, "")]
    if missing:
        codes = {
            "amount": "E-003",
            "email": "E-004",
            "social_id": "E-005",
            "first_name": "E-006",
            "last_name": "E-007",
            "phone": "E-008",
            "description": "E-009",
            "return_url": "E-022",
            "callback_url": "E-023",
        }
        raise VirtualposMockError(codes.get(missing[0], "E-040"))


def _payment_response(record: Dict[str, Any]) -> Dict[str, Any]:
    """Devuelve el objeto documentado y alias superiores tolerantes para clientes."""
    order = {
        "uuid": record["uuid"],
        "status": record["status"],
        "created_at": record["created_at"],
        "card_number": record.get("card_number"),
        "authorized_at": record.get("authorized_at"),
        "auth_code": record.get("auth_code"),
        "installment_amount": record.get("installment_amount", 0),
        "installment_number": record.get("installment_number", 0),
        "payment_type_code": record.get("payment_type_code"),
        "amount": record["amount"],
        "merchant_internal_code": record.get("merchant_internal_code"),
        "merchant_internal_channel": record.get("merchant_internal_channel"),
        "deposits": record.get("deposits", []),
    }
    if record.get("updated_at"):
        order["updated_at"] = record["updated_at"]
    if record.get("rejected_object"):
        order["rejected_object"] = dict(record["rejected_object"])
    result = {
        "payment": {
            "client": dict(record["client"]),
            "order": order,
        },
        # Algunos clientes extraen estos valores directamente de la raíz.
        "uuid": record["uuid"],
        "status": record["status"],
    }
    return result


def _get_payment(payment_uuid: str) -> Dict[str, Any]:
    record = PAYMENTS.get(payment_uuid)
    if not record:
        raise VirtualposMockError("E-020")
    return record


async def _send_webhook(record: Dict[str, Any]) -> Dict[str, Any]:
    target_url = record.get("callback_url")
    delivery = {
        "uuid": record["uuid"],
        "target_url": target_url,
        "status_code": None,
        "response": None,
        "success": False,
        "created_at": _now(),
    }
    if not target_url:
        delivery["response"] = "Payment sin callback_url"
        WEBHOOK_DELIVERIES.append(delivery)
        return delivery

    # La documentación define un POST con el parámetro uuid. Se envía como
    # application/x-www-form-urlencoded para que funcione con request.POST y
    # con los parsers habituales de APIs web.
    try:
        timeout = float(os.getenv("MOCK_VIRTUALPOS_WEBHOOK_TIMEOUT", "10"))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(target_url, data={"uuid": record["uuid"]})
        delivery.update(
            {
                "status_code": response.status_code,
                "response": response.text[:1000],
                "success": 200 <= response.status_code < 300,
            }
        )
    except Exception as exc:
        delivery["response"] = str(exc)

    WEBHOOK_DELIVERIES.append(delivery)
    return delivery


@app.post("/v3/payment")
@app.post("/v3/payment/")
async def virtualpos_create_payment(
    request: Request,
    authorization: Optional[str] = Header(None),
    signature: Optional[str] = Header(None),
):
    from virtualpos.scenarios import global_fault, initialize

    if response := await global_fault("create"):
        return response
    _validate_headers(authorization, signature)
    body = await _json_body(request)
    _required(
        body,
        "amount",
        "email",
        "social_id",
        "first_name",
        "last_name",
        "phone",
        "description",
    )
    try:
        parsed_amount = Decimal(str(body["amount"]))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise VirtualposMockError("E-010") from exc
    if (
        not parsed_amount.is_finite()
        or parsed_amount != parsed_amount.to_integral_value()
    ):
        raise VirtualposMockError("E-011")
    amount = int(parsed_amount)
    if not 1000 <= amount <= 2_147_483_647:
        raise VirtualposMockError("E-011")

    payment_uuid = uuid_module.uuid4().hex[:16]
    record = {
        "uuid": payment_uuid,
        "status": "pendiente",
        "created_at": _now(),
        "authorized_at": None,
        "amount": amount,
        "description": str(body["description"]),
        "merchant_internal_code": body.get("merchant_internal_code"),
        "merchant_internal_channel": body.get("merchant_internal_channel"),
        "return_url": _decode_url(body.get("return_url")),
        "callback_url": _decode_url(body.get("callback_url")),
        "payment_method": "all",
        "client": {
            "email": body["email"],
            "first_name": body["first_name"],
            "last_name": body["last_name"],
            "social_id": body["social_id"],
            "phone": body["phone"],
        },
        "card_number": None,
        "auth_code": None,
        "installment_amount": 0,
        "installment_number": 0,
        "payment_type_code": None,
        "deposits": [],
    }
    initialize(record)
    PAYMENTS[payment_uuid] = record
    return JSONResponse(_payment_response(record), status_code=200)


@app.post("/v3/payment/{payment_uuid}/webcheckout")
async def virtualpos_create_webcheckout(
    payment_uuid: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    signature: Optional[str] = Header(None),
):
    _validate_headers(authorization, signature)
    record = _get_payment(payment_uuid)
    from virtualpos.scenarios import operation_fault

    response, fault = await operation_fault(record, "checkout")
    if response is not None:
        return response
    body = await _json_body(request)
    _required(body, "return_url", "callback_url")

    payment_method = str(body.get("payment_method") or "all").lower()
    if payment_method not in ALLOWED_PAYMENT_METHODS:
        raise VirtualposMockError("E-024")
    return_url = _decode_url(body["return_url"])
    callback_url = _decode_url(body["callback_url"])
    if not return_url or not callback_url:
        raise VirtualposMockError("E-025" if not return_url else "E-026")

    record.update(
        {
            "return_url": return_url,
            "callback_url": callback_url,
            "payment_method": payment_method,
        }
    )
    PAYMENTS[payment_uuid] = record
    checkout_url = f"{_request_base_url(request)}/virtualpos/checkout/{payment_uuid}"
    # ``url`` es la respuesta principal; los alias facilitan clientes que
    # normalizan el nombre del enlace internamente.
    return {
        "url_redirect": checkout_url,
        "url": checkout_url,
        "webcheckout_url": checkout_url,
        "payment_url": checkout_url,
        "uuid": payment_uuid,
    }


@app.get("/v3/payment/{payment_uuid}")
async def virtualpos_get_payment(
    payment_uuid: str,
    authorization: Optional[str] = Header(None),
    signature: Optional[str] = Header(None),
):
    _validate_headers(authorization, signature)
    from virtualpos.scenarios import operation_fault
    from mock_controls import corrupt_payload

    record = _get_payment(payment_uuid)
    response, fault = await operation_fault(record, "get")
    if response is not None:
        return response
    return corrupt_payload(_payment_response(record), fault, provider="virtualpos")


def _checkout_html(
    record: Dict[str, Any], delivery: Optional[Dict[str, Any]] = None
) -> str:
    payment_uuid = html.escape(record["uuid"])
    client = record["client"]
    delivery_notice = ""
    if delivery is not None:
        if delivery["success"]:
            delivery_notice = (
                '<div class="notice ok">Webhook entregado correctamente.</div>'
            )
        else:
            message = html.escape(str(delivery.get("response") or "No entregado"))
            delivery_notice = f'<div class="notice warn">El pago cambió de estado, pero el webhook no pudo entregarse: {message}</div>'

    return f"""
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Mock VirtualPOS - Web Checkout</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin:0; padding:32px 18px; background:#f4f7fb; color:#172033; font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif; }}
    main {{ max-width:680px; margin:0 auto; background:#fff; border-radius:20px; box-shadow:0 18px 50px rgba(27,49,84,.13); overflow:hidden; }}
    header {{ padding:24px 28px; background:#173f78; color:#fff; }}
    header strong {{ font-size:20px; }}
    section {{ padding:28px; }}
    h1 {{ margin:0 0 8px; font-size:28px; }}
    .amount {{ margin:28px 0; font-size:44px; font-weight:800; color:#173f78; }}
    .meta {{ display:grid; gap:10px; margin-bottom:24px; }}
    .row {{ padding:12px 14px; border-radius:11px; background:#f6f8fb; overflow-wrap:anywhere; }}
    .row b {{ display:block; margin-bottom:3px; font-size:12px; text-transform:uppercase; color:#667085; }}
    .actions {{ display:flex; flex-wrap:wrap; gap:12px; }}
    form {{ flex:1 1 230px; }}
    button {{ width:100%; border:0; border-radius:12px; padding:14px 18px; color:#fff; font-size:16px; font-weight:750; cursor:pointer; }}
    .success {{ background:#17864b; }}
    .failure {{ background:#c93636; }}
    .notice {{ margin:0 0 18px; padding:12px 14px; border-radius:10px; font-weight:650; }}
    .notice.ok {{ background:#dcfce7; color:#166534; }}
    .notice.warn {{ background:#fff3cd; color:#7a5200; overflow-wrap:anywhere; }}
    .status {{ display:inline-block; padding:5px 9px; border-radius:999px; background:#e8eef7; color:#173f78; font-weight:700; }}
  </style>
</head>
<body>
  <main>
    <header><strong>VirtualPOS</strong> <span>· Web Checkout de prueba</span></header>
    <section>
      <h1>Confirmar pago</h1>
      <span class="status">{html.escape(record["status"])}</span>
      {delivery_notice}
      <div class="amount">$ {record["amount"]:,} CLP</div>
      <div class="meta">
        <div class="row"><b>Descripción</b>{html.escape(record["description"])}</div>
        <div class="row"><b>Pagador</b>{html.escape(str(client["first_name"]))} {html.escape(str(client["last_name"]))}</div>
        <div class="row"><b>Payment UUID</b><code>{payment_uuid}</code></div>
        <div class="row"><b>Callback</b><code>{html.escape(str(record.get("callback_url") or "No configurado"))}</code></div>
      </div>
      <p><a href="/mock/scenarios?provider=virtualpos&resource_id={payment_uuid}">Configurar errores de este Payment</a></p>
      <div class="actions">
        <form method="post" action="/virtualpos/checkout/{payment_uuid}/success">
          <button class="success" type="submit">Pago correcto</button>
        </form>
        <form method="post" action="/virtualpos/checkout/{payment_uuid}/failure">
          <button class="failure" type="submit">Pago fallido</button>
        </form>
      </div>
    </section>
  </main>
</body>
</html>
"""


@app.get("/virtualpos/checkout/{payment_uuid}", response_class=HTMLResponse)
async def virtualpos_checkout(payment_uuid: str):
    return HTMLResponse(_checkout_html(_get_payment(payment_uuid)))


def _return_post_html(record: Dict[str, Any], delivery: Dict[str, Any]) -> str:
    """Crea el POST de retorno que VirtualPOS envía al finalizar el navegador."""
    return_url = record.get("return_url")
    if not return_url:
        return _checkout_html(record, delivery)
    action = html.escape(return_url, quote=True)
    payment_uuid = html.escape(record["uuid"], quote=True)
    result = (
        "correcto"
        if record["status"] == "pagado"
        else (
            "pendiente"
            if record["status"] in {"pendiente", "reintentando"}
            else "fallido"
        )
    )
    return f"""
<!doctype html>
<html lang="es">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pago {result}</title></head>
<body style="font-family:system-ui;text-align:center;padding:48px;background:#f4f7fb;color:#172033">
  <h1>Pago {result}</h1>
  <p>Estado: <strong>{html.escape(record["status"])}</strong></p>
  <p>Redirigiendo al comercio mediante HTTP POST…</p>
  <form id="return-form" method="post" action="{action}">
    <input type="hidden" name="uuid" value="{payment_uuid}">
    <button type="submit">Continuar</button>
  </form>
  <script>setTimeout(function () {{ document.getElementById('return-form').submit(); }}, 700);</script>
</body>
</html>
"""


async def _resolve_checkout(payment_uuid: str, successful: bool):
    from virtualpos.scenarios import resolve

    return await resolve(payment_uuid, successful)


@app.post("/virtualpos/checkout/{payment_uuid}/success", response_class=HTMLResponse)
async def virtualpos_checkout_success(payment_uuid: str):
    return await _resolve_checkout(payment_uuid, successful=True)


@app.post("/virtualpos/checkout/{payment_uuid}/failure", response_class=HTMLResponse)
async def virtualpos_checkout_failure(payment_uuid: str):
    return await _resolve_checkout(payment_uuid, successful=False)


@app.get("/admin/virtualpos/payments")
async def virtualpos_admin_payments():
    return [_payment_response(record) for record in PAYMENTS.values()]


@app.get("/admin/virtualpos/webhook-deliveries")
async def virtualpos_admin_webhook_deliveries():
    return WEBHOOK_DELIVERIES


@app.post("/admin/virtualpos/reset")
async def virtualpos_admin_reset():
    PAYMENTS.clear()
    WEBHOOK_DELIVERIES.clear()
    return {"status": "ok", "message": "Estado VirtualPOS reseteado"}


# Register scenario routes once the checkout/response helpers are available.
from virtualpos import scenarios as _scenarios  # noqa: E402, F401
