"""Orders v2 lifecycle and controllable incidents for the CrinnoPayments integration."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from app import app
from mock_controls import (
    ControlRequest,
    DEFAULTS,
    SCENARIOS,
    configure,
    consume_fault,
    corrupt_payload,
    fault_response,
    get_config,
    note,
    paypal_error,
    resource_config,
)
from paypal import paypal as core

CAPTURE_LOCKS: dict[str, asyncio.Lock] = {}


def save(order: dict) -> None:
    """Persist the financial state before exposing it to a callback or a subsequent query."""
    core.ORDERS_PAYPAL[order["id"]] = order


def public_order(order: dict) -> dict:
    """Return captures on GET as well as POST, so recovery can verify prior payment."""
    unit = {
        "reference_id": order["reference_id"],
        "amount": {"currency_code": order["currency"], "value": order["amount"]},
    }
    if order.get("capture"):
        unit["payments"] = {"captures": [deepcopy(order["capture"])]}
    result = {
        "id": order["id"],
        "intent": "CAPTURE",
        "status": order["status"],
        "create_time": order["created_at"],
        "purchase_units": [unit],
        "payment_source": {"paypal": {"email_address": "customer@example.com"}},
        "links": [
            {
                "href": f"{core.PAYPAL_MOCK_PUBLIC_BASE_URL}/v2/checkout/orders/{order['id']}",
                "rel": "self",
                "method": "GET",
            }
        ],
    }
    if order["status"] == "APPROVED" and not order.get("capture"):
        result["links"].append(
            {
                "href": f"{core.PAYPAL_MOCK_PUBLIC_BASE_URL}/v2/checkout/orders/{order['id']}/capture",
                "rel": "capture",
                "method": "POST",
            }
        )
    if status := order.get("mock", {}).get("three_ds_status"):
        result["payment_source"]["card"] = {
            "last_digits": "4242",
            "brand": "VISA",
            "authentication_result": {
                "three_d_secure": {
                    "authentication_status": status,
                    "enrollment_status": "Y",
                }
            },
        }
    return result


def find_order(order_id: str) -> dict | None:
    """Migrate older mock records lazily when they are first used by the new controls."""
    order = core.ORDERS_PAYPAL.get(order_id)
    if order is not None:
        order.setdefault("mock", resource_config("paypal"))
        order.setdefault("receipts", {})
        if order.get("capture_id") and "capture" not in order:
            order["capture"] = make_capture(order, "COMPLETED")
        save(order)
    return order


def missing() -> Response:
    """Use the structured 404 that CrinnoPayments distinguishes from an upstream HTML 404."""
    return paypal_error("RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", 404)


async def global_fault(operation: str) -> Response | None:
    """Consume errors for operations that have no order yet, such as OAuth or creation."""
    config = get_config("paypal")
    fault = consume_fault(config, operation)
    DEFAULTS["paypal"]["defaults"] = config
    if operation == "oauth" and fault and fault.get("kind", "error") == "error":
        return JSONResponse(
            {
                "error": "invalid_client",
                "error_description": "Forced OAuth mock failure",
            },
            status_code=fault.get("http_status", 401),
        )
    return await fault_response("paypal", fault)


async def operation_fault(
    order: dict, operation: str
) -> tuple[Response | None, dict | None]:
    """Record each call and commit fault counters before delaying or returning a forced response."""
    config = order["mock"]
    note(config, operation, order["status"])
    fault = consume_fault(config, operation)
    save(order)
    if unavailable := config.get("unavailable"):
        return paypal_error(unavailable, "INVALID_RESOURCE_ID", 404), fault
    return await fault_response("paypal", fault), fault


async def create_order(body: core.CaptureRequest) -> Response:
    """Create a checkout with its own snapshot of the currently selected scenario."""
    if response := await global_fault("create"):
        return response
    for unit in body.purchase_units:
        if unit.amount.currency_code == "CLP" and "." in unit.amount.value:
            return JSONResponse(
                core.build_decimals_not_supported_response(unit), status_code=422
            )
    created = core.build_mock_response_checkout(body)
    order = core.ORDERS_PAYPAL[created["id"]]
    order["mock"] = resource_config("paypal")
    order["receipts"] = {}
    save(order)
    return JSONResponse(created, status_code=201)


async def details(order_id: str) -> Response:
    """Expose approved, pending, paid and unavailable orders without inventing payment success."""
    order = find_order(order_id)
    if order is None:
        return missing()
    response, fault = await operation_fault(order, "get")
    if response is not None:
        return response
    # Retain the legacy switches, but make their counters per-order.
    config = order["mock"]
    if (
        core.DATA_PAYPAL.get("fallar_primer_intento_get_order")
        and config["calls"]["get"] == 1
    ):
        return missing()
    payload = public_order(order)
    if core.DATA_PAYPAL.get("fallar_estado_get_order") and config["calls"]["get"] == 1:
        payload["status"] = "OTRO"
    return JSONResponse(corrupt_payload(payload, fault, provider="paypal"))


def make_capture(order: dict, status: str) -> dict:
    """Keep the same capture ID and financial amount throughout all later queries."""
    return {
        "id": order.get("capture_id") or uuid4().hex[:17].upper(),
        "status": status,
        "amount": {"currency_code": order["currency"], "value": order["amount"]},
        "final_capture": True,
        "create_time": core.paypal_datetime(),
        "update_time": core.paypal_datetime(),
        "supplementary_data": {"related_ids": {"order_id": order["id"]}},
    }


def capture_event(order: dict) -> None:
    """Schedule the same signed financial events the production integration consumes."""
    capture = order["capture"]
    event_type = {
        "COMPLETED": "PAYMENT.CAPTURE.COMPLETED",
        "PENDING": "PAYMENT.CAPTURE.PENDING",
        "DECLINED": "PAYMENT.CAPTURE.DENIED",
    }.get(capture["status"])
    if event_type:
        core.schedule_webhook(
            event_type=event_type,
            resource_type="capture",
            summary=f"Mock capture {capture['status']}",
            resource=deepcopy(capture),
            mode=order["mock"].get("webhook_mode", "normal"),
            delay=order["mock"].get("webhook_delay_seconds", 0),
        )


async def capture(order_id: str, authorization: str, request_id: str) -> Response:
    """Reject unapproved or already captured orders; replay the same idempotency result safely."""
    if not authorization.startswith("Bearer ") or not authorization[7:].strip():
        return paypal_error("AUTHENTICATION_FAILURE", "NOT_AUTHORIZED", 401)
    if not request_id.strip() or len(request_id) > 108:
        return paypal_error("INVALID_REQUEST", "INVALID_REQUEST", 400)
    lock = CAPTURE_LOCKS.setdefault(order_id, asyncio.Lock())
    async with lock:
        order = find_order(order_id)
        if order is None:
            return missing()
        response, fault = await operation_fault(order, "capture")
        if unavailable := order["mock"].get("unavailable"):
            return paypal_error(unavailable, "INVALID_RESOURCE_ID", 404)
        # Idempotency receipts take precedence over injected errors on repeat requests.
        if receipt := order["receipts"].get(request_id):
            return JSONResponse(receipt, status_code=200)
        if order.get("capture"):
            return paypal_error("UNPROCESSABLE_ENTITY", "ORDER_ALREADY_CAPTURED", 422)
        if order["status"] != "APPROVED":
            return paypal_error("UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED", 422)
        if response is not None:
            return response
        if core.DATA_PAYPAL.get("fallar_captura"):
            return paypal_error("UNPROCESSABLE_ENTITY", "TRANSACTION_REFUSED", 422)
        if (
            core.DATA_PAYPAL.get("fallar_primer_intento_capture_order")
            and order["mock"]["calls"]["capture"] == 1
        ):
            return paypal_error("INTERNAL_SERVER_ERROR", "INTERNAL_SERVER_ERROR", 502)
        outcome = order["mock"].get("capture_result", "completed")
        status = {"pending": "PENDING", "declined": "DECLINED"}.get(
            outcome, "COMPLETED"
        )
        order["capture"] = make_capture(order, status)
        order["capture_id"] = order["capture"]["id"]
        if status == "PENDING":
            order["capture"]["status_details"] = {"reason": "PENDING_REVIEW"}
        if status == "DECLINED":
            order["capture"]["status_details"] = {
                "reason": "DECLINED_BY_RISK_FRAUD_FILTERS"
            }
        if code := order["mock"].get("processor_code"):
            order["capture"]["processor_response"] = {"response_code": code}
        order["status"] = "COMPLETED"
        order["mock"]["financial_captures"] = int(status == "COMPLETED")
        payload = public_order(order)
        delivered = corrupt_payload(payload, fault, provider="paypal")
        order["receipts"][request_id] = delivered
        save(order)
        capture_event(order)
        if outcome == "lost_response":
            return paypal_error("SERVICE_UNAVAILABLE", "SERVICE_UNAVAILABLE", 503)
        return JSONResponse(delivered, status_code=201)


async def approve(order_id: str) -> Response:
    """Approve without charging; scenario controls determine whether a notification is delivered."""
    order = find_order(order_id)
    if order is None:
        return missing()
    if order["mock"].get("unavailable"):
        return missing()
    if order["status"] in {"COMPLETED", "VOIDED"}:
        return HTMLResponse(core._legacy_checkout_html(order))
    if order["mock"].get("three_ds_status"):
        order["status"] = "PAYER_ACTION_REQUIRED"
        save(order)
        return HTMLResponse(core._legacy_checkout_html(order))
    order["status"] = "APPROVED"
    first = not order.get("approved_webhook_sent")
    order["approved_webhook_sent"] = True
    save(order)
    if first:
        core.schedule_webhook(
            event_type="CHECKOUT.ORDER.APPROVED",
            resource_type="checkout-order",
            summary="An order has been approved by buyer",
            resource=core._legacy_approved_webhook_resource(order),
            mode=order["mock"].get("approval_webhook_mode", "normal"),
            delay=order["mock"].get("webhook_delay_seconds", 0),
        )
    return HTMLResponse(core._legacy_checkout_html(order))


@app.get("/mock/paypal/controls")
async def controls() -> dict:
    """Show defaults, identifiers, state and counters without creating or capturing orders."""
    return {
        "defaults": get_config("paypal"),
        "scenarios": SCENARIOS["paypal"],
        "resources": list(core.ORDERS_PAYPAL.values()),
    }


@app.post("/mock/paypal/controls")
async def update_controls(data: ControlRequest) -> dict:
    """Apply one scenario to an existing order or to future orders."""
    if data.resource_id:
        async with CAPTURE_LOCKS.setdefault(data.resource_id, asyncio.Lock()):
            return update_order_controls(data)
    if data.status:
        raise HTTPException(
            422, "El estado solo puede cambiarse para una orden concreta"
        )
    return configure("paypal", data)


def update_order_controls(data: ControlRequest) -> dict:
    """Apply controls while the caller holds the same lock as capture."""
    order = find_order(data.resource_id)
    if order is None:
        raise HTTPException(404, "Orden del mock no encontrada")
    if (
        data.status
        and order["status"] in {"COMPLETED", "VOIDED"}
        and data.status != order["status"]
    ):
        raise HTTPException(
            409,
            "Una orden final no puede volver a un estado cobrable; crea otro intento",
        )
    if data.status and data.status not in {
        "CREATED",
        "SAVED",
        "PAYER_ACTION_REQUIRED",
        "APPROVED",
        "VOIDED",
        "COMPLETED",
    }:
        raise HTTPException(422, "Estado de orden no válido")
    order["mock"] = configure("paypal", data, order["mock"])
    if data.status:
        order["status"] = data.status
        if data.status == "COMPLETED" and not order.get("capture"):
            order["capture"] = make_capture(order, "COMPLETED")
            order["capture_id"] = order["capture"]["id"]
            order["mock"]["financial_captures"] = 1
    save(order)
    return order


class ReplayRequest(BaseModel):
    """Replay only the event matching the chosen recorded stage, including duplicate/late deliveries."""

    event: Literal["approval", "capture"] = "approval"


@app.post("/mock/paypal/orders/{order_id}/replay")
async def replay(order_id: str, body: ReplayRequest) -> dict:
    """Send the historical approval even after capture to exercise duplicate-capture guards."""
    order = find_order(order_id)
    if order is None:
        raise HTTPException(404, "Orden del mock no encontrada")
    if body.event == "capture" and order.get("capture"):
        capture_event(order)
    elif body.event == "approval" and (
        order["status"] == "APPROVED" or order.get("capture")
    ):
        core.schedule_webhook(
            event_type="CHECKOUT.ORDER.APPROVED",
            resource_type="checkout-order",
            summary="Replayed mock approval",
            resource=core._legacy_approved_webhook_resource(order),
            mode=order["mock"].get("approval_webhook_mode", "normal"),
        )
    else:
        raise HTTPException(
            409, "Este evento todavía no corresponde al estado de la orden"
        )
    return {"queued": True, "order_id": order_id, "event": body.event}


class ResolveCapture(BaseModel):
    """Resolve a pending capture without a second capture POST."""

    status: Literal["COMPLETED", "DECLINED"]
    notify: bool = True


@app.post("/mock/paypal/orders/{order_id}/resolve-capture")
async def resolve_capture(order_id: str, data: ResolveCapture) -> dict:
    order = find_order(order_id)
    if order is None:
        raise HTTPException(404, "Orden del mock no encontrada")
    if not order.get("capture") or order["capture"]["status"] != "PENDING":
        raise HTTPException(409, "Solo se puede resolver una captura pendiente")
    order["capture"]["status"] = data.status
    order["capture"].pop("status_details", None)
    order["capture"]["update_time"] = core.paypal_datetime()
    order["mock"]["financial_captures"] = int(data.status == "COMPLETED")
    save(order)
    if data.notify:
        capture_event(order)
    return public_order(order)
