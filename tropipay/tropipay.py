"""
Mock TropiPay API for local/integration tests.

Usage inside your current project:
    # app.py must expose: app = FastAPI(...)
    # main.py imports this module, or include this file as tropipay/tropipay.py

Env vars:
    MOCK_TROPIPAY_AUTO_WEBHOOK=true|false  # default false
    MOCK_TROPIPAY_WEBHOOK_DELAY_SECONDS=0  # default 0
    MOCK_TROPIPAY_TOKEN_EXPIRES_IN=7200    # default 7200
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

import httpx
from fastapi import BackgroundTasks, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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


def canonical_json_bytes(payload: Dict[str, Any]) -> bytes:
    """JSON bytes used for webhook delivery and HMAC signature calculation."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def hmac_sha256_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def get_public_base_url(request: Request) -> str:
    configured = os.getenv("MOCK_TROPIPAY_PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def get_webhook_secret_for_client(client_id: Optional[str]) -> str:
    """Resolve the webhook secret used to sign X-Tropipay-Signature.

    For local tests you can either:
    - set MOCK_TROPIPAY_WEBHOOK_SECRET, or
    - set webhook_secret in VALID_CREDENTIALS for the client.

    Since your test credentials are client_001 / secret_001, the default
    webhook_secret for client_001 is also secret_001.
    """
    env_secret = os.getenv("MOCK_TROPIPAY_WEBHOOK_SECRET")
    if env_secret:
        return env_secret
    if client_id and client_id in VALID_CREDENTIALS:
        client_data = VALID_CREDENTIALS[client_id]
        return str(client_data.get("webhook_secret") or client_data["client_secret"])
    return "secret_001"


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
        "webhook_secret": "secret_001",
        "app_name": "App Test 1",
        "user_id": "1f794e90-1e3f-11ed-ba16-31e0d53105ea",
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
        "webhook_secret": "secret_002",
        "app_name": "App Test 2",
        "user_id": "mock-user-002",
        "credential_id": 240470,
        "account_id": 2024,
        "scopes": ["ALLOW_PAYMENT_IN", "ALLOW_PAYMENT_OUT", "ALLOW_GET_BALANCE"],
    },
    "client_demo": {
        "client_secret": "demo_secret",
        "webhook_secret": "demo_secret",
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
        self.paymentcard_meta: Dict[str, Dict[str, Any]] = {}
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
        self.paymentcard_meta.clear()
        self.webhook_deliveries.clear()
        self.merchant_hooks.clear()
        self.user_hooks.clear()

    def can_use_error(self, endpoint: str) -> bool:
        return self.force_error and self.error_usage_count.get(endpoint, 0) < self.max_error_uses

    def consume_error(self, endpoint: str):
        self.error_usage_count[endpoint] = self.error_usage_count.get(endpoint, 0) + 1


TEST_STATE = TestState()


TROPIPAY_CARD_EXAMPLES = {
    "MASTERCARD": {"cardBin": "552433", "cardCategory": "CREDIT", "cardCountry": "US"},
    "VISA": {"cardBin": "411111", "cardCategory": "DEBIT", "cardCountry": "US"},
    "AMERICAN EXPRESS": {"cardBin": "378282", "cardCategory": "CREDIT", "cardCountry": "US"},
}

TROPIPAY_BANK_EXAMPLES = [
    "BANK OF AMERICA NATIONAL ASSO",
    "JPMORGAN CHASE BANK, N.A.",
    "WELLS FARGO BANK, N.A.",
    "CITIBANK N.A.",
]

TROPIPAY_ERROR_EXAMPLES = [
    "60022:Unauthenticated",
    "89:Security Violation",
    "05:Do not honor",
    "51:Insufficient funds",
    "54:Expired card",
]

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
    status: Literal["OK", "KO", "FAILED"] = "OK"
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
        "webhook_secret": client_data.get("webhook_secret") or client_data["client_secret"],
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
import secrets
import string

def random_query_param(length: int) -> str:
    if length < 0:
        raise ValueError("length debe ser mayor o igual a 0")

    chars = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(chars) for _ in range(length))

def build_paymentcard_response(payload: PaymentCardCreate, token_data: Dict[str, Any], request: Request) -> Dict[str, Any]:
    now = utc_now()
    card_id = str(uuid.uuid1())
    suffix = uuid.uuid4().hex[:8]
    amount = int(payload.amount)
    service_date = date_to_iso_datetime(payload.serviceDate) or f"{now.date().isoformat()}T00:00:00.000Z"
    expiration_date = date_to_iso_datetime(payload.expirationDate)
    paymentcard_type = as_int(payload.paymentcardType, 4)
    #base_url = get_public_base_url(request)
    #payment_url = f"{base_url}/pay/{card_id}"
    extra_long_param=random_query_param(1600)
    base_payment =  f"http://127.0.0.1:7000/pay/{card_id}"
    payment_url =f"{base_payment}?extra_long_param={extra_long_param}"

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
        "shortUrl": payment_url,
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
        "paymentUrl": payment_url,
        "urlSuccess": payload.urlSuccess,
        "description": payload.description,
        "serviceDate": service_date,
        "credentialId": token_data["credential_id"],
        "bankOrderCode": str(uuid.uuid4().int)[:12],
        "rawUrlPayment": f"{base_payment}/process?lang={payload.lang or 'es'}",
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

    card = build_paymentcard_response(payload, token_data, request)
    TEST_STATE.paymentcards[card["id"]] = card
    TEST_STATE.paymentcard_meta[card["id"]] = {
        "client_id": token_data.get("client_id"),
        "webhook_secret": token_data.get("webhook_secret") or get_webhook_secret_for_client(token_data.get("client_id")),
        "charges": [],
    }

    auto_webhook = os.getenv("MOCK_TROPIPAY_AUTO_WEBHOOK", "false").lower() in ("1", "true", "yes")
    if auto_webhook and card.get("urlNotification"):
        background_tasks.add_task(send_tropipay_webhook, card, card["urlNotification"], "OK", 3)

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


def _clean_failure_value(value: str, default: str) -> str:
    cleaned = " ".join((value or "").split())
    return cleaned[:254] or default


def build_failed_charge(
    card: Dict[str, Any],
    card_brand: str,
    bank: str,
    card_pan: str,
    error_reason: str,
) -> Dict[str, Any]:
    """Build one failed card attempt using the fields emitted by TropiPay."""

    now = iso_z()
    brand = _clean_failure_value(card_brand, "MASTERCARD").upper()
    profile = TROPIPAY_CARD_EXAMPLES.get(brand, TROPIPAY_CARD_EXAMPLES["MASTERCARD"])
    pan = "".join(character for character in str(card_pan) if character.isdigit())[-4:] or "6699"
    client = card.get("client") or {}
    amount = int(card.get("amount") or 0)
    charge_id = int(str(uuid.uuid4().int)[:7])

    return {
        "aft": None,
        "amount": amount,
        "bank": _clean_failure_value(bank, TROPIPAY_BANK_EXAMPLES[0]),
        "bookingId": str(charge_id),
        "browserAcceptHeader": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "browserColorDepth": "24",
        "browserIP": "127.0.0.1",
        "browserJavaEnabled": "false",
        "browserJavascriptEnabled": "true",
        "browserLanguage": "es",
        "browserScreenHeight": "1080",
        "browserScreenWidth": "1920",
        "browserTZ": "240",
        "browserUserAgent": "Mock-TropiPay/1.0",
        "cardBin": profile["cardBin"],
        "cardBrand": brand,
        "cardCategory": profile["cardCategory"],
        "cardCountry": profile["cardCountry"],
        "cardExpirationDate": "12/29",
        "cardHolderName": f"{client.get('name', 'Test')} {client.get('lastName', 'User')}".strip(),
        "cardPan": pan,
        "clientAddress": client.get("address", "Test address"),
        "clientCity": client.get("city", "Miami"),
        "clientCountryId": client.get("countryId", 840),
        "clientEmail": client.get("email", "test@example.com"),
        "clientIp": "127.0.0.1",
        "clientLastName": client.get("lastName", "User"),
        "clientName": client.get("name", "Test"),
        "clientPhone": client.get("phone", "+10000000000"),
        "clientState": client.get("state", "FL"),
        "clientTC": True,
        "createdAt": now,
        "currency": card.get("currency", "USD"),
        "errorCode": _clean_failure_value(error_reason, TROPIPAY_ERROR_EXAMPLES[0]).split(":", 1)[0],
        "errorReason": _clean_failure_value(error_reason, TROPIPAY_ERROR_EXAMPLES[0]),
        "id": charge_id,
        "orderCode": card.get("reference"),
        "riskScore": 0,
        "saveToken": False,
        "securityCheckAddress": None,
        "securityCheckPostCode": None,
        "serviceId": 2,
        "state": 4,
        "updatedAt": now,
        "userId": card.get("userId"),
    }


def record_failed_charge(
    card: Dict[str, Any],
    card_brand: str,
    bank: str,
    card_pan: str,
    error_reason: str,
) -> Dict[str, Any]:
    charge = build_failed_charge(card, card_brand, bank, card_pan, error_reason)
    meta = TEST_STATE.paymentcard_meta.setdefault(card["id"], {})
    meta.setdefault("charges", []).append(charge)
    return charge


def build_payment_webhook(card: Dict[str, Any], status: str = "OK", state: int = 3) -> Dict[str, Any]:
    now = iso_z()
    transaction_id = int(str(uuid.uuid4().int)[:7])
    paid_amount = max(100, int(card["amount"] * 0.891))
    originalCurrencyAmount=str(card["amount"])
    meta = TEST_STATE.paymentcard_meta.get(card["id"], {})
    charges = list(meta.get("charges") or [])
    latest_failure = charges[-1] if charges else {}
    bankOrderCode= f"TX{uuid.uuid4().int}"[:18]
    signature=get_signature(
    bankOrderCode=bankOrderCode,
    client_id=meta.get("client_id"),
    client_secret=meta.get("webhook_secret"),
    originalCurrencyAmount=originalCurrencyAmount,
    )

    # signature = stable_signature(card["id"], card.get("reference"), paid_amount, state)
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
        "isInternal": False,
        "bookingDate": None,
        "charges": charges,
        "cardTokenId": None,
        "errorReason": None if status == "OK" else latest_failure.get("errorReason", TROPIPAY_ERROR_EXAMPLES[0]),
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
        "bankOrderCode":bankOrderCode,
        "paymentcardId": card["id"],
        "transactionId": transaction_id,
        "conversionRate": 1.12,
        "expirationDate": card.get("expirationDate"),
        "movementTypeId": 5,
        "notificationUrl": card.get("urlNotification"),
        "depositaccountId": None,
        "destinationAmount": str(int(paid_amount * 0.99)),
        "destinationCurrency": card.get("destinationCurrency", "EUR"),
        "originalCurrencyAmount": originalCurrencyAmount,
    }
    payload = {"data": data, "status": status}
    include_event = os.getenv("MOCK_TROPIPAY_WEBHOOK_INCLUDE_EVENT", "true").lower() in ("1", "true", "yes")
    if include_event:
        payload = {
            "event": "payment.completed" if status == "OK" else "payment.failed",
            "data": data,
            "status": status,
            "timestamp": now,
        }
    return payload

from hashlib import sha256 as encode_sha256
def get_signature(bankOrderCode,client_id,client_secret,originalCurrencyAmount):
    
    # print(f"0!!!! bankOrderCode {bankOrderCode}")
    # print(f"0!!!! client_id {client_id}")
    # print(f"0!!!! client_secret {client_secret}")
    # print(f"0!!!! originalCurrencyAmount {originalCurrencyAmount}")
    payload_to_sign = (
        f"{bankOrderCode}{client_id}"
        f"{client_secret}{originalCurrencyAmount}"
    )
    print(f"0!!!! payload_to_sign {payload_to_sign}")
    system_signature = encode_sha256(payload_to_sign.encode("utf-8")).hexdigest()
    print(f"0!!!! system_signature {system_signature}")
    return system_signature

async def send_tropipay_webhook(card: Dict[str, Any], target_url: str, status: str = "OK", state: int = 3) -> Dict[str, Any]:
    print("entro a send_tropipay_webhook !!!!!!!!!!!")
    delay = float(os.getenv("MOCK_TROPIPAY_WEBHOOK_DELAY_SECONDS", "0"))
    if delay > 0:
        await asyncio.sleep(delay)

    payload = build_payment_webhook(card, status=status, state=state)
    body = canonical_json_bytes(payload)
    meta = TEST_STATE.paymentcard_meta.get(card["id"], {})
    webhook_secret = str(meta.get("webhook_secret") or get_webhook_secret_for_client(meta.get("client_id")))

    data=payload.get("data")
    system_signature=get_signature(
    bankOrderCode=data.get('bankOrderCode'),
    client_id=meta.get("client_id"),
    client_secret=meta.get("webhook_secret"),
    originalCurrencyAmount=data.get('originalCurrencyAmount'),
    )
    
    # bankOrderCode=data.get('bankOrderCode')
    # client_id=meta.get("client_id")
    # client_secret=meta.get("webhook_secret")
    # originalCurrencyAmount=data.get('originalCurrencyAmount')
    # print(f"0!!!! bankOrderCode {bankOrderCode}")
    # print(f"0!!!! client_id {client_id}")
    # print(f"0!!!! client_secret {client_secret}")
    # print(f"0!!!! originalCurrencyAmount {originalCurrencyAmount}")
    # payload_to_sign = (
    #     f"{bankOrderCode}{client_id}"
    #     f"{client_secret}{originalCurrencyAmount}"
    # )
    # print(f"0!!!! payload_to_sign {payload_to_sign}")
    # system_signature = encode_sha256(payload_to_sign.encode("utf-8")).hexdigest()
    # print(f"0!!!! system_signature {system_signature}")
    
    # signature = hmac_sha256_hex(webhook_secret, body)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mock-TropiPay/1.0",
        "X-Tropipay-Signature": system_signature,
    }
    payload["data"]["signature"]=system_signature
    payload["data"]["signaturev2"]=system_signature
    payload["data"]["signaturev3"]=system_signature
    if payload.get("event"):
        headers["X-Tropipay-Event"] = str(payload["event"])
    delivery = {
        "target_url": target_url,
        "payload": payload,
        "signature": system_signature,
        "signature_header": "X-Tropipay-Signature",
        "webhook_secret_used": webhook_secret,
        "createdAt": iso_z(),
        "status_code": None,
        "response": None,
        "success": False,
    }
    try:
        print("se va a intentar comunicar con crinnopayments")
        async with httpx.AsyncClient(timeout=30.0) as client:
            print(f"target_url {target_url} !!!")
            response = await client.post(target_url, content=body, headers=headers)
            print("paso la comunicacion !!!")
        delivery.update({"status_code": response.status_code, "response": response.text[:1000], "success": 200 <= response.status_code < 300})
    except Exception as exc:
        print({"response": str(exc), "success": False})
        delivery.update({"response": str(exc), "success": False})

    TEST_STATE.webhook_deliveries.append(delivery)
    return delivery



# ==================================================
# Local payment page
# ==================================================

def render_payment_page(card: Dict[str, Any], result: Optional[str] = None) -> str:
    amount = card.get("amount")
    currency = html.escape(str(card.get("currency", "")))
    concept = html.escape(str(card.get("concept", "")))
    reference = html.escape(str(card.get("reference", "")))
    webhook_url = html.escape(str(card.get("urlNotification") or "No configurado"))
    paymentcard_id = html.escape(str(card["id"]))
    attempts = TEST_STATE.paymentcard_meta.get(card["id"], {}).get("charges", [])

    status_block = ""
    if result == "ok":
        status_block = '<div class="notice ok">Pago simulado correctamente. El webhook de pago fue enviado.</div>'
    elif result == "failed":
        status_block = (
            '<div class="notice fail">Intento fallido añadido y webhook enviado. '
            'Puedes repetir el formulario con otra tarjeta.</div>'
        )
    elif result == "no_webhook":
        status_block = (
            '<div class="notice warning">No se envió ningún webhook. '
            'Esto simula que el usuario abandonó o no superó el 3DS de TropiPay.</div>'
        )

    card_options = "".join(
        f'<option value="{html.escape(brand)}"></option>' for brand in TROPIPAY_CARD_EXAMPLES
    )
    bank_options = "".join(
        f'<option value="{html.escape(bank)}"></option>' for bank in TROPIPAY_BANK_EXAMPLES
    )
    error_options = "".join(
        f'<option value="{html.escape(reason)}"></option>' for reason in TROPIPAY_ERROR_EXAMPLES
    )
    attempts_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(attempt.get('cardBrand') or 'No informado'))}</td>"
        f"<td>{html.escape(str(attempt.get('bank') or 'No informado'))}</td>"
        f"<td>•••• {html.escape(str(attempt.get('cardPan') or ''))}</td>"
        f"<td>{html.escape(str(attempt.get('errorReason') or 'No informado'))}</td>"
        "</tr>"
        for attempt in attempts
    )
    attempts_table = (
        f"""
        <div class="table-wrap">
          <table>
            <thead><tr><th>Tarjeta</th><th>Banco</th><th>Terminación</th><th>Error</th></tr></thead>
            <tbody>{attempts_rows}</tbody>
          </table>
        </div>
        """
        if attempts
        else '<p class="empty">Todavía no hay intentos fallidos para esta orden.</p>'
    )

    return f"""
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Mock TropiPay - Pago</title>
  <style>
    * {{ box-sizing:border-box; }}
    body {{ font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#f1f5f9; color:#0f172a; margin:0; padding:32px 16px; }}
    .card {{ max-width:880px; margin:0 auto; background:white; border-radius:18px; box-shadow:0 12px 40px rgba(15,23,42,.12); padding:28px; }}
    .brand {{ font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:#64748b; font-weight:700; }}
    h1 {{ margin:8px 0 4px; }}
    h2 {{ font-size:20px; margin:0 0 6px; }}
    .amount {{ font-size:42px; line-height:1; margin:26px 0; font-weight:800; }}
    .meta {{ display:grid; gap:10px; margin:20px 0; }}
    .row {{ background:#f8fafc; border-radius:12px; padding:12px 14px; color:#334155; }}
    .row strong {{ display:block; color:#0f172a; margin-bottom:4px; }}
    .panel {{ border:1px solid #e2e8f0; border-radius:14px; margin-top:22px; padding:20px; }}
    .panel p {{ color:#475569; margin:6px 0 16px; }}
    .form-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }}
    label {{ color:#334155; display:grid; font-size:13px; font-weight:700; gap:6px; }}
    input {{ width:100%; border:1px solid #cbd5e1; border-radius:10px; color:#0f172a; font:inherit; padding:11px 12px; }}
    input:focus {{ border-color:#7c3aed; box-shadow:0 0 0 3px rgba(124,58,237,.12); outline:none; }}
    .actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:16px; }}
    form {{ margin:0; }}
    button {{ border:0; border-radius:11px; padding:12px 17px; font-weight:750; cursor:pointer; }}
    .pay {{ background:#16a34a; color:white; }}
    .fail-button {{ background:#dc2626; color:white; }}
    .no-webhook {{ background:#e2e8f0; color:#334155; }}
    .notice {{ border-radius:12px; padding:12px 14px; margin:16px 0; font-weight:700; }}
    .notice.ok {{ background:#dcfce7; color:#166534; }}
    .notice.fail {{ background:#fee2e2; color:#991b1b; }}
    .notice.warning {{ background:#fef3c7; color:#92400e; }}
    .table-wrap {{ overflow-x:auto; }}
    table {{ border-collapse:collapse; font-size:14px; width:100%; }}
    th,td {{ border-bottom:1px solid #e2e8f0; padding:10px 8px; text-align:left; vertical-align:top; }}
    th {{ color:#475569; }}
    .empty {{ margin-bottom:0 !important; }}
    .hint {{ font-size:13px; }}
    code {{ overflow-wrap:anywhere; }}
    @media (max-width:640px) {{
      .card {{ padding:20px; }}
      .form-grid {{ grid-template-columns:1fr; }}
      .amount {{ font-size:34px; }}
    }}
  </style>
</head>
<body>
  <main class="card">
    <div class="brand">Mock TropiPay checkout</div>
    <h1>Confirmar pago</h1>
    {status_block}
    <div class="amount">{amount} {currency}</div>
    <div class="meta">
      <div class="row"><strong>Concepto</strong>{concept}</div>
      <div class="row"><strong>Referencia</strong>{reference}</div>
      <div class="row"><strong>PaymentCard ID</strong><code>{paymentcard_id}</code></div>
      <div class="row"><strong>Webhook destino</strong><code>{webhook_url}</code></div>
    </div>

    <form method="post" action="/pay/{paymentcard_id}/pay">
      <button class="pay" type="submit">Pagar y enviar webhook OK</button>
    </form>

    <section class="panel">
      <h2>Simular intento fallido</h2>
      <p>Los campos incluyen ejemplos del log, pero también aceptan valores personalizados.</p>
      <form method="post" action="/pay/{paymentcard_id}/fail">
        <div class="form-grid">
          <label>Marca de tarjeta
            <input name="card_brand" list="card-brands" value="MASTERCARD" required />
          </label>
          <label>Últimos 4 dígitos
            <input name="card_pan" value="6699" inputmode="numeric" maxlength="4" pattern="[0-9]{{4}}" required />
          </label>
          <label>Banco
            <input name="bank" list="banks" value="{html.escape(TROPIPAY_BANK_EXAMPLES[0])}" required />
          </label>
          <label>Motivo del error
            <input name="error_reason" list="errors" value="{html.escape(TROPIPAY_ERROR_EXAMPLES[0])}" required />
          </label>
        </div>
        <datalist id="card-brands">{card_options}</datalist>
        <datalist id="banks">{bank_options}</datalist>
        <datalist id="errors">{error_options}</datalist>
        <div class="actions">
          <button class="fail-button" type="submit">Añadir fallo y enviar webhook</button>
        </div>
      </form>
      <p class="hint">Envía el formulario varias veces para simular intentos con tarjetas o errores diferentes.</p>
    </section>

    <section class="panel">
      <h2>Simular 3DS no completado</h2>
      <p>No genera webhook, como ocurre cuando el usuario no supera o abandona el 3DS de la plataforma.</p>
      <form method="post" action="/pay/{paymentcard_id}/no-webhook">
        <button class="no-webhook" type="submit">Continuar sin enviar webhook</button>
      </form>
    </section>

    <section class="panel">
      <h2>Intentos fallidos acumulados ({len(attempts)})</h2>
      {attempts_table}
    </section>
  </main>
</body>
</html>
"""


@app.get("/pay/{paymentcard_id}", response_class=HTMLResponse)
async def show_mock_payment_page(paymentcard_id: str, result: Optional[str] = Query(None)):
    card = TEST_STATE.paymentcards.get(paymentcard_id)
    if not card:
        return HTMLResponse("<h1>Payment card not found</h1>", status_code=404)
    return HTMLResponse(render_payment_page(card, result=result))


@app.get("/pay/{paymentcard_id}/process", response_class=HTMLResponse)
async def show_mock_payment_process(paymentcard_id: str, result: Optional[str] = Query(None)):
    return await show_mock_payment_page(paymentcard_id, result=result)


@app.post("/pay/{paymentcard_id}/pay")
async def pay_mock_paymentcard(paymentcard_id: str, background_tasks: BackgroundTasks):
    card = TEST_STATE.paymentcards.get(paymentcard_id)
    if not card:
        raise HTTPException(status_code=404, detail="Payment card not found")
    target_url = card.get("urlNotification")
    if not target_url:
        raise HTTPException(status_code=400, detail="Payment card does not have urlNotification")
    card["state"] = 3
    card["updatedAt"] = iso_z()
    background_tasks.add_task(send_tropipay_webhook, card, target_url, "OK", 3)
    return RedirectResponse(url=f"/pay/{paymentcard_id}?result=ok", status_code=303)


@app.post("/pay/{paymentcard_id}/fail")
async def fail_mock_paymentcard(
    paymentcard_id: str,
    background_tasks: BackgroundTasks,
    card_brand: str = Form("MASTERCARD"),
    bank: str = Form(TROPIPAY_BANK_EXAMPLES[0]),
    card_pan: str = Form("6699"),
    error_reason: str = Form(TROPIPAY_ERROR_EXAMPLES[0]),
):
    card = TEST_STATE.paymentcards.get(paymentcard_id)
    if not card:
        raise HTTPException(status_code=404, detail="Payment card not found")
    target_url = card.get("urlNotification")
    if not target_url:
        raise HTTPException(status_code=400, detail="Payment card does not have urlNotification")

    record_failed_charge(card, card_brand, bank, card_pan, error_reason)
    card["state"] = 4
    card["updatedAt"] = iso_z()
    background_tasks.add_task(send_tropipay_webhook, card, target_url, "KO", 4)
    return RedirectResponse(url=f"/pay/{paymentcard_id}?result=failed", status_code=303)


@app.post("/pay/{paymentcard_id}/no-webhook")
async def abandon_mock_paymentcard(paymentcard_id: str):
    if paymentcard_id not in TEST_STATE.paymentcards:
        raise HTTPException(status_code=404, detail="Payment card not found")
    return RedirectResponse(url=f"/pay/{paymentcard_id}?result=no_webhook", status_code=303)


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
        "webhook_signature_header": "X-Tropipay-Signature",
        "default_webhook_secret": get_webhook_secret_for_client("client_001"),
        "error_usage": TEST_STATE.error_usage_count,
        "max_uses": TEST_STATE.max_error_uses,
    }


@app.post("/admin/test-control/reset")
async def reset_test_state():
    TEST_STATE.reset()
    return {"status": "ok", "message": "Estado reseteado"}
