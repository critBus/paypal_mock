"""
Mock TropiPay API for local/integration tests.

Usage inside your current project:
    # app.py must expose: app = FastAPI(...)
    # main.py imports this module, or include this file as tropipay/tropipay.py

Env vars:
    MOCK_TROPIPAY_AUTO_WEBHOOK=true|false  # default false
    MOCK_TROPIPAY_WEBHOOK_DELAY_SECONDS=0  # default 0
    MOCK_TROPIPAY_TOKEN_EXPIRES_IN=7200    # default 7200
    MOCK_TROPIPAY_PUBLIC_BASE_URL=http://127.0.0.1:8000  # optional; used to build paymentUrl/shortUrl
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

import httpx
from fastapi import BackgroundTasks, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from app import app


# ==================================================
# Helpers
# ==================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: Optional[datetime] = None) -> str:
    value = dt or utc_now()
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def date_to_iso_datetime(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    # TropiPay accepts YYYY-MM-DD in the create request and returns YYYY-MM-DDT00:00:00.000Z
    if len(value) == 10:
        return f"{value}T00:00:00.000Z"
    if value.endswith("Z"):
        return value
    return value


def as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def stable_signature(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_error_response(error_type: str, error_code: int, message: Optional[str] = None):
    messages = {
        "invalid_request": "Missing or invalid parameters.",
        "invalid_client": "Invalid client credentials.",
        "forbidden": "Unauthorized access.",
        "unauthorized": "Token inválido o expirado.",
        "not_found": "Resource not found.",
        "validation_error": "Validation error.",
    }
    return JSONResponse(
        status_code=error_code,
        content={
            "error": {
                "type": "VALIDATION_ERROR" if error_code < 500 else "SERVER_ERROR",
                "code": error_type.upper(),
                "message": message or messages.get(error_type, "Error desconocido"),
                "details": [],
                "i18n": "Parámetros inválidos",
            }
        },
    )


# ==================================================
# Mock configuration/state
# ==================================================

VALID_CREDENTIALS: Dict[str, Dict[str, Any]] = {
    "client_001": {
        "client_secret": "secret_001",
        "app_name": "App Test 1",
        "user_id": "client_001",
        "credential_id": 140470,
        "account_id": 1024,
        "scopes": [
            "ALLOW_EXTERNAL_CHARGE",
            "ALLOW_CREATE_BENEFICIARY",
            "ALLOW_UPDATE_BENEFICIARY",
            "ALLOW_PAYMENT_IN",
            "ALLOW_PAYMENT_OUT",
            "ALLOW_MARKET_PURCHASES",
            "ALLOW_GET_PROFILE_DATA",
            "ALLOW_GET_BALANCE",
            "ALLOW_GET_MOVEMENT_LIST",
            "ALLOW_GET_POS_MOVEMENT_BY_CREDENTIALS",
        ],
    },
    "client_002": {
        "client_secret": "secret_002",
        "app_name": "App Test 2",
        "user_id": "mock-user-002",
        "credential_id": 240470,
        "account_id": 2024,
        "scopes": ["ALLOW_PAYMENT_IN", "ALLOW_PAYMENT_OUT", "ALLOW_GET_BALANCE"],
    },
    "client_demo": {
        "client_secret": "demo_secret",
        "app_name": "Demo App",
        "user_id": "mock-user-demo",
        "credential_id": 340470,
        "account_id": 3024,
        "scopes": ["ALLOW_PAYMENT_IN"],
    },
}

MERCHANT_EVENTS = [
    {"name": "user_signup", "description": "Event launched once a user completes registration on the TropiPay platform."},
    {"name": "user_login", "description": "Event launched once a user completes login on the TropiPay platform."},
    {"name": "user_kyc", "description": "Event launched once a user completes KYC process."},
    {"name": "payment_in_state_change", "description": "The event is fired once a user changes their status payment in entry method."},
    {"name": "payment_out_state_change", "description": "The event is fired once a user changes their status payment out entry method."},
]

USER_EVENTS = MERCHANT_EVENTS + [
    {"name": "beneficiary_added", "description": "Launched after new beneficiary is created."},
    {"name": "beneficiary_updated", "description": "Launched after a beneficiary is modified."},
    {"name": "beneficiary_deleted", "description": "Launched after a beneficiary is deleted."},
]


class TestState:
    def __init__(self):
        self.force_error = False
        self.error_type = "invalid_client"
        self.error_code = 401
        self.max_error_uses = 1
        self.error_usage_count: Dict[str, int] = {"token": 0, "paymentcards": 0, "webhooks": 0}
        self.active_tokens: Dict[str, Dict[str, Any]] = {}
        self.paymentcards: Dict[str, Dict[str, Any]] = {}
        self.webhook_deliveries: List[Dict[str, Any]] = []
        self.merchant_hooks: List[Dict[str, Any]] = []
        self.user_hooks: List[Dict[str, Any]] = []

    def reset(self):
        self.force_error = False
        self.error_type = "invalid_client"
        self.error_code = 401
        self.max_error_uses = 1
        self.error_usage_count = {"token": 0, "paymentcards": 0, "webhooks": 0}
        self.active_tokens.clear()
        self.paymentcards.clear()
        self.webhook_deliveries.clear()
        self.merchant_hooks.clear()
        self.user_hooks.clear()

    def can_use_error(self, endpoint: str) -> bool:
        return self.force_error and self.error_usage_count.get(endpoint, 0) < self.max_error_uses

    def consume_error(self, endpoint: str):
        self.error_usage_count[endpoint] = self.error_usage_count.get(endpoint, 0) + 1


TEST_STATE = TestState()


# ==================================================
# Pydantic models
# ==================================================

class TokenRequest(BaseModel):
    grant_type: str
    client_id: str
    client_secret: str


class ClientData(BaseModel):
    name: str
    lastName: str
    email: str
    phone: str
    address: str
    countryIso: Optional[str] = None
    countryId: Optional[int] = None
    termsAndConditions: Any = True
    city: Optional[str] = None
    postCode: Optional[str] = None
    state: Optional[str] = None
    dateOfBirth: Optional[str] = None

    @field_validator("termsAndConditions")
    @classmethod
    def must_accept_terms(cls, value: Any) -> bool:
        if value in (True, "true", "True", "1", 1):
            return True
        raise ValueError("termsAndConditions debe ser true")


class PaymentCardCreate(BaseModel):
    concept: str = Field(..., max_length=254)
    description: str
    amount: Any
    currency: Literal["USD", "EUR", "USDC"]
    singleUse: bool
    favorite: bool = False
    reasonId: Optional[int] = 3
    accountId: Optional[int] = None
    reference: Optional[str] = None
    serviceDate: Optional[str] = None
    expirationDate: Optional[str] = None
    expirationDays: Optional[int] = 0
    lang: Optional[str] = "es"
    saveToken: Optional[bool] = False
    directPayment: Optional[bool] = False
    urlSuccess: Optional[str] = None
    urlFailed: Optional[str] = None
    urlNotification: Optional[str] = None
    paymentMethods: Optional[List[str]] = None
    strictPostalCodeCheck: Optional[bool] = False
    strictAddressCheck: Optional[bool] = False
    paymentcardType: Optional[Any] = 4
    payment3DS: Optional[Any] = 1
    client: Optional[ClientData] = None

    @field_validator("amount")
    @classmethod
    def amount_must_be_int_and_min_100(cls, value: Any) -> int:
        amount = as_int(value)
        if amount is None or amount < 100:
            raise ValueError("amount debe ser un entero >= 100")
        return amount


class HookPayload(BaseModel):
    event: str
    target: Literal["web", "email"]
    value: str


class HookDeletePayload(BaseModel):
    event: str


class ForceErrorRequest(BaseModel):
    force_error: bool
    error_type: Optional[str] = "invalid_client"
    error_code: Optional[int] = 401
    max_uses: Optional[int] = 1


class SendWebhookRequest(BaseModel):
    paymentcard_id: Optional[str] = None
    reference: Optional[str] = None
    url: Optional[str] = None
    status: Literal["OK", "FAILED"] = "OK"
    state: int = 5


# ==================================================
# Auth
# ==================================================

async def require_token(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization Bearer token")
    token = authorization.replace("Bearer ", "", 1).strip()
    token_data = TEST_STATE.active_tokens.get(token)
    if not token_data:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")
    if token_data["expires_at"] < utc_now():
        TEST_STATE.active_tokens.pop(token, None)
        raise HTTPException(status_code=401, detail="Token expirado")
    return token_data


# ==================================================
# Access token endpoint
# ==================================================

@app.post("/api/v3/access/token")
async def get_access_token(request: TokenRequest):
    if TEST_STATE.can_use_error("token"):
        TEST_STATE.consume_error("token")
        return create_error_response(TEST_STATE.error_type, TEST_STATE.error_code)

    if request.grant_type != "client_credentials":
        return create_error_response("invalid_request", 400, "grant_type debe ser 'client_credentials'")

    client_data = VALID_CREDENTIALS.get(request.client_id)
    if not client_data or client_data["client_secret"] != request.client_secret:
        return create_error_response("invalid_client", 401, "Invalid client credentials.")

    expires_in = int(os.getenv("MOCK_TROPIPAY_TOKEN_EXPIRES_IN", "7200"))
    access_token = f"mock_access_{uuid.uuid4().hex}"
    refresh_token = f"mock_refresh_{uuid.uuid4().hex}"
    TEST_STATE.active_tokens[access_token] = {
        "client_id": request.client_id,
        "app_name": client_data["app_name"],
        "user_id": client_data["user_id"],
        "credential_id": client_data["credential_id"],
        "account_id": client_data["account_id"],
        "scopes": client_data["scopes"],
        "created_at": utc_now(),
        "expires_at": utc_now() + timedelta(seconds=expires_in),
    }

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": " ".join(client_data["scopes"]),
    }


# ==================================================
# Payment cards
# ==================================================

def get_mock_base_url(request: Request) -> str:
    configured = os.getenv("MOCK_TROPIPAY_PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def build_paymentcard_response(payload: PaymentCardCreate, token_data: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    now = utc_now()
    card_id = str(uuid.uuid1())
    suffix = uuid.uuid4().hex[:8]
    #payment_page_url = f"{base_url}/pay/{card_id}"
    payment_page_url = f"http://127.0.0.1:7000/pay/{card_id}"
    amount = int(payload.amount)
    service_date = date_to_iso_datetime(payload.serviceDate) or f"{now.date().isoformat()}T00:00:00.000Z"
    expiration_date = date_to_iso_datetime(payload.expirationDate)
    paymentcard_type = as_int(payload.paymentcardType, 4)

    response = {
        "id": card_id,
        "lang": payload.lang or "es",
        "state": 1,
        "amount": amount,
        "origin": 2,
        "userId": token_data["user_id"],
        "concept": payload.concept,
        "qrImage": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lxjH5wAAAABJRU5ErkJggg==",
        "currency": payload.currency,
        "favorite": payload.favorite,
        "force3ds": payload.payment3DS in ("force", 1, "1"),
        "giftcard": None,
        "reasonId": payload.reasonId,
        "shortUrl": payment_page_url,
        "accountId": payload.accountId or token_data["account_id"],
        "createdAt": iso_z(now),
        "hasClient": payload.client is not None,
        "imageBase": None,
        "reasonDes": None,
        "reference": payload.reference or f"S{uuid.uuid4().hex[:8].upper()}",
        "saveToken": payload.saveToken,
        "singleUse": payload.singleUse,
        "updatedAt": iso_z(now + timedelta(seconds=1)),
        "urlFailed": payload.urlFailed,
        "payment3DS": 1,
        "paymentUrl": payment_page_url,
        "urlSuccess": payload.urlSuccess,
        "description": payload.description,
        "serviceDate": service_date,
        "credentialId": token_data["credential_id"],
        "bankOrderCode": str(uuid.uuid4().int)[:12],
        "rawUrlPayment": f"{base_url}/pay/{card_id}/process?lang={payload.lang or 'es'}",
        "expirationDate": expiration_date,
        "expirationDays": payload.expirationDays or 0,
        "paymentcardType": paymentcard_type,
        "urlNotification": payload.urlNotification,
        "strictAddressCheck": payload.strictAddressCheck,
        "destinationCurrency": "EUR" if payload.currency == "USD" else payload.currency,
        "strictPostalCodeCheck": payload.strictPostalCodeCheck,
    }
    if payload.client:
        response["client"] = payload.client.model_dump()
    return response


@app.post("/api/v3/paymentcards")
async def create_payment_card(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(None),
):
    token_data = await require_token(authorization)

    if TEST_STATE.can_use_error("paymentcards"):
        TEST_STATE.consume_error("paymentcards")
        return create_error_response(TEST_STATE.error_type, TEST_STATE.error_code)

    try:
        payload = PaymentCardCreate.model_validate(await request.json())
    except Exception as exc:
        return create_error_response("validation_error", 400, str(exc))

    if payload.singleUse and not payload.reference:
        return create_error_response("validation_error", 400, "reference es requerido cuando singleUse=true")
    if payload.singleUse and not payload.serviceDate:
        return create_error_response("validation_error", 400, "serviceDate es requerido cuando singleUse=true")
    if payload.singleUse and not payload.client:
        return create_error_response("validation_error", 400, "client es requerido cuando singleUse=true")

    card = build_paymentcard_response(payload, token_data, get_mock_base_url(request))
    TEST_STATE.paymentcards[card["id"]] = card

    auto_webhook = os.getenv("MOCK_TROPIPAY_AUTO_WEBHOOK", "false").lower() in ("1", "true", "yes")
    if auto_webhook and card.get("urlNotification"):
        background_tasks.add_task(send_tropipay_webhook, card, card["urlNotification"], "OK", 5)

    return card


@app.get("/api/v3/paymentcards")
async def list_payment_cards(
    authorization: Optional[str] = Header(None),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    state: Optional[int] = Query(None),
):
    await require_token(authorization)
    cards = list(TEST_STATE.paymentcards.values())
    if state is not None:
        cards = [card for card in cards if card.get("state") == state]
    return cards[offset : offset + limit]


@app.get("/api/v3/paymentcards/{paymentcard_id}")
async def get_payment_card(paymentcard_id: str, authorization: Optional[str] = Header(None)):
    await require_token(authorization)
    card = TEST_STATE.paymentcards.get(paymentcard_id)
    if not card:
        return create_error_response("not_found", 404, "Payment card not found")
    return card


@app.get("/pay/{paymentcard_id}", response_class=HTMLResponse)
async def show_mock_payment_page(paymentcard_id: str):
    card = TEST_STATE.paymentcards.get(paymentcard_id)
    if not card:
        raise HTTPException(status_code=404, detail="Payment card not found")

    amount = int(card.get("amount", 0)) / 100
    currency = card.get("currency", "USD")
    concept = card.get("concept") or card.get("description") or "Pago TropiPay Mock"
    reference = card.get("reference") or "-"
    notification_url = card.get("urlNotification") or "No configurada"

    html = f"""
    <!doctype html>
    <html lang="es">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Mock TropiPay - Pagar {reference}</title>
        <style>
          body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#f5f7fb; margin:0; padding:32px; color:#172033; }}
          .card {{ max-width: 560px; margin: 0 auto; background:white; border-radius:18px; box-shadow: 0 18px 45px rgba(15, 23, 42, .12); padding:28px; }}
          .badge {{ display:inline-block; padding:6px 10px; border-radius:999px; background:#e8f2ff; color:#075985; font-size:13px; font-weight:700; }}
          h1 {{ margin:18px 0 8px; font-size:28px; }}
          .amount {{ font-size:42px; font-weight:800; margin:22px 0; }}
          .muted {{ color:#64748b; line-height:1.5; }}
          .row {{ border-top:1px solid #e2e8f0; padding:12px 0; }}
          .label {{ color:#64748b; font-size:13px; }}
          .value {{ word-break:break-all; margin-top:4px; }}
          button {{ width:100%; border:0; border-radius:14px; padding:15px 18px; background:#16a34a; color:white; font-size:18px; font-weight:800; cursor:pointer; margin-top:20px; }}
          button:hover {{ background:#15803d; }}
          .danger button {{ background:#dc2626; margin-top:10px; }}
          .danger button:hover {{ background:#b91c1c; }}
        </style>
      </head>
      <body>
        <main class="card">
          <span class="badge">TropiPay Mock</span>
          <h1>Confirmar pago</h1>
          <p class="muted">Esta página simula el checkout de TropiPay. Al presionar pagar, el mock enviará el webhook al <code>urlNotification</code> recibido en <code>POST /api/v3/paymentcards</code>.</p>
          <div class="amount">{amount:,.2f} {currency}</div>
          <div class="row"><div class="label">Concepto</div><div class="value">{concept}</div></div>
          <div class="row"><div class="label">Referencia</div><div class="value">{reference}</div></div>
          <div class="row"><div class="label">Webhook destino</div><div class="value">{notification_url}</div></div>
          <form method="post" action="/pay/{paymentcard_id}/pay">
            <button type="submit">Pagar y enviar webhook OK</button>
          </form>
          <form class="danger" method="post" action="/pay/{paymentcard_id}/fail">
            <button type="submit">Simular pago fallido</button>
          </form>
        </main>
      </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/pay/{paymentcard_id}/process", response_class=HTMLResponse)
async def show_mock_payment_process(paymentcard_id: str):
    return await show_mock_payment_page(paymentcard_id)


async def complete_mock_payment(paymentcard_id: str, status: Literal["OK", "FAILED"], state: int) -> HTMLResponse:
    card = TEST_STATE.paymentcards.get(paymentcard_id)
    if not card:
        raise HTTPException(status_code=404, detail="Payment card not found")

    target_url = card.get("urlNotification")
    if not target_url:
        return HTMLResponse(
            content="<h1>No hay urlNotification configurada</h1><p>No se pudo enviar el webhook porque el paymentcard no tiene urlNotification.</p>",
            status_code=400,
        )

    card["state"] = 2 if status == "OK" else 4
    card["updatedAt"] = iso_z()
    delivery = await send_tropipay_webhook(card, target_url, status=status, state=state)

    title = "Pago simulado enviado" if delivery["success"] else "Pago simulado, pero falló el webhook"
    color = "#16a34a" if delivery["success"] else "#dc2626"
    html = f"""
    <!doctype html>
    <html lang="es">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{title}</title>
        <style>
          body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#f5f7fb; margin:0; padding:32px; color:#172033; }}
          .card {{ max-width: 680px; margin: 0 auto; background:white; border-radius:18px; box-shadow: 0 18px 45px rgba(15, 23, 42, .12); padding:28px; }}
          h1 {{ color:{color}; }}
          pre {{ white-space:pre-wrap; word-break:break-word; background:#0f172a; color:#e2e8f0; padding:16px; border-radius:12px; overflow:auto; }}
          a {{ color:#2563eb; }}
        </style>
      </head>
      <body>
        <main class="card">
          <h1>{title}</h1>
          <p><strong>PaymentCard:</strong> {paymentcard_id}</p>
          <p><strong>Webhook destino:</strong> {target_url}</p>
          <p><strong>Status HTTP del webhook:</strong> {delivery.get("status_code")}</p>
          <p><strong>Respuesta:</strong></p>
          <pre>{delivery.get("response")}</pre>
          <p><a href="/pay/{paymentcard_id}">Volver al checkout mock</a></p>
        </main>
      </body>
    </html>
    """
    return HTMLResponse(content=html, status_code=200 if delivery["success"] else 502)


@app.post("/pay/{paymentcard_id}/pay", response_class=HTMLResponse)
async def pay_mock_payment(paymentcard_id: str):
    return await complete_mock_payment(paymentcard_id, status="OK", state=5)


@app.post("/pay/{paymentcard_id}/fail", response_class=HTMLResponse)
async def fail_mock_payment(paymentcard_id: str):
    return await complete_mock_payment(paymentcard_id, status="FAILED", state=4)


# ==================================================
# TropiPay hooks management endpoints
# ==================================================

def upsert_hook(storage: List[Dict[str, Any]], payload: HookPayload, action: str) -> Dict[str, Any]:
    now = iso_z()
    existing = next((hook for hook in storage if hook["event"] == payload.event), None)
    if existing:
        existing.update({"target": payload.target, "value": payload.value, "updatedAt": now})
        return {"action": "update", "status": "success", "details": payload.event}
    storage.append({"event": payload.event, "target": payload.target, "value": payload.value, "createdAt": now, "updatedAt": now})
    return {"action": action, "status": "success", "details": payload.event}


def delete_hook(storage: List[Dict[str, Any]], event: str) -> Dict[str, Any]:
    storage[:] = [hook for hook in storage if hook["event"] != event]
    return {"action": "update", "status": "success", "details": event}


@app.get("/api/v3/merchant/hooks")
async def list_merchant_hooks(authorization: Optional[str] = Header(None)):
    await require_token(authorization)
    return TEST_STATE.merchant_hooks


@app.post("/api/v3/merchant/hooks")
async def subscribe_merchant_hook(payload: HookPayload, authorization: Optional[str] = Header(None)):
    await require_token(authorization)
    return upsert_hook(TEST_STATE.merchant_hooks, payload, "subscribe")


@app.put("/api/v3/merchant/hooks")
async def update_merchant_hook(payload: HookPayload, authorization: Optional[str] = Header(None)):
    await require_token(authorization)
    return upsert_hook(TEST_STATE.merchant_hooks, payload, "update")


@app.delete("/api/v3/merchant/hooks")
async def delete_merchant_hook(payload: HookDeletePayload, authorization: Optional[str] = Header(None)):
    await require_token(authorization)
    return delete_hook(TEST_STATE.merchant_hooks, payload.event)


@app.get("/api/v3/merchant/hooks/events")
async def merchant_hook_events(authorization: Optional[str] = Header(None)):
    await require_token(authorization)
    return MERCHANT_EVENTS


@app.get("/api/v3/user/hooks")
async def list_user_hooks(authorization: Optional[str] = Header(None)):
    await require_token(authorization)
    return TEST_STATE.user_hooks


@app.post("/api/v3/user/hooks")
async def subscribe_user_hook(payload: HookPayload, authorization: Optional[str] = Header(None)):
    await require_token(authorization)
    return upsert_hook(TEST_STATE.user_hooks, payload, "subscribe")


@app.put("/api/v3/user/hooks")
async def update_user_hook(payload: HookPayload, authorization: Optional[str] = Header(None)):
    await require_token(authorization)
    return upsert_hook(TEST_STATE.user_hooks, payload, "update")


@app.delete("/api/v3/user/hooks")
async def delete_user_hook(payload: HookDeletePayload, authorization: Optional[str] = Header(None)):
    await require_token(authorization)
    return delete_hook(TEST_STATE.user_hooks, payload.event)


@app.get("/api/v3/user/hooks/events")
async def user_hook_events(authorization: Optional[str] = Header(None)):
    await require_token(authorization)
    return USER_EVENTS


@app.get("/api/v3/user/hooks/{event_name}")
async def get_user_hooks_by_event(event_name: str, authorization: Optional[str] = Header(None)):
    await require_token(authorization)
    return [hook for hook in TEST_STATE.user_hooks if hook["event"] == event_name]


# ==================================================
# Webhook payload simulator
# ==================================================

def build_payment_webhook(card: Dict[str, Any], status: str = "OK", state: int = 5) -> Dict[str, Any]:
    now = iso_z()
    transaction_id = int(str(uuid.uuid4().int)[:7])
    paid_amount = max(100, int(card["amount"] * 0.891))
    signature = stable_signature(card["id"], card.get("reference"), paid_amount, state)
    data = {
        "id": int(str(uuid.uuid4().int)[:6]),
        "ip": "216.147.125.230",
        "days": None,
        "agent": "TROPIPAY",
        "state": state,
        "amount": paid_amount,
        "ourFee": None,
        "userId": card["userId"],
        "currency": card["currency"],
        "provider": 4,
        "reasonId": None,
        "riskFlag": 0,
        "createdAt": now,
        "reasonDes": None,
        "reference": card.get("reference"),
        "riskScore": 0,
        "serviceId": 250,
        "signature": signature,
        "updatedAt": now,
        "clientData": {
            "clientName": (card.get("client") or {}).get("name", "TestDev"),
            "clientEmail": (card.get("client") or {}).get("email", "testdev@mailinator.com"),
            "clientAddress": (card.get("client") or {}).get("address", "Santa ifigenia"),
            "clientLastName": (card.get("client") or {}).get("lastName", "dev"),
        },
        "isInternal": True,
        "bookingDate": None,
        "cardTokenId": None,
        "errorReason": None if status == "OK" else "Mock payment failed",
        "paymentcard": {
            key: card.get(key)
            for key in [
                "id", "lang", "state", "amount", "userId", "concept", "qrImage", "currency", "favorite", "reasonId",
                "shortUrl", "hasClient", "imageBase", "reasonDes", "reference", "saveToken", "singleUse", "urlFailed",
                "paymentUrl", "urlSuccess", "description", "serviceDate", "expirationDate", "expirationDays", "urlNotification",
            ]
        },
        "providerFee": None,
        "signaturev2": stable_signature(signature, "v2"),
        "signaturev3": stable_signature(signature, "v3"),
        "bankOrderCode": f"TX{uuid.uuid4().int}"[:18],
        "paymentcardId": card["id"],
        "transactionId": transaction_id,
        "conversionRate": 1.12,
        "expirationDate": card.get("expirationDate"),
        "movementTypeId": 5,
        "notificationUrl": card.get("urlNotification"),
        "depositaccountId": None,
        "destinationAmount": str(int(paid_amount * 0.99)),
        "destinationCurrency": card.get("destinationCurrency", "EUR"),
        "originalCurrencyAmount": str(card["amount"]),
    }
    return {"data": data, "status": status}


async def send_tropipay_webhook(card: Dict[str, Any], target_url: str, status: str = "OK", state: int = 5) -> Dict[str, Any]:
    delay = float(os.getenv("MOCK_TROPIPAY_WEBHOOK_DELAY_SECONDS", "0"))
    if delay > 0:
        await asyncio.sleep(delay)

    payload = build_payment_webhook(card, status=status, state=state)
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Mock-TropiPay/1.0"}
    delivery = {"target_url": target_url, "payload": payload, "createdAt": iso_z(), "status_code": None, "response": None, "success": False}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(target_url, json=payload, headers=headers)
        delivery.update({"status_code": response.status_code, "response": response.text[:1000], "success": 200 <= response.status_code < 300})
    except Exception as exc:
        delivery.update({"response": str(exc), "success": False})

    TEST_STATE.webhook_deliveries.append(delivery)
    return delivery


@app.post("/admin/tropipay/send-webhook")
async def admin_send_webhook(payload: SendWebhookRequest, background_tasks: BackgroundTasks):
    card = None
    if payload.paymentcard_id:
        card = TEST_STATE.paymentcards.get(payload.paymentcard_id)
    elif payload.reference:
        card = next((item for item in TEST_STATE.paymentcards.values() if item.get("reference") == payload.reference), None)
    else:
        card = next(reversed(TEST_STATE.paymentcards.values()), None) if TEST_STATE.paymentcards else None

    if not card:
        return create_error_response("not_found", 404, "Payment card not found. Create a payment card first.")

    target_url = payload.url or card.get("urlNotification")
    if not target_url:
        return create_error_response("validation_error", 400, "No hay urlNotification ni url explícita para enviar el webhook")

    background_tasks.add_task(send_tropipay_webhook, card, target_url, payload.status, payload.state)
    return {"status": "queued", "paymentcardId": card["id"], "target_url": target_url}


@app.get("/admin/tropipay/webhook-deliveries")
async def admin_list_webhook_deliveries():
    return TEST_STATE.webhook_deliveries


@app.post("/admin/test-control")
async def update_test_control(payload: ForceErrorRequest):
    TEST_STATE.force_error = payload.force_error
    TEST_STATE.error_type = payload.error_type or "invalid_client"
    TEST_STATE.error_code = payload.error_code or 401
    TEST_STATE.max_error_uses = payload.max_uses or 1
    if TEST_STATE.force_error:
        TEST_STATE.error_usage_count = {"token": 0, "paymentcards": 0, "webhooks": 0}
    return {
        "status": "ok",
        "force_error": TEST_STATE.force_error,
        "error_type": TEST_STATE.error_type,
        "error_code": TEST_STATE.error_code,
        "max_uses": TEST_STATE.max_error_uses,
        "error_usage": TEST_STATE.error_usage_count,
    }


@app.get("/admin/test-control/status")
async def get_test_status():
    return {
        "force_error": TEST_STATE.force_error,
        "error_type": TEST_STATE.error_type,
        "error_code": TEST_STATE.error_code,
        "active_tokens": len(TEST_STATE.active_tokens),
        "valid_accounts": len(VALID_CREDENTIALS),
        "paymentcards": len(TEST_STATE.paymentcards),
        "webhook_deliveries": len(TEST_STATE.webhook_deliveries),
        "error_usage": TEST_STATE.error_usage_count,
        "max_uses": TEST_STATE.max_error_uses,
    }


@app.post("/admin/test-control/reset")
async def reset_test_state():
    TEST_STATE.reset()
    return {"status": "ok", "message": "Estado reseteado"}
