from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from dotenv import load_dotenv
from fastapi import Header, HTTPException, Path as FastAPIPath, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from app import app


load_dotenv()


# =============================================================================
# Local mock configuration
# =============================================================================

PAYPAL_MOCK_PUBLIC_BASE_URL = os.getenv(
    "PAYPAL_MOCK_PUBLIC_BASE_URL",
    "http://localhost:7000",
).rstrip("/")

# CrinnoPayments Paypal webhook endpoint.
PAYPAL_MOCK_WEBHOOK_URL = os.getenv(
    "PAYPAL_MOCK_WEBHOOK_URL",
    "http://localhost:8001/api/paypal/webhooks/notifications/",
).strip()

# This value must match PaypalConfiguration.paypal_webhook_id in CrinnoPayments.
PAYPAL_MOCK_WEBHOOK_ID = os.getenv(
    "PAYPAL_MOCK_WEBHOOK_ID",
    "tu_webhook_id_aqui",
).strip()

# Small delay avoids racing CHECKOUT.PAYMENT-RESOURCE.CREATED against the
# CrinnoPayments transaction that is still storing paypal_payment_link_id.
PAYPAL_MOCK_WEBHOOK_DELAY_SECONDS = float(
    os.getenv("PAYPAL_MOCK_WEBHOOK_DELAY_SECONDS", "1.0")
)


# =============================================================================
# Shared in-memory state
# =============================================================================

# Legacy Orders v2 state.
DATA_PAYPAL = {
    "amount": 0,
    "currency": "USD",
    "orders_id": "xxxxx",
    "intentos_get_order": 0,
    "fallar_primer_intento_get_order": False,
    "intentos_capture_order": 0,
    "fallar_primer_intento_capture_order": False,
    "fallar_captura": False,
    "fallar_estado_get_order": False,
    "intentos_fallar_estado_get_order": 0,
}

ORDERS_PAYPAL: dict[str, dict[str, Any]] = {}

# New Payment Links and Buttons API state.
PAYMENT_RESOURCES: dict[str, dict[str, Any]] = {}

# Useful when debugging local delivery.
WEBHOOK_DELIVERIES: list[dict[str, Any]] = []

# Prevent two simultaneous POSTs to the same mock checkout from both being paid.
PAYMENT_RESOURCE_LOCK = asyncio.Lock()


# =============================================================================
# Local PayPal-like webhook signing
# =============================================================================

# Generate a new certificate per mock process. The certificate URL includes a
# random ID so CrinnoPayments will not reuse a certificate cached by a previous
# mock process after a restart.
_CERTIFICATE_ID = uuid.uuid4().hex
_WEBHOOK_PRIVATE_KEY = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
)

_cert_name = x509.Name(
    [
        x509.NameAttribute(NameOID.COUNTRY_NAME, "SR"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Local PayPal Mock"),
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
    ]
)

_cert_now = datetime.now(timezone.utc)
_WEBHOOK_CERTIFICATE = (
    x509.CertificateBuilder()
    .subject_name(_cert_name)
    .issuer_name(_cert_name)
    .public_key(_WEBHOOK_PRIVATE_KEY.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(_cert_now - timedelta(days=1))
    .not_valid_after(_cert_now + timedelta(days=3650))
    .add_extension(
        x509.BasicConstraints(ca=True, path_length=None),
        critical=True,
    )
    .sign(_WEBHOOK_PRIVATE_KEY, hashes.SHA256())
)

_WEBHOOK_CERTIFICATE_PEM = _WEBHOOK_CERTIFICATE.public_bytes(
    serialization.Encoding.PEM
).decode("ascii")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def paypal_datetime(value: datetime | None = None) -> str:
    value = value or utc_now()
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _model_to_dict(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _webhook_target_url() -> str:
    """
    Add ?whid=<mock webhook id> unless the configured URL already contains it.
    CrinnoPayments uses this value to select the PaypalConfiguration first.
    """
    if not PAYPAL_MOCK_WEBHOOK_URL:
        return ""

    parts = urlsplit(PAYPAL_MOCK_WEBHOOK_URL)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("whid", PAYPAL_MOCK_WEBHOOK_ID)

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


def _build_signed_webhook(
    *,
    event_type: str,
    resource_type: str,
    summary: str,
    resource: dict[str, Any],
) -> tuple[bytes, dict[str, str], dict[str, Any]]:
    event = {
        "id": f"WH-{uuid.uuid4().hex.upper()}",
        "event_version": "1.0",
        "create_time": paypal_datetime(),
        "resource_type": resource_type,
        "event_type": event_type,
        "summary": summary,
        "resource": resource,
        "links": [],
    }

    # Use exactly these bytes for CRC, signature and HTTP delivery.
    body = json.dumps(
        event,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    transmission_id = str(uuid.uuid4())
    transmission_time = paypal_datetime()
    body_crc = zlib.crc32(body)

    message = (
        f"{transmission_id}|"
        f"{transmission_time}|"
        f"{PAYPAL_MOCK_WEBHOOK_ID}|"
        f"{body_crc}"
    ).encode("utf-8")

    signature = _WEBHOOK_PRIVATE_KEY.sign(
        message,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )

    cert_url = (
        f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
        f"/v1/notifications/certs/{_CERTIFICATE_ID}"
    )

    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Paypal-Auth-Algo": "SHA256withRSA",
        "Paypal-Auth-Version": "v2",
        "Paypal-Cert-Url": cert_url,
        "Paypal-Transmission-Id": transmission_id,
        "Paypal-Transmission-Sig": base64.b64encode(signature).decode("ascii"),
        "Paypal-Transmission-Time": transmission_time,
        "User-Agent": "PayPal/LOCAL-MOCK-1.0",
    }

    return body, headers, event


async def _deliver_webhook(
    *,
    event_type: str,
    resource_type: str,
    summary: str,
    resource: dict[str, Any],
) -> None:
    target_url = _webhook_target_url()

    if not target_url:
        print(
            f"[PaypalMock][Webhook][{event_type}] "
            "PAYPAL_MOCK_WEBHOOK_URL is empty. Delivery skipped."
        )
        return

    body, headers, event = _build_signed_webhook(
        event_type=event_type,
        resource_type=resource_type,
        summary=summary,
        resource=resource,
    )

    delivery = {
        "event_id": event["id"],
        "event_type": event_type,
        "target_url": target_url,
        "status_code": None,
        "error": "",
        "created_at": paypal_datetime(),
    }
    WEBHOOK_DELIVERIES.append(delivery)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                target_url,
                content=body,
                headers=headers,
            )

        delivery["status_code"] = response.status_code

        print(
            f"[PaypalMock][Webhook][{event_type}] "
            f"{target_url} -> {response.status_code}"
        )

        if not response.is_success:
            print(
                f"[PaypalMock][Webhook][{event_type}] "
                f"Response: {response.text}"
            )

    except Exception as exc:
        delivery["error"] = str(exc)
        print(
            f"[PaypalMock][Webhook][{event_type}] "
            f"Delivery error: {exc}"
        )


async def _deliver_webhook_after_delay(
    *,
    event_type: str,
    resource_type: str,
    summary: str,
    resource: dict[str, Any],
    delay: float | None = None,
) -> None:
    await asyncio.sleep(
        PAYPAL_MOCK_WEBHOOK_DELAY_SECONDS
        if delay is None
        else delay
    )

    await _deliver_webhook(
        event_type=event_type,
        resource_type=resource_type,
        summary=summary,
        resource=resource,
    )


def schedule_webhook(
    *,
    event_type: str,
    resource_type: str,
    summary: str,
    resource: dict[str, Any],
    delay: float | None = None,
) -> None:
    asyncio.create_task(
        _deliver_webhook_after_delay(
            event_type=event_type,
            resource_type=resource_type,
            summary=summary,
            resource=resource,
            delay=delay,
        )
    )


@app.get(
    "/v1/notifications/certs/{certificate_id}",
    response_class=PlainTextResponse,
)
async def get_mock_paypal_certificate(certificate_id: str):
    if certificate_id != _CERTIFICATE_ID:
        raise HTTPException(status_code=404, detail="Certificate not found")

    return PlainTextResponse(
        content=_WEBHOOK_CERTIFICATE_PEM,
        media_type="application/x-pem-file",
    )


# =============================================================================
# OAuth
# =============================================================================

@app.post("/v1/oauth2/token")
async def create_paypal_access_token(request: Request):
    return {
        "scope": (
            "https://uri.paypal.com/services/checkout/payment-resources "
            "https://uri.paypal.com/services/payments/payment/authcapture"
        ),
        "access_token": str(uuid.uuid4()),
        "token_type": "Bearer",
        "app_id": "APP-LOCAL-MOCK",
        "expires_in": 31668,
        "nonce": uuid.uuid4().hex,
    }


# =============================================================================
# Legacy Orders v2 flow
# =============================================================================

class Amount(BaseModel):
    currency_code: str = Field(..., min_length=3, max_length=3)
    value: str = Field(..., pattern=r"^\d+(?:\.\d{2})?$")


class PurchaseUnit(BaseModel):
    reference_id: str = Field(..., min_length=1, max_length=127)
    amount: Amount


class ExperienceContext(BaseModel):
    payment_method_preference: Literal[
        "IMMEDIATE_PAYMENT_REQUIRED"
    ] = "IMMEDIATE_PAYMENT_REQUIRED"
    landing_page: Literal["GUEST_CHECKOUT"] = "GUEST_CHECKOUT"
    return_url: str
    cancel_url: str


class PayPalSource(BaseModel):
    experience_context: ExperienceContext


class PaymentSource(BaseModel):
    paypal: PayPalSource


class CaptureRequest(BaseModel):
    intent: Literal["CAPTURE"] = "CAPTURE"
    payment_source: PaymentSource
    purchase_units: list[PurchaseUnit] = Field(
        ...,
        min_length=1,
        max_length=10,
    )


def build_decimals_not_supported_response(
    purchase_unit: PurchaseUnit,
) -> dict[str, Any]:
    return {
        "name": "UNPROCESSABLE_ENTITY",
        "links": [
            {
                "rel": "information_link",
                "href": (
                    "https://developer.paypal.com/api/rest/reference/"
                    "orders/v2/errors/#DECIMALS_NOT_SUPPORTED"
                ),
                "method": "GET",
            }
        ],
        "details": [
            {
                "field": (
                    "/purchase_units/"
                    f"@reference_id=='{purchase_unit.reference_id}'"
                    "/amount/value"
                ),
                "issue": "DECIMALS_NOT_SUPPORTED",
                "value": purchase_unit.amount.value,
                "description": "Currency does not support decimals.",
            }
        ],
        "message": (
            "The requested action could not be performed, "
            "semantically incorrect, or failed business validation."
        ),
        "debug_id": uuid.uuid4().hex[:13],
    }


def build_mock_response_checkout(
    request_data: CaptureRequest,
) -> dict[str, Any]:
    purchase_unit = request_data.purchase_units[0]

    order_id = str(uuid.uuid4())
    amount = purchase_unit.amount.value
    currency = purchase_unit.amount.currency_code
    context = request_data.payment_source.paypal.experience_context

    ORDERS_PAYPAL[order_id] = {
        "id": order_id,
        "status": "PAYER_ACTION_REQUIRED",
        "reference_id": purchase_unit.reference_id,
        "amount": amount,
        "currency": currency,
        "return_url": context.return_url,
        "cancel_url": context.cancel_url,
        "approved_webhook_sent": False,
        "capture_id": "",
        "created_at": paypal_datetime(),
    }

    DATA_PAYPAL.update(
        {
            "amount": amount,
            "currency": currency,
            "orders_id": order_id,
            "intentos_get_order": 0,
            "intentos_capture_order": 0,
            "intentos_fallar_estado_get_order": 0,
        }
    )

    return {
        "id": order_id,
        "status": "PAYER_ACTION_REQUIRED",
        "payment_source": {"paypal": {}},
        "links": [
            {
                "href": (
                    f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
                    f"/v2/checkout/orders/{order_id}"
                ),
                "rel": "self",
                "method": "GET",
            },
            {
                "href": (
                    f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
                    f"/checkoutnow?token={order_id}"
                ),
                "rel": "payer-action",
                "method": "GET",
            },
        ],
    }


@app.post("/v2/checkout/orders")
async def create_legacy_paypal_order(request: CaptureRequest):
    for purchase_unit in request.purchase_units:
        if (
            purchase_unit.amount.currency_code == "CLP"
            and "." in purchase_unit.amount.value
        ):
            return JSONResponse(
                status_code=422,
                content=build_decimals_not_supported_response(purchase_unit),
            )

    return JSONResponse(
        status_code=201,
        content=build_mock_response_checkout(request),
    )


def _legacy_approved_webhook_resource(
    order: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": order["id"],
        "status": "APPROVED",
        "purchase_units": [
            {
                "reference_id": order["reference_id"],
                "amount": {
                    "currency_code": order["currency"],
                    "value": order["amount"],
                },
            }
        ],
    }


def _legacy_checkout_html(order: dict[str, Any]) -> str:
    order_id = html.escape(order["id"])
    amount = html.escape(str(order["amount"]))
    currency = html.escape(order["currency"])
    reference_id = html.escape(order["reference_id"])
    return_url = html.escape(order.get("return_url") or "")
    cancel_url = html.escape(order.get("cancel_url") or "")
    status = html.escape(order["status"])

    if order["status"] == "COMPLETED":
        action_block = """
        <div class="notice success">
            Esta orden ya fue capturada correctamente.
        </div>
        """
    elif order["status"] == "APPROVED":
        action_block = """
        <div class="notice warning">
            El pago ya fue aprobado. CrinnoPayments debe capturarlo mediante
            el webhook CHECKOUT.ORDER.APPROVED.
        </div>
        """
    else:
        action_block = f"""
        <form method="post" action="/mock/paypal/orders/{order_id}/approve">
            <button class="primary" type="submit">Aprobar pago</button>
        </form>
        """

    continue_link = (
        f'<a class="secondary" href="{return_url}">Ir al return_url</a>'
        if return_url
        else ""
    )
    cancel_link = (
        f'<a class="secondary" href="{cancel_url}">Cancelar / volver</a>'
        if cancel_url
        else ""
    )

    return _checkout_page(
        title="PayPal Mock - Orders v2",
        subtitle="Legacy Orders v2 checkout",
        body=f"""
        <div class="card">
            <div class="paypal-logo">PayPal <span>LOCAL MOCK</span></div>
            <h2>{amount} {currency}</h2>
            <p><strong>Order:</strong> {order_id}</p>
            <p><strong>Reference:</strong> {reference_id}</p>
            <p><strong>Status:</strong> {status}</p>
            {action_block}
            <div class="links">
                {continue_link}
                {cancel_link}
            </div>
        </div>
        """,
    )


@app.get("/checkoutnow", response_class=HTMLResponse)
async def legacy_checkout_page(
    token: str = Query(...),
):
    order = ORDERS_PAYPAL.get(token)

    if not order:
        return HTMLResponse(
            _not_available_page(
                "Orden no encontrada",
                "El token de la orden no existe en el mock.",
            ),
            status_code=404,
        )

    return HTMLResponse(_legacy_checkout_html(order))


@app.post(
    "/mock/paypal/orders/{order_id}/approve",
    response_class=HTMLResponse,
)
async def approve_legacy_order(order_id: str):
    order = ORDERS_PAYPAL.get(order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] == "COMPLETED":
        return HTMLResponse(_legacy_checkout_html(order))

    if order["status"] != "APPROVED":
        order["status"] = "APPROVED"

    if not order["approved_webhook_sent"]:
        order["approved_webhook_sent"] = True

        schedule_webhook(
            event_type="CHECKOUT.ORDER.APPROVED",
            resource_type="checkout-order",
            summary="An order has been approved by buyer",
            resource=_legacy_approved_webhook_resource(order),
            delay=0.0,
        )

    return HTMLResponse(
        _checkout_page(
            title="PayPal Mock - Pago aprobado",
            subtitle="CHECKOUT.ORDER.APPROVED encolado",
            body=f"""
            <div class="card">
                <div class="notice success">
                    Pago aprobado localmente.
                </div>
                <p>
                    El mock envió <code>CHECKOUT.ORDER.APPROVED</code>.
                    CrinnoPayments debe consultar la orden y ejecutar
                    <code>/capture</code>.
                </p>
                <p><strong>Order:</strong> {html.escape(order_id)}</p>
                <a class="secondary"
                   href="/checkoutnow?token={html.escape(order_id)}">
                    Ver estado
                </a>
            </div>
            """,
        )
    )


@app.post("/v2/checkout/orders/{order_id}/capture")
async def capture_order(
    order_id: str = FastAPIPath(
        ...,
        description="ID de la orden a capturar",
    ),
    authorization: str = Header(
        ...,
        description="Bearer token for authentication",
    ),
    paypal_request_id: str = Header(
        ...,
        alias="PayPal-Request-Id",
        description="Unique request ID for idempotency",
    ),
):
    if (
        not authorization.startswith("Bearer ")
        or not authorization.removeprefix("Bearer ").strip()
    ):
        raise HTTPException(
            status_code=401,
            detail=(
                "Invalid authorization header. "
                "Expected format: 'Bearer <token>'"
            ),
        )

    if (
        not paypal_request_id
        or not paypal_request_id.strip()
        or len(paypal_request_id.strip()) > 107
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Missing or empty PayPal-Request-Id header "
                "required for idempotency"
            ),
        )

    order = ORDERS_PAYPAL.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if DATA_PAYPAL["fallar_captura"]:
        return JSONResponse(
            status_code=422,
            content={
                "name": "UNPROCESSABLE_ENTITY",
                "details": [
                    {
                        "issue": "TRANSACTION_REFUSED",
                        "description": "The request was refused",
                    }
                ],
                "message": (
                    "The requested action could not be performed, "
                    "semantically incorrect, or failed business validation."
                ),
                "debug_id": uuid.uuid4().hex[:13],
            },
        )

    if (
        DATA_PAYPAL["fallar_primer_intento_capture_order"]
        and DATA_PAYPAL["intentos_capture_order"] == 0
    ):
        DATA_PAYPAL["intentos_capture_order"] = 1
        raise HTTPException(
            status_code=502,
            detail="Error procesando pago con PayPal",
        )

    # Make capture idempotent in the mock as well.
    if not order["capture_id"]:
        order["capture_id"] = uuid.uuid4().hex[:17].upper()

    capture_id = order["capture_id"]
    order["status"] = "COMPLETED"

    current_time = paypal_datetime()

    response = {
        "id": order_id,
        "status": "COMPLETED",
        "payment_source": {
            "paypal": {
                "email_address": "example@gmail.com",
                "account_id": "MOCKACCOUNT",
                "account_status": "VERIFIED",
                "name": {
                    "given_name": "John",
                    "surname": "Doe",
                },
                "address": {
                    "country_code": "SR",
                },
            }
        },
        "purchase_units": [
            {
                "reference_id": order["reference_id"],
                "payments": {
                    "captures": [
                        {
                            "id": capture_id,
                            "status": "COMPLETED",
                            "amount": {
                                "currency_code": order["currency"],
                                "value": order["amount"],
                            },
                            "final_capture": True,
                            "create_time": current_time,
                            "update_time": current_time,
                            "links": [
                                {
                                    "href": (
                                        f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
                                        f"/v2/payments/captures/{capture_id}"
                                    ),
                                    "rel": "self",
                                    "method": "GET",
                                },
                                {
                                    "href": (
                                        f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
                                        f"/v2/checkout/orders/{order_id}"
                                    ),
                                    "rel": "up",
                                    "method": "GET",
                                },
                            ],
                        }
                    ]
                },
            }
        ],
        "payer": {
            "name": {
                "given_name": "John",
                "surname": "Doe",
            },
            "email_address": "customer@example.com",
            "payer_id": "MOCKPAYER",
            "address": {
                "country_code": "SR",
            },
        },
        "links": [
            {
                "href": (
                    f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
                    f"/v2/checkout/orders/{order_id}"
                ),
                "rel": "self",
                "method": "GET",
            }
        ],
    }

    return JSONResponse(status_code=201, content=response)


@app.get("/v2/checkout/orders/{order_id}")
async def get_order_details(order_id: str):
    order = ORDERS_PAYPAL.get(order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if (
        DATA_PAYPAL["fallar_primer_intento_get_order"]
        and DATA_PAYPAL["intentos_get_order"] == 0
    ):
        DATA_PAYPAL["intentos_get_order"] = 1
        raise HTTPException(status_code=404, detail="Order not found")

    status = order["status"]

    if (
        DATA_PAYPAL["fallar_estado_get_order"]
        and DATA_PAYPAL["intentos_fallar_estado_get_order"] == 0
    ):
        DATA_PAYPAL["intentos_fallar_estado_get_order"] = 1
        status = "OTRO"

    return {
        "id": order_id,
        "status": status,
        "intent": "CAPTURE",
        "payment_source": {
            "paypal": {
                "name": {
                    "given_name": "John",
                    "surname": "Doe",
                },
                "email_address": "customer@example.com",
                "account_id": "MOCKACCOUNT",
            }
        },
        "purchase_units": [
            {
                "reference_id": order["reference_id"],
                "amount": {
                    "currency_code": order["currency"],
                    "value": order["amount"],
                },
            }
        ],
        "payer": {
            "name": {
                "given_name": "John",
                "surname": "Doe",
            },
            "email_address": "customer@example.com",
            "payer_id": "MOCKPAYER",
        },
        "create_time": order["created_at"],
        "links": [
            {
                "href": (
                    f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
                    f"/v2/checkout/orders/{order_id}"
                ),
                "rel": "self",
                "method": "GET",
            },
            {
                "href": (
                    f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
                    f"/checkoutnow?token={order_id}"
                ),
                "rel": "approve",
                "method": "GET",
            },
            {
                "href": (
                    f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
                    f"/v2/checkout/orders/{order_id}/capture"
                ),
                "rel": "capture",
                "method": "POST",
            },
        ],
    }


# =============================================================================
# Payment Links and Buttons API flow
# =============================================================================

class PaymentResourceAmount(BaseModel):
    currency_code: str = Field(..., min_length=3, max_length=3)
    value: str = Field(..., pattern=r"^\d+(?:\.\d{1,2})?$")


class AdjustableQuantity(BaseModel):
    maximum: int | None = None


class PaymentResourceLineItem(BaseModel):
    name: str
    product_id: str | None = None
    description: str | None = None
    unit_amount: PaymentResourceAmount
    adjustable_quantity: AdjustableQuantity | None = None


class PaymentResourceCreateRequest(BaseModel):
    integration_mode: Literal["LINK"]
    type: Literal["BUY_NOW"]
    reusable: Literal["MULTIPLE"]
    line_items: list[PaymentResourceLineItem] = Field(
        ...,
        min_length=1,
    )
    return_url: str | None = None


def generate_payment_resource_id() -> str:
    return f"PLB-{uuid.uuid4().hex[:12].upper()}"


def _payment_resource_api_response(
    resource: dict[str, Any],
) -> dict[str, Any]:
    payment_resource_id = resource["id"]

    return {
        "id": payment_resource_id,
        "type": resource["type"],
        "links": [
            {
                "rel": "self",
                "href": (
                    f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
                    f"/v1/checkout/payment-resources/"
                    f"{payment_resource_id}"
                ),
                "method": "GET",
            },
            {
                "rel": "replace",
                "href": (
                    f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
                    f"/v1/checkout/payment-resources/"
                    f"{payment_resource_id}"
                ),
                "method": "PUT",
            },
            {
                "rel": "edit",
                "href": (
                    f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
                    f"/v1/checkout/payment-resources/"
                    f"{payment_resource_id}"
                ),
                "method": "PATCH",
            },
            {
                "rel": "delete",
                "href": (
                    f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
                    f"/v1/checkout/payment-resources/"
                    f"{payment_resource_id}"
                ),
                "method": "DELETE",
            },
            {
                "rel": "payment_link",
                "href": (
                    f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
                    f"/ncp/payment/{payment_resource_id}"
                ),
                "method": "GET",
            },
        ],
        "status": resource["status"],
        "reusable": resource["reusable"],
        "line_items": resource["line_items"],
        "return_url": resource.get("return_url"),
        "create_time": resource["create_time"],
        "update_time": resource["update_time"],
        "integration_mode": resource["integration_mode"],
    }


def _payment_resource_created_webhook_resource(
    resource: dict[str, Any],
) -> dict[str, Any]:
    return {
        "update_time": resource["update_time"],
        "create_time": resource["create_time"],
        "integration_mode": resource["integration_mode"],
        "return_url": resource.get("return_url"),
        "id": resource["id"],
        "line_items": resource["line_items"],
        "type": resource["type"],
        "reusable": resource["reusable"],
        "status": resource["status"],
    }


def _payment_resource_deleted_webhook_resource(
    resource: dict[str, Any],
) -> dict[str, Any]:
    return {
        "update_time": resource["update_time"],
        "create_time": resource["create_time"],
        "integration_mode": resource["integration_mode"],
        "return_url": resource.get("return_url"),
        "id": resource["id"],
        "line_items": resource["line_items"],
        "type": resource["type"],
        "reusable": resource["reusable"],
        "status": "DELETED",
    }


def _payment_resource_total(
    resource: dict[str, Any],
) -> tuple[Decimal, str]:
    first_item = resource["line_items"][0]
    currency = first_item["unit_amount"]["currency_code"]

    total = Decimal("0.00")

    for item in resource["line_items"]:
        item_currency = item["unit_amount"]["currency_code"]
        if item_currency != currency:
            raise ValueError(
                "The local mock only supports one currency per Payment Resource."
            )

        total += Decimal(item["unit_amount"]["value"])

    return total.quantize(Decimal("0.01")), currency


def _payment_completed_webhook_resource(
    resource: dict[str, Any],
) -> dict[str, Any]:
    total, currency = _payment_resource_total(resource)
    fee = (total * Decimal("0.035")).quantize(Decimal("0.01"))
    net = (total - fee).quantize(Decimal("0.01"))

    transaction_id = resource["transaction_id"]
    transaction_time = resource["transaction_time"]

    return {
        "transaction_id": transaction_id,
        "payee": {
            "merchant_id": "MOCKMERCHANT",
        },
        "amount_breakdown": {
            "item_total": {
                "currency_code": currency,
                "value": f"{total:.2f}",
            }
        },
        "transaction_status": "COMPLETED",
        "shipping": {
            "name": {
                "full_name": "John Doe",
            },
            "address": {
                "address_line_1": "Waterkant",
                "admin_area_2": "Paramaribo",
                "admin_area_1": "Paramaribo",
                "postal_code": "SR0000",
                "country_code": "SR",
            },
        },
        "payment_resource_id": resource["id"],
        "paypal_fee": {
            "currency_code": currency,
            "value": f"{fee:.2f}",
        },
        "transaction_time": transaction_time,
        "gross_amount": {
            "currency_code": currency,
            "value": f"{total:.2f}",
        },
        "net_amount": {
            "currency_code": currency,
            "value": f"{net:.2f}",
        },
        "items": [
            {
                "name": item["name"],
                "unit_amount": item["unit_amount"],
                "quantity": "1",
            }
            for item in resource["line_items"]
        ],
        "payer": {
            "payer_id": "MOCKPAYER",
            "email_address": "mock-buyer@personal.example.com",
            "name": {
                "given_name": "John",
                "surname": "Doe",
            },
            "country_code": "SR",
        },
    }


@app.post("/v1/checkout/payment-resources")
async def create_payment_resource(
    request_data: PaymentResourceCreateRequest,
):
    payment_resource_id = generate_payment_resource_id()
    now = paypal_datetime()

    line_items = [
        _model_to_dict(item)
        for item in request_data.line_items
    ]

    # Match the real response observed in sandbox.
    for item in line_items:
        item.setdefault("collect_shipping_address", True)

        # Remove Pydantic optional fields that were not sent.
        if item.get("description") is None:
            item.pop("description", None)
        if item.get("product_id") is None:
            item.pop("product_id", None)
        if item.get("adjustable_quantity") is None:
            item.pop("adjustable_quantity", None)

    resource = {
        "id": payment_resource_id,
        "type": request_data.type,
        "status": "ACTIVE",
        "reusable": request_data.reusable,
        "line_items": line_items,
        "return_url": request_data.return_url,
        "create_time": now,
        "update_time": now,
        "integration_mode": request_data.integration_mode,
        "payment_completed": False,
        "transaction_id": "",
        "transaction_time": "",
        "deleted_webhook_sent": False,
    }

    PAYMENT_RESOURCES[payment_resource_id] = resource

    # Real PayPal sends this event shortly after resource creation.
    schedule_webhook(
        event_type="CHECKOUT.PAYMENT-RESOURCE.CREATED",
        resource_type="payment-resource",
        summary="Created",
        resource=_payment_resource_created_webhook_resource(resource),
    )

    return JSONResponse(
        status_code=201,
        content=_payment_resource_api_response(resource),
    )


@app.get("/v1/checkout/payment-resources/{payment_resource_id}")
async def get_payment_resource(payment_resource_id: str):
    resource = PAYMENT_RESOURCES.get(payment_resource_id)

    if not resource or resource["status"] == "DELETED":
        raise HTTPException(
            status_code=404,
            detail="Payment Resource not found",
        )

    return _payment_resource_api_response(resource)


@app.delete("/v1/checkout/payment-resources/{payment_resource_id}")
async def delete_payment_resource(payment_resource_id: str):
    resource = PAYMENT_RESOURCES.get(payment_resource_id)

    if not resource:
        raise HTTPException(
            status_code=404,
            detail="Payment Resource not found",
        )

    # Idempotent local behavior. CrinnoPayments already treats 404 as success,
    # but returning 204 repeatedly is friendlier during local tests.
    if resource["status"] == "DELETED":
        return Response(status_code=204)

    resource["status"] = "DELETED"
    resource["update_time"] = paypal_datetime()

    if not resource["deleted_webhook_sent"]:
        resource["deleted_webhook_sent"] = True

        schedule_webhook(
            event_type="CHECKOUT.PAYMENT-RESOURCE.DELETED",
            resource_type="payment-resource",
            summary="Deleted",
            resource=_payment_resource_deleted_webhook_resource(resource),
        )

    return Response(status_code=204)


def _payment_resource_checkout_html(
    resource: dict[str, Any],
) -> str:
    payment_resource_id = html.escape(resource["id"])
    status = html.escape(resource["status"])

    try:
        total, currency = _payment_resource_total(resource)
        amount_label = f"{total:.2f} {html.escape(currency)}"
    except Exception:
        amount_label = "Invalid amount"

    items_html = "".join(
        (
            "<li>"
            f"{html.escape(item['name'])}"
            " — "
            f"{html.escape(item['unit_amount']['value'])} "
            f"{html.escape(item['unit_amount']['currency_code'])}"
            "</li>"
        )
        for item in resource["line_items"]
    )

    if resource["status"] == "DELETED":
        action = """
        <div class="notice danger">
            Este Payment Link fue eliminado y ya no puede utilizarse.
        </div>
        """
    elif resource["payment_completed"]:
        action = f"""
        <div class="notice success">
            El pago ya fue completado. El mock bloquea cualquier segundo pago.
        </div>
        <p>
            Transaction:
            <code>{html.escape(resource["transaction_id"])}</code>
        </p>
        """
    else:
        action = f"""
        <form method="post"
              action="/ncp/payment/{payment_resource_id}/complete">
            <button class="primary" type="submit">
                Pagar correctamente
            </button>
        </form>
        """

    return _checkout_page(
        title="PayPal Mock - Payment Link",
        subtitle="Payment Links and Buttons API",
        body=f"""
        <div class="card">
            <div class="paypal-logo">PayPal <span>LOCAL MOCK</span></div>

            <h2>{amount_label}</h2>

            <p>
                <strong>Payment Resource:</strong>
                <code>{payment_resource_id}</code>
            </p>

            <p><strong>Status:</strong> {status}</p>

            <ul class="items">
                {items_html}
            </ul>

            {action}
        </div>
        """,
    )


@app.get(
    "/ncp/payment/{payment_resource_id}",
    response_class=HTMLResponse,
)
async def payment_resource_checkout_page(
    payment_resource_id: str,
):
    resource = PAYMENT_RESOURCES.get(payment_resource_id)

    if not resource:
        return HTMLResponse(
            _not_available_page(
                "Payment Link no encontrado",
                "El Payment Resource no existe en el mock.",
            ),
            status_code=404,
        )

    if resource["status"] == "DELETED":
        return HTMLResponse(
            _payment_resource_checkout_html(resource),
            status_code=410,
        )

    return HTMLResponse(
        _payment_resource_checkout_html(resource)
    )


@app.post(
    "/ncp/payment/{payment_resource_id}/complete",
    response_class=HTMLResponse,
)
async def complete_payment_resource(
    payment_resource_id: str,
):
    async with PAYMENT_RESOURCE_LOCK:
        resource = PAYMENT_RESOURCES.get(payment_resource_id)

        if not resource:
            return HTMLResponse(
                _not_available_page(
                    "Payment Link no encontrado",
                    "El Payment Resource no existe en el mock.",
                ),
                status_code=404,
            )

        if resource["status"] == "DELETED":
            return HTMLResponse(
                _payment_resource_checkout_html(resource),
                status_code=410,
            )

        # Important: set this BEFORE scheduling the webhook. This makes the
        # local checkout single-use even while CrinnoPayments is still
        # processing CHECKOUT.PAYMENT-RESOURCE.PAYMENT-COMPLETED and before
        # it calls DELETE.
        if resource["payment_completed"]:
            return HTMLResponse(
                _payment_resource_checkout_html(resource),
                status_code=409,
            )

        resource["payment_completed"] = True
        resource["transaction_id"] = uuid.uuid4().hex[:17].upper()
        resource["transaction_time"] = paypal_datetime()
        resource["update_time"] = resource["transaction_time"]

        webhook_resource = _payment_completed_webhook_resource(resource)

        schedule_webhook(
            event_type="CHECKOUT.PAYMENT-RESOURCE.PAYMENT-COMPLETED",
            resource_type="payment-resource",
            summary="A payment was completed on a payment resource.",
            resource=webhook_resource,
            delay=0.0,
        )

    return HTMLResponse(
        _checkout_page(
            title="PayPal Mock - Pago completado",
            subtitle=(
                "CHECKOUT.PAYMENT-RESOURCE.PAYMENT-COMPLETED encolado"
            ),
            body=f"""
            <div class="card">
                <div class="notice success">
                    Pago completado correctamente en el mock.
                </div>

                <p>
                    El webhook de Payment Resource fue encolado hacia
                    CrinnoPayments.
                </p>

                <p>
                    <strong>Payment Resource:</strong>
                    <code>{html.escape(payment_resource_id)}</code>
                </p>

                <p>
                    <strong>Transaction:</strong>
                    <code>{html.escape(resource["transaction_id"])}</code>
                </p>

                <p>
                    Cuando CrinnoPayments procese el webhook debe ejecutar
                    <code>DELETE /v1/checkout/payment-resources/{html.escape(payment_resource_id)}</code>.
                </p>

                <a class="secondary"
                   href="/ncp/payment/{html.escape(payment_resource_id)}">
                    Refrescar estado del link
                </a>
            </div>
            """,
        )
    )


# =============================================================================
# Diagnostics
# =============================================================================

@app.get("/mock/paypal/state")
async def paypal_mock_state():
    return {
        "public_base_url": PAYPAL_MOCK_PUBLIC_BASE_URL,
        "webhook_url": _webhook_target_url(),
        "webhook_id": PAYPAL_MOCK_WEBHOOK_ID,
        "certificate_url": (
            f"{PAYPAL_MOCK_PUBLIC_BASE_URL}"
            f"/v1/notifications/certs/{_CERTIFICATE_ID}"
        ),
        "legacy_orders": ORDERS_PAYPAL,
        "payment_resources": PAYMENT_RESOURCES,
        "webhook_deliveries": WEBHOOK_DELIVERIES[-50:],
    }


# =============================================================================
# Minimal local checkout UI
# =============================================================================

def _checkout_page(
    *,
    title: str,
    subtitle: str,
    body: str,
) -> str:
    return f"""<!doctype html>
<html lang="es">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(title)}</title>
    <style>
        * {{
            box-sizing: border-box;
        }}
        body {{
            margin: 0;
            min-height: 100vh;
            background: #f5f7fa;
            font-family: Arial, Helvetica, sans-serif;
            color: #18222c;
        }}
        .shell {{
            width: min(680px, calc(100% - 32px));
            margin: 48px auto;
        }}
        .header {{
            margin-bottom: 16px;
        }}
        .header h1 {{
            margin: 0 0 6px;
            font-size: 22px;
        }}
        .header p {{
            margin: 0;
            color: #5b6670;
        }}
        .card {{
            background: #fff;
            border: 1px solid #d9dfe5;
            border-radius: 14px;
            padding: 28px;
            box-shadow: 0 10px 32px rgba(0, 0, 0, .06);
        }}
        .paypal-logo {{
            margin-bottom: 22px;
            font-size: 24px;
            font-weight: 700;
        }}
        .paypal-logo span {{
            margin-left: 7px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: .08em;
            color: #66727d;
        }}
        h2 {{
            font-size: 32px;
            margin: 0 0 22px;
        }}
        code {{
            overflow-wrap: anywhere;
            background: #f1f3f5;
            border-radius: 5px;
            padding: 2px 5px;
        }}
        button,
        .secondary {{
            display: inline-block;
            border-radius: 24px;
            padding: 12px 20px;
            text-decoration: none;
            font-weight: 700;
            cursor: pointer;
        }}
        button.primary {{
            width: 100%;
            margin-top: 16px;
            border: 0;
            background: #0070ba;
            color: #fff;
            font-size: 16px;
        }}
        button.primary:hover {{
            background: #005ea6;
        }}
        .secondary {{
            margin-top: 12px;
            margin-right: 8px;
            border: 1px solid #9ba5ae;
            color: #28343e;
            background: #fff;
        }}
        .notice {{
            margin: 18px 0;
            border-radius: 8px;
            padding: 14px;
        }}
        .notice.success {{
            background: #e9f7ef;
            color: #1f633b;
        }}
        .notice.warning {{
            background: #fff7df;
            color: #775b00;
        }}
        .notice.danger {{
            background: #fdecec;
            color: #8b2424;
        }}
        .items {{
            margin: 18px 0;
            padding-left: 22px;
        }}
        .links {{
            margin-top: 16px;
        }}
    </style>
</head>
<body>
    <main class="shell">
        <div class="header">
            <h1>{html.escape(title)}</h1>
            <p>{html.escape(subtitle)}</p>
        </div>
        {body}
    </main>
</body>
</html>"""


def _not_available_page(
    title: str,
    message: str,
) -> str:
    return _checkout_page(
        title=title,
        subtitle="PayPal local mock",
        body=f"""
        <div class="card">
            <div class="notice danger">
                {html.escape(message)}
            </div>
        </div>
        """,
    )
