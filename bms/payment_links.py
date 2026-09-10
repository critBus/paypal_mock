"""BMSPay Payment Links and local checkout controls. No real charges."""

import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from .config import BMS_CONFIG
from .payment_link_storage import PaymentLinkStorage
from .payment_link_webhooks import deliver, find_event, new_event

router = APIRouter(tags=["BMS Payment Links"])
storage = PaymentLinkStorage(
    os.getenv(
        "BMS_MOCK_DB_PATH",
        str(Path(__file__).resolve().parents[1] / "data" / "bms.sqlite3"),
    )
)
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parents[1] / "templates")
)


def now():
    return datetime.now(timezone.utc).isoformat()


def public(link):
    data = {key: value for key, value in link.items() if not key.startswith("_")}
    # Keep persistent records from older mock versions readable.
    data["Status"] = 1 if data["Status"] in (1, "Paid") else 0
    data["Type"] = 1
    data["AllowedPaymentSchedule"] = 0
    return data


def result(code=200, message="SUCCESS.", link=None, **extra):
    return {
        "ResponseCode": code,
        "Msg": [message],
        "verbiage": None,
        "displayMessage": None,
        "PaymentLink": public(link) if link else None,
        **extra,
    }


async def parameters(request):
    try:
        body = await request.json() if await request.body() else {}
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    return {**body, **dict(request.query_params)}


def credential(data, key):
    lowered = {
        name.removeprefix("request.").lower(): value for name, value in data.items()
    }
    return str(lowered.get(key.lower(), ""))


def authenticated(data):
    expected = {
        "mid": BMS_CONFIG.mid,
        "cid": BMS_CONFIG.cid,
        "AppKey": BMS_CONFIG.app_key,
        "UserName": BMS_CONFIG.username,
        "Password": BMS_CONFIG.password,
        "AppType": "1",
    }
    return data is not None and all(
        credential(data, key) == str(value) for key, value in expected.items()
    )


def amount(value):
    try:
        parsed = Decimal(str(value))
        if (
            not parsed.is_finite()
            or parsed <= 0
            or parsed > Decimal("99999999999999999.99")
        ):
            return None
        if parsed != parsed.quantize(Decimal("0.01")):
            return None
        return format(parsed, ".2f")
    except (InvalidOperation, ValueError):
        return None


async def authorized_link(request, identifier=None):
    data = await parameters(request)
    if not authenticated(data):
        return None, result(1, "INVALID CREDENTIALS.")
    payment_link = data.get("PaymentLink", {})
    if not isinstance(payment_link, dict):
        return None, result(506, "INVALID PARAMETER.")
    identifier = identifier or data.get("id") or payment_link.get("Id")
    link = storage.get(str(identifier), BMS_CONFIG.mid)
    if not link:
        return None, result(404, "PAYMENT LINK NOT FOUND.")
    return link, None


def operate(identifier, operation, mutate):
    def apply(link):
        failure = link.get("_failures", {}).get(operation)
        if failure and failure["remaining"] > 0:
            failure["remaining"] -= 1
            return result(failure["response_code"], "SIMULATED BMSPAY ERROR.")
        return mutate(link)

    return storage.update(identifier, apply) or result(404, "PAYMENT LINK NOT FOUND.")


@router.post("/api/PaymentLinks/AddPaymentLink")
@router.post("/api/PaymentLinks")
async def add_payment_link(request: Request):
    data = await parameters(request)
    if not authenticated(data):
        return result(1, "INVALID CREDENTIALS.")
    payload = data.get("PaymentLink")
    if not isinstance(payload, dict):
        return result(506, "INVALID PAYMENT LINK.")
    total = amount(payload.get("Amount"))
    if total is None:
        return result(506, "INVALID AMOUNT. A FIXED POSITIVE AMOUNT IS REQUIRED.")
    identifier = str(uuid4())
    base = os.getenv("BMS_MOCK_PUBLIC_URL", str(request.base_url)).rstrip("/")
    link = {
        "Id": identifier,
        "MerchantId": int(BMS_CONFIG.mid),
        "Amount": total,
        "Description": str(payload.get("Description") or ""),
        "InvoiceNumber": str(payload.get("InvoiceNumber") or ""),
        "Type": "Fixed",
        "CreatedOn": now(),
        "PaidOn": None,
        "DisabledOn": None,
        "Status": "UnPaid",
        "Active": True,
        "ServiceReferenceNumber": None,
        "Link": f"{base}/payments/link/{identifier}",
        "AllowRecurringPayment": False,
        "AllowedPaymentSchedule": 0,
        "PaymentSchedule": 0,
        "IsOpenPaymentSchedule": False,
        "ForcedPaymentMethod": None,
        "FrequencyId": None,
        "StartDate": None,
        "EndDate": None,
        "RecurringDescription": None,
        "InstallmentTotal": None,
        "_is_test": credential(data, "IsTest").lower() not in ("false", "0"),
    }
    storage.create(link)
    return result(message="PAYMENT LINK SUCCESSFULLY CREATED.", link=link)


@router.get("/api/PaymentLinks/GetPaymentLink")
@router.get("/api/PaymentLinks/GetPaymentLinkById")
async def get_payment_link(request: Request):
    link, error = await authorized_link(request)
    return error or operate(
        link["Id"], "retrieve", lambda current: result(link=current)
    )


@router.get("/api/PaymentLinks/GetPaymentLinks")
@router.get("/api/PaymentLinks")
async def list_payment_links(request: Request):
    data = await parameters(request)
    if not authenticated(data):
        return result(1, "INVALID CREDENTIALS.")
    return {
        "ResponseCode": 200,
        "Msg": ["SUCCESS."],
        "verbiage": None,
        "PaymentLinks": [public(link) for link in storage.list(BMS_CONFIG.mid)],
    }


@router.put("/api/PaymentLinks/DisablePaymentLink")
async def disable_payment_link(request: Request):
    link, error = await authorized_link(request)
    if error:
        return error

    def disable(current):
        if current["Status"] != "Paid" and current["Active"]:
            current["Active"] = False
            current["DisabledOn"] = now()
        return result(link=current)

    return operate(link["Id"], "disable", disable)


@router.get("/api/PaymentLinks/{identifier}")
async def get_payment_link_rest(identifier: str, request: Request):
    link, error = await authorized_link(request, identifier)
    return error or operate(
        link["Id"], "retrieve", lambda current: result(link=current)
    )


@router.get("/payments/link/{identifier}")
async def checkout(identifier: str, request: Request):
    link = storage.get(identifier)
    if not link:
        return JSONResponse(result(404, "PAYMENT LINK NOT FOUND."), status_code=404)
    return templates.TemplateResponse(
        request=request, name="bms_checkout.html", context={"link": public(link)}
    )


@router.post("/mock-bms/payment-links/{identifier}/simulate")
async def simulate_payment(
    identifier: str, request: Request, background_tasks: BackgroundTasks
):
    data = await parameters(request)
    outcome = (data or {}).get("outcome")
    if outcome not in ("success", "decline", "cancel"):
        return JSONResponse(
            result(506, "USE success, decline OR cancel."), status_code=400
        )

    event_ids = []

    def simulate(link):
        if link["Status"] == "Paid":
            return result(link=link, message="PAYMENT ALREADY COMPLETED.")
        if not link["Active"]:
            return result(506, "PAYMENT LINK DISABLED.", link)
        if outcome == "success":
            link.update(
                Status="Paid", PaidOn=now(), ServiceReferenceNumber=str(uuid4())
            )
        if outcome in ("success", "decline"):
            event = new_event(link, outcome)
            link.setdefault("_webhooks", []).append(event)
            event_ids.append(event["id"])
        # A declined attempt or leaving checkout does not cancel the reusable unpaid link.
        return result(
            link=link,
            message={
                "success": "PAYMENT COMPLETED.",
                "decline": "PAYMENT DECLINED.",
                "cancel": "CHECKOUT CANCELLED.",
            }[outcome],
        )

    response = storage.update(identifier, simulate)
    if response is None:
        return JSONResponse(result(404, "PAYMENT LINK NOT FOUND."), status_code=404)
    for event_id in event_ids:
        background_tasks.add_task(deliver, storage, identifier, event_id)
    response["mock_webhook_event_ids"] = event_ids
    return response


@router.get("/mock-bms/payment-links/{identifier}/webhooks")
async def list_webhooks(identifier: str):
    link = storage.get(identifier)
    if not link:
        return JSONResponse(result(404, "PAYMENT LINK NOT FOUND."), status_code=404)
    return {"events": link.get("_webhooks", [])}


@router.post(
    "/mock-bms/payment-links/{identifier}/webhooks/{event_id}/send", status_code=202
)
async def resend_webhook(
    identifier: str, event_id: str, background_tasks: BackgroundTasks
):
    link = storage.get(identifier)
    if not link or not find_event(link, event_id):
        return JSONResponse(result(404, "WEBHOOK EVENT NOT FOUND."), status_code=404)
    background_tasks.add_task(deliver, storage, identifier, event_id)
    return {"event_id": event_id, "queued": True}


@router.post("/mock-bms/payment-links/{identifier}/failure")
async def configure_failure(identifier: str, request: Request):
    """Local test control: fail the next N retrieve/disable calls to exercise retries."""
    data = await parameters(request)
    if not isinstance(data, dict) or data.get("operation") not in (
        "retrieve",
        "disable",
    ):
        return JSONResponse(result(506, "INVALID OPERATION."), status_code=400)
    code, remaining = data.get("response_code", 300), data.get("remaining", 1)
    if (
        type(code) is not int
        or code not in (1, 11, 28, 300, 404, 506)
        or type(remaining) is not int
        or not 0 <= remaining <= 100
    ):
        return JSONResponse(result(506, "INVALID FAILURE SETTINGS."), status_code=400)

    def configure(link):
        link.setdefault("_failures", {})[data["operation"]] = {
            "response_code": code,
            "remaining": remaining,
        }
        return result(link=link)

    return storage.update(identifier, configure) or result(
        404, "PAYMENT LINK NOT FOUND."
    )
