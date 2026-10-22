"""Subconjunto Plaid para los flujos bancarios y de pagos de CrinnoPayments."""

import hashlib
import html
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import app
from .signing import public_key, sign
from .storage import PlaidStorage

__all__ = []
BASE_DIR = Path(__file__).resolve().parents[1]
storage = PlaidStorage(
    os.getenv("PLAID_MOCK_DB_PATH", str(BASE_DIR / "data" / "plaid.sqlite3"))
)
PUBLIC_BASE_URL = os.getenv("PLAID_MOCK_PUBLIC_BASE_URL", "").rstrip("/")
STRICT_AUTH = os.getenv("PLAID_MOCK_STRICT_AUTH", "false").lower() in {
    "1",
    "true",
    "yes",
}
CLIENT_ID = os.getenv("PLAID_MOCK_CLIENT_ID", "mock-plaid-client")
SECRET = os.getenv("PLAID_MOCK_SECRET", "mock-plaid-secret")
PRODUCTS = [
    "transactions",
    "auth",
    "identity",
    "assets",
    "statements",
    "transfer",
    "payment_initiation",
]
router = APIRouter(tags=["Plaid mock"])


def identifier(prefix):
    return f"{prefix}-{uuid4()}"


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def reply(**values):
    return {**values, "request_id": identifier("mock-request")}


class PlaidMockError(HTTPException):
    pass


def error(
    code, message="Solicitud Plaid mock inválida", status=400, kind="INVALID_INPUT"
):
    raise PlaidMockError(
        status_code=status,
        detail=reply(
            error_code=code,
            error_type=kind,
            error_message=message,
            display_message=None,
        ),
    )


@app.exception_handler(PlaidMockError)
async def plaid_error_handler(request, exc):
    # Only our structured Plaid errors use the provider envelope. Preserve other mocks' errors.
    if isinstance(exc.detail, dict) and "error_code" in exc.detail:
        return JSONResponse(
            exc.detail, status_code=exc.status_code, headers=exc.headers
        )
    return JSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
    )


async def credentials(request: Request):
    try:
        body = await request.json()
    except ValueError:
        error("INVALID_REQUEST")
    if not isinstance(body, dict):
        error("INVALID_REQUEST")
    client = request.headers.get("PLAID-CLIENT-ID") or body.get("client_id")
    secret = request.headers.get("PLAID-SECRET") or body.get("secret")
    if (
        not isinstance(client, str)
        or not isinstance(secret, str)
        or not client
        or not secret
    ):
        error("INVALID_API_KEYS")
    if STRICT_AUTH and (client != CLIENT_ID or secret != SECRET):
        error("INVALID_API_KEYS")
    return hashlib.sha256(f"{client}\0{secret}".encode()).hexdigest()


def require(db, kind, key, owner):
    if not isinstance(key, str) or not key:
        error("INVALID_FIELD")
    value = db.get(kind, key, owner)
    if value is None:
        error(
            "INVALID_TOKEN" if "token" in kind else "INVALID_FIELD",
            "Recurso no encontrado",
        )
    return value


def item_for_token(db, data, owner, allow_error=False):
    token = require(db, "access_token", data.get("access_token", ""), owner)
    item = require(db, "item", token["item_id"], owner)
    if item["removed"]:
        error("ITEM_NOT_FOUND", kind="ITEM_ERROR")
    if item.get("error") and not allow_error:
        error(item["error"]["error_code"], kind="ITEM_ERROR")
    return item


def item_response(item):
    return {
        key: item[key]
        for key in (
            "item_id",
            "institution_id",
            "webhook",
            "error",
            "available_products",
            "billed_products",
            "consented_products",
        )
    }


def create_item(db, owner, products, webhook, countries, statements=None):
    item_id = identifier("item-sandbox")
    currency = "USD" if countries[0] == "US" else "EUR"
    accounts = [
        {
            "account_id": identifier("account"),
            "name": "Mock Checking",
            "official_name": "Plaid Mock Checking",
            "mask": "0000",
            "type": "depository",
            "subtype": "checking",
            "balances": {
                "available": 3500.0,
                "current": 3600.0,
                "limit": None,
                "iso_currency_code": currency,
                "unofficial_currency_code": None,
            },
        }
    ]
    item = {
        "item_id": item_id,
        "institution_id": "ins_mock_plaid",
        "webhook": webhook,
        "error": None,
        "available_products": PRODUCTS,
        "billed_products": products,
        "consented_products": products,
        "removed": False,
        "accounts": accounts,
        "transactions_ready": True,
        "transactions": [],
        "statements": [],
    }
    today = date.today()
    for index, (name, amount) in enumerate(
        [("Salary", -2500.0), ("Grocery Store", 75.50), ("Bank Transfer", -150.0)]
    ):
        item["transactions"].append(
            {
                "transaction_id": identifier("transaction"),
                "account_id": accounts[0]["account_id"],
                "amount": amount,
                "iso_currency_code": currency,
                "unofficial_currency_code": None,
                "date": (today - timedelta(days=index)).isoformat(),
                "name": name,
                "merchant_name": None,
                "pending": False,
                "pending_transaction_id": None,
                "payment_channel": "other",
            }
        )
    if "statements" in products and statements:
        item["statements"] = [
            {
                "statement_id": identifier("statement"),
                "month": date.fromisoformat(statements["end_date"]).month,
                "year": date.fromisoformat(statements["end_date"]).year,
            }
        ]
    db.put("item", item_id, owner, item)
    public = identifier("public-sandbox")
    db.put(
        "public_token",
        public,
        owner,
        {"item_id": item_id, "expires": time.time() + 1800, "exchanged": False},
    )
    return item, public


def queue_webhook(db, owner, url, body):
    key = identifier("webhook")
    db.put(
        "webhook",
        key,
        owner,
        {"id": key, "url": url, "body": body, "attempts": 0, "delivered": False},
    )
    return key


async def deliver_webhook(key):
    with storage.transaction() as db:
        delivery = db.get("webhook", key)
    if not delivery or not delivery["url"]:
        return
    raw = json.dumps(
        delivery["body"], separators=(",", ":"), ensure_ascii=False
    ).encode()
    signature = sign(storage, raw)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(
                delivery["url"],
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "Plaid-Verification": signature,
                },
            )
        status = response.status_code
    except httpx.HTTPError:
        status = 0
    with storage.transaction() as db:
        stored = db.get("webhook", key)
        stored.update(
            attempts=stored["attempts"] + 1,
            delivered=200 <= status < 300,
            last_status=status,
        )
        # Read-only operations need no tenant owner, but writes preserve it from the row.
        owner = db.db.execute(
            "SELECT owner FROM plaid_records WHERE kind='webhook' AND id=?", (key,)
        ).fetchone()[0]
        db.put("webhook", key, owner, stored)


@router.post("/institutions/get")
def institutions(data: dict, owner=Depends(credentials)):
    countries = data.get("country_codes")
    if not isinstance(countries, list) or not countries:
        error("INVALID_FIELD")
    institution = {
        "institution_id": "ins_mock_plaid",
        "name": "Plaid Mock Bank",
        "country_codes": countries,
        "products": PRODUCTS,
        "oauth": False,
    }
    count, offset = data.get("count", 10), data.get("offset", 0)
    if (
        not isinstance(count, int)
        or not 1 <= count <= 500
        or not isinstance(offset, int)
        or offset < 0
    ):
        error("INVALID_FIELD")
    return reply(institutions=[institution][offset : offset + count], total=1)


@router.post("/link/token/create")
def link_create(data: dict, request: Request, owner=Depends(credentials)):
    hosted = data.get("hosted_link", {})
    products = data.get("products", [])
    countries = data.get("country_codes", [])
    if (
        not isinstance(hosted, dict)
        or not isinstance(products, list)
        or not isinstance(countries, list)
        or not countries
    ):
        error("INVALID_FIELD")
    if any(product not in PRODUCTS for product in products):
        error("INVALID_PRODUCT")
    lifetime = hosted.get("url_lifetime_seconds", 1800)
    if not isinstance(lifetime, int) or not 1 <= lifetime <= 1814400:
        error("INVALID_FIELD")
    statement_dates = data.get("statements")
    if "statements" in products:
        try:
            start, end = (
                date.fromisoformat(statement_dates[key])
                for key in ("start_date", "end_date")
            )
            if start > end or (end - start).days > 730 or countries != ["US"]:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            error("INVALID_FIELD", "Statements requiere US y un rango válido de fechas")
    with storage.transaction() as db:
        item_id = None
        if data.get("access_token"):
            item = item_for_token(db, data, owner, allow_error=True)
            item_id = item["item_id"]
        elif not products:
            error("INVALID_FIELD", "products es obligatorio")
        payment_id = (data.get("payment_initiation") or {}).get("payment_id")
        intent_id = (data.get("transfer") or {}).get("intent_id")
        if "payment_initiation" in products:
            require(db, "payment", payment_id or "", owner)
        if "transfer" in products:
            require(db, "intent", intent_id or "", owner)
        token = identifier("link-sandbox")
        expiration = (
            (datetime.now(timezone.utc) + timedelta(seconds=lifetime))
            .isoformat()
            .replace("+00:00", "Z")
        )
        base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
        session = {
            "link_token": token,
            "created_at": now(),
            "expiration": expiration,
            "expires": time.time() + lifetime,
            "status": "pending",
            "webhook": data.get("webhook", ""),
            "products": products,
            "countries": countries,
            "statements": statement_dates,
            "item_id": item_id,
            "payment_id": payment_id,
            "intent_id": intent_id,
            "completion_redirect_uri": hosted.get("completion_redirect_uri", ""),
            "link_session_id": identifier("session"),
            "results": None,
            "finished_at": None,
            "events": [],
        }
        db.put("link_token", token, owner, session)
    return reply(
        link_token=token,
        hosted_link_url=f"{base}/mock-plaid/hosted-link/{token}",
        expiration=expiration,
    )


@router.post("/link/token/get")
def link_get(data: dict, owner=Depends(credentials)):
    with storage.transaction() as db:
        session = require(db, "link_token", data.get("link_token", ""), owner)
    if (
        session.get("finished_at")
        and time.time() - session["finished_timestamp"] > 21600
    ):
        error("INVALID_LINK_TOKEN")
    result = []
    if session["finished_at"]:
        result = [
            {
                key: session[key]
                for key in ("link_session_id", "finished_at", "results", "events")
            }
        ]
    return reply(
        created_at=session["created_at"],
        expiration=session["expiration"],
        link_sessions=result,
    )


@app.get("/mock-plaid/hosted-link/{token}", response_class=HTMLResponse)
def hosted_link(token: str):
    with storage.transaction() as db:
        session = require(db, "link_token", token, None)
    if session["expires"] < time.time():
        return HTMLResponse("<h1>Enlace caducado</h1>", status_code=410)
    label = "Reautorizar cuenta" if session["item_id"] else "Autorizar cuenta o pago"
    return HTMLResponse(f"""<!doctype html><html lang="es"><meta charset="utf-8"><title>Plaid Mock</title>
    <body style="font-family:system-ui;max-width:650px;margin:4rem auto;padding:1rem"><h1>Plaid Mock Bank</h1>
    <p>Simulación local. Se usarán cuentas y movimientos ficticios.</p>
    <p>Productos: {html.escape(", ".join(session["products"]))}</p>
    <form method="post" action="/mock-plaid/hosted-link/{html.escape(token, quote=True)}/simulate">
    <button name="outcome" value="success">{label}</button>
    <button name="outcome" value="exit">Cancelar</button>
    <button name="outcome" value="failed">Simular fallo del pago</button></form></body></html>""")


@app.post("/mock-plaid/hosted-link/{token}/simulate")
def finish_link(token: str, tasks: BackgroundTasks, outcome: str = Form("success")):
    if outcome not in {"success", "exit", "failed"}:
        error("INVALID_FIELD")
    with storage.transaction() as db:
        session = require(db, "link_token", token, None)
        owner = db.db.execute(
            "SELECT owner FROM plaid_records WHERE kind='link_token' AND id=?", (token,)
        ).fetchone()[0]
        if session["expires"] < time.time():
            error("INVALID_LINK_TOKEN", status=410)
        if session["status"] != "pending":
            return reply(
                status=session["status"], link_session_id=session["link_session_id"]
            )
        success = outcome == "success"
        public_tokens = []
        if success and session["item_id"]:
            item = require(db, "item", session["item_id"], owner)
            if item["removed"]:
                error("ITEM_NOT_FOUND")
            item["error"] = None
            db.put("item", item["item_id"], owner, item)
        elif session["payment_id"]:
            payment = require(db, "payment", session["payment_id"], owner)
            payment["status"] = (
                "PAYMENT_STATUS_SETTLED"
                if success
                else "PAYMENT_STATUS_FAILED"
                if outcome == "failed"
                else "PAYMENT_STATUS_CANCELLED"
            )
            db.put("payment", payment["payment_id"], owner, payment)
            key = queue_webhook(
                db,
                owner,
                session["webhook"],
                {
                    "webhook_type": "PAYMENT_INITIATION",
                    "webhook_code": "PAYMENT_STATUS_UPDATE",
                    "payment_id": payment["payment_id"],
                    "new_payment_status": payment["status"],
                    "environment": "sandbox",
                },
            )
            tasks.add_task(deliver_webhook, key)
        elif session["intent_id"]:
            intent = require(db, "intent", session["intent_id"], owner)
            intent["status"] = "SUCCEEDED" if success else "FAILED"
            if success:
                transfer_id = identifier("transfer")
                transfer = {
                    "id": transfer_id,
                    "status": "pending",
                    "type": "debit",
                    "iso_currency_code": intent["iso_currency_code"],
                    "amount": intent["amount"],
                    "metadata": intent.get("metadata", {}),
                    "funding_account_id": intent.get("funding_account_id", ""),
                    "created": now(),
                }
                db.put("transfer", transfer_id, owner, transfer)
                intent["transfer_id"] = transfer_id
                add_transfer_event(db, owner, transfer, "pending")
            db.put("intent", intent["id"], owner, intent)
        elif success:
            item, public = create_item(
                db,
                owner,
                session["products"],
                session["webhook"],
                session["countries"],
                session["statements"],
            )
            session["item_id"] = item["item_id"]
            public_tokens = [public]
        session.update(
            status="SUCCESS" if success else "EXITED",
            finished_at=now(),
            finished_timestamp=time.time(),
            results={
                "item_add_results": [{"public_token": value} for value in public_tokens]
            },
            events=[
                {"event_name": "HANDOFF" if success else "EXIT", "timestamp": now()}
            ],
        )
        key = queue_webhook(
            db,
            owner,
            session["webhook"],
            {
                "webhook_type": "LINK",
                "webhook_code": "SESSION_FINISHED",
                "status": session["status"],
                "link_token": token,
                "link_session_id": session["link_session_id"],
                "public_tokens": public_tokens,
                "environment": "sandbox",
            },
        )
        session["webhook_id"] = key
        db.put("link_token", token, owner, session)
        tasks.add_task(deliver_webhook, key)
    if session["completion_redirect_uri"]:
        return RedirectResponse(session["completion_redirect_uri"], status_code=303)
    return reply(
        status=session["status"],
        link_session_id=session["link_session_id"],
        webhook_id=key,
    )


@router.post("/item/public_token/exchange")
def exchange(data: dict, owner=Depends(credentials)):
    with storage.transaction() as db:
        key = data.get("public_token", "")
        public = require(db, "public_token", key, owner)
        if public["exchanged"] or public["expires"] < time.time():
            error("INVALID_PUBLIC_TOKEN")
        token = identifier("access-sandbox")
        public["exchanged"] = True
        db.put("public_token", key, owner, public)
        db.put("access_token", token, owner, {"item_id": public["item_id"]})
    return reply(access_token=token, item_id=public["item_id"])


@router.post("/item/get")
def item_get(data: dict, owner=Depends(credentials)):
    with storage.transaction() as db:
        item = item_for_token(db, data, owner, allow_error=True)
    return reply(item=item_response(item), status={})


@router.post("/item/remove")
def item_remove(data: dict, owner=Depends(credentials)):
    with storage.transaction() as db:
        item = item_for_token(db, data, owner, allow_error=True)
        item["removed"] = True
        db.put("item", item["item_id"], owner, item)
    return reply()


@router.post("/accounts/get")
@router.post("/accounts/balance/get")
def accounts(data: dict, owner=Depends(credentials)):
    with storage.transaction() as db:
        item = item_for_token(db, data, owner)
    return reply(accounts=item["accounts"], item=item_response(item))


@router.post("/transactions/get")
def transactions(data: dict, owner=Depends(credentials)):
    try:
        start, end = (
            date.fromisoformat(data["start_date"]),
            date.fromisoformat(data["end_date"]),
        )
        options = data.get("options", {})
        count, offset = options.get("count", 100), options.get("offset", 0)
        if (
            start > end
            or not isinstance(count, int)
            or not 1 <= count <= 500
            or not isinstance(offset, int)
            or offset < 0
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        error("INVALID_FIELD")
    with storage.transaction() as db:
        item = item_for_token(db, data, owner)
    if "transactions" not in item["consented_products"]:
        error("PRODUCTS_NOT_SUPPORTED")
    if not item["transactions_ready"]:
        error("PRODUCT_NOT_READY", kind="ITEM_ERROR")
    values = [
        value
        for value in item["transactions"]
        if start <= date.fromisoformat(value["date"]) <= end
    ]
    return reply(
        accounts=item["accounts"],
        item=item_response(item),
        transactions=values[offset : offset + count],
        total_transactions=len(values),
    )


@router.post("/statements/list")
def statements(data: dict, owner=Depends(credentials)):
    with storage.transaction() as db:
        item = item_for_token(db, data, owner)
    if "statements" not in item["consented_products"]:
        error("PRODUCTS_NOT_SUPPORTED")
    account = item["accounts"][0]
    return reply(
        item_id=item["item_id"],
        institution_id=item["institution_id"],
        institution_name="Plaid Mock Bank",
        accounts=[
            {
                "account_id": account["account_id"],
                "account_name": account["name"],
                "account_mask": account["mask"],
                "statements": item["statements"],
            }
        ],
    )


@router.post("/webhook_verification_key/get")
def verification_key(data: dict, owner=Depends(credentials)):
    key = public_key(storage)
    if data.get("key_id") != key["kid"]:
        error("INVALID_FIELD")
    return reply(key=key)


@router.post("/mock-plaid/items/{item_id}/simulate")
def simulate_item(
    item_id: str, data: dict, tasks: BackgroundTasks, owner=Depends(credentials)
):
    outcome = data.get("outcome")
    if outcome not in {
        "login_required",
        "revoke",
        "transactions_ready",
        "transactions_pending",
    }:
        error("INVALID_FIELD")
    with storage.transaction() as db:
        item = require(db, "item", item_id, owner)
        if item["removed"]:
            error("ITEM_NOT_FOUND")
        if outcome == "login_required":
            item["error"] = {
                "error_code": "ITEM_LOGIN_REQUIRED",
                "error_type": "ITEM_ERROR",
            }
            body = {
                "webhook_type": "ITEM",
                "webhook_code": "ERROR",
                "error": item["error"],
            }
        elif outcome == "revoke":
            item["removed"] = True
            body = {"webhook_type": "ITEM", "webhook_code": "USER_PERMISSION_REVOKED"}
        else:
            item["transactions_ready"] = outcome == "transactions_ready"
            body = {"webhook_type": "TRANSACTIONS", "webhook_code": "DEFAULT_UPDATE"}
        db.put("item", item_id, owner, item)
        key = queue_webhook(
            db,
            owner,
            item["webhook"],
            {**body, "item_id": item_id, "environment": "sandbox"},
        )
        tasks.add_task(deliver_webhook, key)
    return reply(webhook_id=key)


@router.post("/mock-plaid/webhooks/{key}/retry")
def retry_webhook(
    key: str, data: dict, tasks: BackgroundTasks, owner=Depends(credentials)
):
    with storage.transaction() as db:
        require(db, "webhook", key, owner)
    tasks.add_task(deliver_webhook, key)
    return reply(webhook_id=key)


@router.post("/mock-plaid/webhooks/{key}/get")
def get_webhook(key: str, data: dict, owner=Depends(credentials)):
    with storage.transaction() as db:
        delivery = require(db, "webhook", key, owner)
    return reply(
        webhook={
            field: value
            for field, value in delivery.items()
            if field not in {"body", "url"}
        }
    )


@router.post("/user/create")
def user_create(data: dict, owner=Depends(credentials)):
    with storage.transaction() as db:
        key = identifier("user")
        db.put("user", key, owner, data)
    return reply(user_id=key)


@router.post("/payment_initiation/payment/create")
def payment_create(data: dict, owner=Depends(credentials)):
    if (
        not data.get("recipient_id")
        or not data.get("reference")
        or not isinstance(data.get("amount"), dict)
    ):
        error("INVALID_FIELD")
    with storage.transaction() as db:
        require(db, "user", data.get("user_id", ""), owner)
        payment = {
            **data,
            "payment_id": identifier("payment"),
            "status": "PAYMENT_STATUS_INPUT_NEEDED",
            "last_status_update": now(),
        }
        db.put("payment", payment["payment_id"], owner, payment)
    return reply(payment_id=payment["payment_id"], status=payment["status"])


@router.post("/payment_initiation/payment/get")
def payment_get(data: dict, owner=Depends(credentials)):
    with storage.transaction() as db:
        payment = require(db, "payment", data.get("payment_id", ""), owner)
    return reply(**payment)


@router.post("/transfer/intent/create")
def intent_create(data: dict, owner=Depends(credentials)):
    if (
        data.get("mode") != "PAYMENT"
        or not data.get("amount")
        or data.get("iso_currency_code") != "USD"
    ):
        error("INVALID_FIELD")
    intent = {
        **data,
        "id": identifier("intent"),
        "status": "PENDING",
        "transfer_id": None,
    }
    with storage.transaction() as db:
        db.put("intent", intent["id"], owner, intent)
    return reply(transfer_intent=intent)


@router.post("/transfer/intent/get")
def intent_get(data: dict, owner=Depends(credentials)):
    with storage.transaction() as db:
        intent = require(db, "intent", data.get("transfer_intent_id", ""), owner)
    return reply(transfer_intent=intent)


@router.post("/transfer/get")
def transfer_get(data: dict, owner=Depends(credentials)):
    with storage.transaction() as db:
        transfer = require(db, "transfer", data.get("transfer_id", ""), owner)
    return reply(transfer=transfer)


def add_transfer_event(db, owner, transfer, event_type):
    events = db.list("transfer_event", owner)
    cursor = max((event["event_id"] for event in events), default=0) + 1
    event = {
        "event_id": cursor,
        "transfer_id": transfer["id"],
        "event_type": event_type,
        "timestamp": now(),
    }
    db.put("transfer_event", f"{owner}:{cursor}", owner, event)


@router.post("/sandbox/transfer/simulate")
def simulate_transfer(data: dict, tasks: BackgroundTasks, owner=Depends(credentials)):
    event_type = data.get("event_type")
    if event_type not in {
        "posted",
        "settled",
        "funds_available",
        "failed",
        "returned",
        "cancelled",
    }:
        error("INVALID_FIELD")
    with storage.transaction() as db:
        transfer = require(db, "transfer", data.get("transfer_id", ""), owner)
        if transfer["status"] != event_type:
            transfer["status"] = event_type
            db.put("transfer", transfer["id"], owner, transfer)
            add_transfer_event(db, owner, transfer, event_type)
        # Wake up the same webhook URL used for the originating Hosted Link.
        sessions = [
            session for session in db.list("link_token", owner) if session["intent_id"]
        ]
        intents = {
            intent["id"]
            for intent in db.list("intent", owner)
            if intent.get("transfer_id") == transfer["id"]
        }
        for session in sessions:
            if session["intent_id"] in intents:
                key = queue_webhook(
                    db,
                    owner,
                    session["webhook"],
                    {
                        "webhook_type": "TRANSFER",
                        "webhook_code": "TRANSFER_EVENTS_UPDATE",
                        "environment": "sandbox",
                    },
                )
                tasks.add_task(deliver_webhook, key)
    return reply()


@router.post("/transfer/event/sync")
def transfer_event_sync(data: dict, owner=Depends(credentials)):
    after, count = data.get("after_id", 0), data.get("count", 100)
    if (
        not isinstance(after, int)
        or after < 0
        or not isinstance(count, int)
        or not 1 <= count <= 500
    ):
        error("INVALID_FIELD")
    with storage.transaction() as db:
        events = [
            event
            for event in db.list("transfer_event", owner)
            if event["event_id"] > after
        ]
    return reply(transfer_events=events[:count], has_more=len(events) > count)


app.include_router(router)
