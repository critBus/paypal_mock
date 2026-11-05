"""The Noon subset consumed by CrinnoPayments; no real financial effects."""

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app import app, templates
from noon.storage import NoonStorage

__all__ = []
storage = NoonStorage(
    os.getenv(
        "NOON_MOCK_DB_PATH",
        str(Path(__file__).resolve().parents[1] / "data" / "noon.sqlite3"),
    )
)
PENDING = {
    "INITIATED",
    "PAYMENT_INFO_ADDED",
    "3DS_ENROLL_CHECKED",
    "3DS_RESULT_VERIFIED",
    "AUTHENTICATED",
    "TOKENIZED",
}
CANCELLABLE = PENDING - {"TOKENIZED"}
FINAL = {"CAPTURED", "FAILED", "REJECTED", "CANCELLED", "EXPIRED"}
STATES = (
    PENDING
    | FINAL
    | {
        "AUTHORIZED",
        "PENDING",
        "MARKED_FOR_REVIEW",
        "PARTIALLY_CAPTURED",
        "REFUNDED",
        "UNKNOWN",
    }
)
OPERATIONS = {"INITIATE", "GET", "GET_BY_REFERENCE", "CAPTURE", "CANCEL"}
SCENARIOS = {
    "normal": {},
    "no_webhook": {"webhook_mode": "suppress"},
    "duplicate_webhook": {"webhook_mode": "duplicate"},
    "invalid_signature": {"webhook_mode": "bad_signature"},
    "wrong_kid": {"webhook_mode": "wrong_kid"},
    "wrong_algorithm": {"webhook_mode": "wrong_alg"},
    "malformed_webhook": {"webhook_mode": "malformed"},
    "capture_pending": {"capture_outcome": "pending"},
    "capture_declined": {"capture_outcome": "declined"},
    "capture_lost_response": {
        "capture_outcome": "lost_response",
        "webhook_mode": "suppress",
    },
    "initiate_lost_response": {
        "faults": {"INITIATE": {"kind": "lost_response", "times": 1}}
    },
    "missing_checkout": {
        "faults": {"INITIATE": {"kind": "missing_checkout", "times": 1}}
    },
    "get_unavailable": {"faults": {"GET": {"kind": "api_error", "times": -1}}},
    "invalid_json": {"faults": {"GET": {"kind": "invalid_json", "times": -1}}},
    "wrong_amount": {"faults": {"GET": {"kind": "wrong_amount", "times": -1}}},
    "wrong_currency": {"faults": {"GET": {"kind": "wrong_currency", "times": -1}}},
    "wrong_id": {"faults": {"GET": {"kind": "wrong_id", "times": -1}}},
    "wrong_reference": {"faults": {"GET": {"kind": "wrong_reference", "times": -1}}},
    "captured_amount_mismatch": {
        "faults": {"GET": {"kind": "captured_amount_mismatch", "times": -1}}
    },
}


class Fault(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "api_error",
        "invalid_json",
        "delay",
        "lost_response",
        "wrong_amount",
        "wrong_currency",
        "wrong_id",
        "wrong_reference",
        "missing_order",
        "missing_checkout",
        "captured_amount_mismatch",
    ]
    times: int = Field(default=1, ge=-1, le=100)
    delay_seconds: float = Field(default=0, ge=0, le=120)
    http_status: int = Field(default=503, ge=400, le=599)
    result_code: int = Field(default=2000, ge=1)


class Controls(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str | None = None
    scenario: str = "normal"
    faults: dict[str, Fault] | None = None
    webhook_mode: (
        Literal[
            "normal",
            "suppress",
            "duplicate",
            "bad_signature",
            "wrong_kid",
            "wrong_alg",
            "malformed",
        ]
        | None
    ) = None
    webhook_delay_seconds: float = Field(default=0, ge=0, le=30)
    capture_outcome: (
        Literal["completed", "pending", "declined", "lost_response"] | None
    ) = None


class Transition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: str = "success"
    notify: bool = True
    force: bool = False


class Replay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str | None = None
    mode: (
        Literal[
            "normal",
            "suppress",
            "duplicate",
            "bad_signature",
            "wrong_kid",
            "wrong_alg",
            "malformed",
        ]
        | None
    ) = None


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def error(code, message, status=400):
    return JSONResponse(
        {
            "resultCode": code,
            "message": message,
            "resultClass": 30,
            "requestReference": str(uuid4()),
            "result": None,
        },
        status_code=status,
    )


def auth(request):
    business = os.getenv("NOON_MOCK_BUSINESS_IDENTIFIER", "mock-business")
    application = os.getenv("NOON_MOCK_APPLICATION_IDENTIFIER", "mock-app")
    key = os.getenv("NOON_MOCK_APPLICATION_KEY", "mock-application-key")
    expected = (
        "Key " + base64.b64encode(f"{business}.{application}:{key}".encode()).decode()
    )
    return hmac.compare_digest(
        request.headers.get("authorization", "").encode(), expected.encode()
    )


def public_url():
    value = os.getenv("NOON_MOCK_CHECKOUT_BASE_URL", "http://localhost:7000").rstrip(
        "/"
    )
    if urlsplit(value).scheme not in {"http", "https"} or not urlsplit(value).netloc:
        raise HTTPException(
            503, "NOON_MOCK_CHECKOUT_BASE_URL must be an HTTP(S) origin"
        )
    return value


def money(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Invalid amount")
    return amount


def view(order):
    return {key: value for key, value in order.items() if not key.startswith("_")}


def envelope(order):
    return {
        "resultCode": 0,
        "message": "Processed successfully",
        "resultClass": 0,
        "requestReference": str(uuid4()),
        "result": {
            "order": view(order),
            "checkoutData": {
                "postUrl": f"{public_url()}/mock-noon/checkout/{order['_token']}"
            },
        },
    }


def record_event(order, event_type=None):
    event = {
        "eventId": str(uuid4()),
        "eventType": event_type
        or {
            "AUTHORIZED": "Authorize",
            "CAPTURED": "Capture",
            "CANCELLED": "Cancel",
            "EXPIRED": "Expire",
            "FAILED": "Fail",
            "REJECTED": "Reject",
        }.get(order["status"], "Update"),
        "orderId": order["id"],
        "merchantOrderRef": order["reference"],
        "orderStatus": order["status"],
        "timeStamp": now(),
    }
    order["_events"].append(event)
    return copy.deepcopy(event)


def transition(order, status, *, force=False):
    if status not in STATES:
        raise HTTPException(422, "Unsupported simulated state")
    if order["status"] == status:
        return None
    if order["status"] in FINAL and not force:
        raise HTTPException(
            409,
            "Terminal order; use force only for deliberate late/out-of-order simulations",
        )
    order["status"] = status
    order["totalAuthorizedAmount"] = (
        order["amount"]
        if status in {"AUTHORIZED", "CAPTURED", "PARTIALLY_CAPTURED"}
        else order["totalAuthorizedAmount"]
    )
    if status == "CAPTURED":
        order["totalCapturedAmount"] = order["amount"]
    elif status == "PARTIALLY_CAPTURED":
        order["totalCapturedAmount"] = str(Decimal(order["amount"]) / 2)
    order["lastUpdatedTime"] = now()
    return record_event(order)


def expire(order):
    deadline = order["_configuration"].get("initiationValidity")
    if deadline and order["status"] in PENDING:
        instant = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        if instant.timestamp() <= time.time():
            return transition(order, "EXPIRED")
    return None


def encode(value):
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def sign_event(event, mode="normal"):
    header = {
        "alg": "none" if mode == "wrong_alg" else "HS256",
        "kid": "wrong-key"
        if mode == "wrong_kid"
        else os.getenv("NOON_MOCK_WEBHOOK_KEY_ID", "mock-noon-key"),
        "iat": int(time.time()),
    }
    body = f"{encode(header)}.{encode(event)}"
    secret = (
        "deliberately-wrong"
        if mode == "bad_signature"
        else os.getenv("NOON_MOCK_WEBHOOK_SECRET", "mock-noon-webhook-secret")
    )
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(secret.encode(), body.encode("ascii"), hashlib.sha256).digest()
        )
        .decode()
        .rstrip("=")
    )
    return (
        {"data": f"{body}.{signature}"}
        if mode != "malformed"
        else {"data": "deliberately-malformed"}
    )


async def deliver(order, event, mode=None):
    if not event:
        return
    controls = order["_controls"]
    mode = mode or controls.get("webhook_mode", "normal")
    target = os.getenv("NOON_MOCK_WEBHOOK_URL")
    if target is None:
        target = order["_configuration"].get("webhookUrl", "")
    attempts = 2 if mode == "duplicate" else 1
    if mode == "suppress" or not target:
        storage.record_delivery(
            {
                "event_id": event["eventId"],
                "order_id": str(order["id"]),
                "mode": mode,
                "status": "suppressed",
                "time": now(),
            }
        )
        return
    await asyncio.sleep(controls.get("webhook_delay_seconds", 0))
    body = sign_event(event, mode)
    for attempt in range(attempts):
        result = {
            "event_id": event["eventId"],
            "order_id": str(order["id"]),
            "mode": mode,
            "attempt": attempt + 1,
            "time": now(),
        }
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.post(target, json=body)
            result.update(
                status="accepted" if response.status_code == 200 else "rejected",
                http_status=response.status_code,
            )
        except (httpx.HTTPError, httpx.InvalidURL):
            result["status"] = "connection_error"
        storage.record_delivery(result)


def consume_fault(order, operation):
    fault = order["_controls"].get("faults", {}).get(operation)
    if not fault or fault.get("times", 1) == 0:
        return None
    saved = copy.deepcopy(fault)
    if fault.get("times", 1) > 0:
        fault["times"] = fault.get("times", 1) - 1
    return saved


async def fault_response(fault, response):
    if not fault:
        return response
    kind = fault["kind"]
    if kind == "delay":
        await asyncio.sleep(fault.get("delay_seconds", 0))
    if kind in {"api_error", "lost_response"}:
        return error(
            fault.get("result_code", 2000),
            "Simulated provider outcome; reconcile before retrying",
            fault.get("http_status", 503),
        )
    if kind == "invalid_json":
        return Response("invalid-json", media_type="application/json")
    result = response["result"]
    remote = result["order"]
    if kind == "wrong_amount":
        remote["amount"] = str(Decimal(remote["amount"]) + 1)
    elif kind == "wrong_currency":
        remote["currency"] = "XXX"
    elif kind == "wrong_id":
        remote["id"] += 1
    elif kind == "wrong_reference":
        remote["reference"] = "wrong-reference"
    elif kind == "missing_order":
        result.pop("order")
    elif kind == "missing_checkout":
        result.pop("checkoutData")
    elif kind == "captured_amount_mismatch":
        remote["totalCapturedAmount"] = str(Decimal(remote["amount"]) / 2)
    return response


@app.post("/payment/v1/order")
async def noon_operation(request: Request, background: BackgroundTasks):
    if not auth(request):
        return error(1500, "Invalid credentials", 401)
    try:
        payload = json.loads(await request.body(), parse_float=Decimal)
        operation = payload["apiOperation"]
        remote = payload["order"]
        if not isinstance(remote, dict) or not isinstance(operation, str):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        return error(19000, "Invalid request")
    if operation == "INITIATE":
        try:
            amount = money(remote["amount"])
            currency = remote["currency"]
            category = remote["category"]
            reference = remote["reference"]
            config = payload["configuration"]
            if not isinstance(reference, str) or not 0 < len(reference) <= 50:
                raise ValueError("Invalid reference")
            if not (
                currency.isascii()
                and currency.isalpha()
                and len(currency) == 3
                and currency.isupper()
            ):
                raise ValueError("Invalid currency")
            if config.get("paymentAction", "SALE") not in {"SALE", "AUTHORIZE"}:
                raise ValueError("Invalid payment action")
            if remote["channel"].upper() not in {"WEB", "MOBILE"}:
                raise ValueError("Invalid channel")
            precision = (
                3
                if currency in {"KWD", "BHD", "OMR", "JOD", "TND", "IQD", "LYD"}
                else 0
                if currency
                in {
                    "JPY",
                    "KRW",
                    "BIF",
                    "CLP",
                    "DJF",
                    "GNF",
                    "ISK",
                    "KMF",
                    "PYG",
                    "RWF",
                    "UGX",
                    "UYI",
                    "VND",
                    "VUV",
                    "XAF",
                    "XOF",
                    "XPF",
                }
                else 2
            )
            if amount != amount.quantize(Decimal(10) ** -precision):
                raise ValueError("Invalid precision")
            if config.get("initiationValidity"):
                datetime.fromisoformat(
                    config["initiationValidity"].replace("Z", "+00:00")
                )
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            InvalidOperation,
        ):
            return error(
                19000, "Invalid order amount, currency, reference or configuration"
            )
        if category not in os.getenv("NOON_MOCK_CATEGORIES", "pay").split(","):
            return error(19014, "No transaction route configured for category")
        order = {
            "id": secrets.randbelow(7_000_000_000_000_000) + 1_000_000_000_000_000,
            "status": "INITIATED",
            "reference": reference,
            "amount": str(amount),
            "currency": currency,
            "category": category,
            "name": remote.get("name", reference),
            "creationTime": now(),
            "totalCapturedAmount": "0",
            "totalAuthorizedAmount": "0",
            "_token": secrets.token_urlsafe(24),
            "_configuration": config,
            "_controls": storage.defaults(),
            "_events": [],
            "_capture_reference": None,
            "_capture_submitted": False,
        }
        configured_fault = order["_controls"].get("faults", {}).get("INITIATE", {})
        if (
            configured_fault.get("kind") == "api_error"
            and configured_fault.get("times", 1) != 0
        ):
            return await fault_response(configured_fault, {})
        order, created = storage.create(order)
        if not created:
            return JSONResponse(
                {
                    **envelope(order),
                    "resultCode": 19012,
                    "message": "Duplicate merchant reference",
                },
                status_code=409,
            )
        order, fault = storage.mutate(
            order["id"], lambda value: consume_fault(value, "INITIATE")
        )
        return await fault_response(fault, envelope(order))
    if operation not in {"CAPTURE", "CANCEL"}:
        return error(19000, "Unsupported apiOperation")
    order = storage.get(remote.get("id", ""))
    if not order:
        return error(19001, "Order does not exist", 404)

    def mutate(value):
        expired = expire(value)
        fault = consume_fault(value, operation)
        if fault and fault["kind"] in {"api_error", "invalid_json"}:
            return {"event": expired, "fault": fault}
        if operation == "CANCEL":
            if value["status"] not in CANCELLABLE:
                return {
                    "event": expired,
                    "error": (
                        19019,
                        "Only unpaid pre-authorization orders can be cancelled",
                    ),
                }
            event = transition(value, "CANCELLED")
        else:
            transaction = payload.get("transaction", {})
            if not isinstance(transaction, dict):
                return {"event": expired, "error": (19000, "Invalid transaction")}
            try:
                matches = (
                    money(transaction["amount"]) == Decimal(value["amount"])
                    and transaction["currency"] == value["currency"]
                )
            except (KeyError, ValueError, TypeError, InvalidOperation):
                matches = False
            if not matches or transaction.get("finalCapture") is not True:
                return {
                    "event": expired,
                    "error": (19068, "Only exact full final capture is supported"),
                }
            if value["_capture_submitted"]:
                return {
                    "event": expired,
                    "error": (
                        19069,
                        "Capture already submitted; reconcile the existing result",
                    ),
                }
            if value["status"] != "AUTHORIZED":
                return {
                    "event": expired,
                    "error": (19019, "Capture requires AUTHORIZED state"),
                }
            value["_capture_submitted"] = True
            value["_capture_reference"] = transaction.get("transactionReference")
            outcome = value["_controls"].get("capture_outcome", "completed")
            status = {
                "completed": "CAPTURED",
                "lost_response": "CAPTURED",
                "pending": "PENDING",
                "declined": "AUTHORIZED",
            }[outcome]
            event = transition(value, status)
            if outcome == "declined":
                return {"event": event, "error": (16528, "Simulated capture decline")}
            if outcome == "lost_response":
                fault = {"kind": "lost_response"}
        return {"event": event, "fault": fault}

    order, result = storage.mutate(order["id"], mutate)
    background.add_task(deliver, order, result.get("event"))
    if "error" in result:
        return error(*result["error"], status=409)
    return await fault_response(result.get("fault"), envelope(order))


async def retrieve(request, identifier, background, by="id"):
    if not auth(request):
        return error(1500, "Invalid credentials", 401)
    order = storage.get(identifier, by=by)
    if not order:
        return error(19001, "Order does not exist", 404)
    order, result = storage.mutate(
        order["id"],
        lambda value: (
            expire(value),
            consume_fault(value, "GET" if by == "id" else "GET_BY_REFERENCE"),
        ),
    )
    event, fault = result
    background.add_task(deliver, order, event)
    return await fault_response(fault, envelope(order))


@app.get("/payment/v1/order/getbyreference/{reference}")
async def noon_get_by_reference(
    reference: str, request: Request, background: BackgroundTasks
):
    return await retrieve(request, reference, background, by="reference")


@app.get("/payment/v1/order/{order_id}")
async def noon_get_order(order_id: str, request: Request, background: BackgroundTasks):
    return await retrieve(request, order_id, background)


@app.get("/mock-noon/orders")
async def noon_orders():
    return [
        {
            "id": str(order["id"]),
            "reference": order["reference"],
            "amount": order["amount"],
            "currency": order["currency"],
            "status": order["status"],
            "payment_action": order["_configuration"].get("paymentAction", "SALE"),
            "checkout_path": f"/mock-noon/checkout/{order['_token']}",
            "events": order["_events"],
            "controls": order["_controls"],
        }
        for order in storage.orders()
    ]


@app.get("/mock-noon/controls")
async def noon_controls():
    return {
        "defaults": storage.defaults(),
        "scenarios": list(SCENARIOS),
        "states": sorted(STATES),
    }


@app.post("/mock-noon/controls")
async def noon_set_controls(data: Controls):
    if data.scenario not in SCENARIOS or (
        data.faults and not set(data.faults) <= OPERATIONS
    ):
        raise HTTPException(422, "Unknown scenario or operation")
    config = copy.deepcopy(SCENARIOS[data.scenario])
    config["webhook_delay_seconds"] = data.webhook_delay_seconds
    for name in ("webhook_mode", "capture_outcome"):
        if getattr(data, name) is not None:
            config[name] = getattr(data, name)
    if data.faults is not None:
        config["faults"] = {
            name: value.model_dump() for name, value in data.faults.items()
        }
    if data.order_id:
        result = storage.mutate(
            data.order_id, lambda value: value.update(_controls=config)
        )
        if not result:
            raise HTTPException(404, "Order not found")
    else:
        storage.defaults(config)
    return config


@app.post("/mock-noon/orders/{order_id}/simulate")
async def noon_simulate(order_id: str, data: Transition, background: BackgroundTasks):
    def change(order):
        expired = expire(order)
        if expired and not data.force:
            return expired
        desired = (
            "AUTHORIZED"
            if order["_configuration"].get("paymentAction") == "AUTHORIZE"
            else "CAPTURED"
        )
        status = desired if data.outcome == "success" else data.outcome.upper()
        if (
            status in {"CANCELLED", "EXPIRED"}
            and order["status"] not in CANCELLABLE
            and not data.force
        ):
            raise HTTPException(
                409,
                "Only unpaid pre-authorization checkout can be cancelled or expired",
            )
        return transition(order, status, force=data.force)

    result = storage.mutate(order_id, change)
    if not result:
        raise HTTPException(404, "Order not found")
    order, event = result
    if data.notify:
        background.add_task(deliver, order, event)
    return {
        "order": view(order),
        "return_url": order["_configuration"].get("returnUrl", ""),
        "event_id": event["eventId"] if event else None,
    }


@app.post("/mock-noon/orders/{order_id}/replay")
async def noon_replay(order_id: str, data: Replay, background: BackgroundTasks):
    order = storage.get(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    events = order["_events"]
    event = next(
        (
            event
            for event in reversed(events)
            if not data.event_id or event["eventId"] == data.event_id
        ),
        None,
    )
    if not event:
        raise HTTPException(404, "Event not found")
    background.add_task(deliver, order, event, data.mode)
    return {"event_id": event["eventId"], "order_status": event["orderStatus"]}


@app.get("/mock-noon/webhook-deliveries")
async def noon_deliveries():
    return storage.deliveries()


@app.get("/mock-noon", response_class=HTMLResponse)
async def noon_dashboard(request: Request):
    return templates.TemplateResponse(
        request=request, name="noon_dashboard.html", context={}
    )


@app.get("/mock-noon/checkout/{token}", response_class=HTMLResponse)
async def noon_checkout(token: str, request: Request, background: BackgroundTasks):
    order = storage.get(token, by="token")
    if not order:
        raise HTTPException(404, "Checkout not found")
    order, event = storage.mutate(order["id"], expire)
    background.add_task(deliver, order, event)
    return templates.TemplateResponse(
        request=request,
        name="noon_checkout.html",
        context={
            "order": view(order),
            "order_id": str(order["id"]),
            "action": order["_configuration"].get("paymentAction", "SALE"),
        },
    )
