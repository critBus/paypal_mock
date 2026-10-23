"""VirtualPOS financial outcomes, webhook controls and per-payment incident diagnostics."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from uuid import uuid4

import httpx
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, Response

from app import app
from mock_controls import (
    ControlRequest,
    DEFAULTS,
    SCENARIOS,
    configure,
    consume_fault,
    fault_response,
    get_config,
    note,
    resource_config,
)
from virtualpos import virtualpos as core

REJECTIONS = {
    "-1": "Los datos de la tarjeta no son válidos.",
    "-3": "El banco rechazó el importe por un límite de pago.",
    "-5": "No se pudo completar la autenticación con el banco emisor.",
    "-6": "Rechazo general del pago.",
    "-7": "La tarjeta se encuentra bloqueada.",
    "-8": "La tarjeta está vencida.",
    "-1017": "No se completó la validación de seguridad del cliente.",
    "-1018": "El cliente volvió atrás durante el pago.",
}
LOCKS: dict[str, asyncio.Lock] = {}


def save(record: dict) -> None:
    """Commit the result before sending the callback that queries it."""
    core.PAYMENTS[record["uuid"]] = record


def initialize(record: dict) -> dict:
    """Older records inherit normal controls without losing their original status or authorization."""
    record.setdefault("mock", resource_config("virtualpos"))
    record.setdefault("attempts", [])
    return record


async def global_fault(operation: str) -> Response | None:
    """Inject errors before creating a Payment, without assigning them to unrelated payments."""
    config = get_config("virtualpos")
    fault = consume_fault(config, operation)
    DEFAULTS["virtualpos"]["defaults"] = config
    return await fault_response("virtualpos", fault)


async def operation_fault(
    record: dict, operation: str
) -> tuple[Response | None, dict | None]:
    """Consume independent counters and keep an audit of GET and checkout-link requests."""
    initialize(record)
    note(record["mock"], operation, record["status"])
    fault = consume_fault(record["mock"], operation)
    save(record)
    return await fault_response("virtualpos", fault), fault


async def deliver(record: dict, *, retry: bool = False) -> dict:
    """Model absent, delayed, duplicated and mismatched UUID callbacks without changing the payment."""
    config = record.get("mock", {})
    mode = config.get("webhook_mode", "normal")
    delay = config.get("webhook_delay_seconds", 0)
    if delay:
        await asyncio.sleep(delay)
    result = {}
    for _attempt in range(2 if mode == "duplicate" else 1):
        result = {
            "uuid": record["uuid"],
            "target_url": record.get("callback_url"),
            "status_code": None,
            "response": None,
            "success": False,
            "created_at": core._now(),
            "retry": retry,
            "skipped": mode == "suppress",
        }
        if mode == "suppress" or not record.get("callback_url"):
            result["response"] = (
                "Notificación omitida por el escenario"
                if mode == "suppress"
                else "Payment sin callback_url"
            )
        else:
            callback_uuid = (
                "MOCK-WRONG-UUID" if mode == "wrong_uuid" else record["uuid"]
            )
            try:
                timeout = float(core.os.getenv("MOCK_VIRTUALPOS_WEBHOOK_TIMEOUT", "10"))
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        record["callback_url"], data={"uuid": callback_uuid}
                    )
                result.update(
                    status_code=response.status_code,
                    response=response.text[:1000],
                    success=response.is_success,
                )
            except Exception as exc:
                result["response"] = str(exc)
        core.WEBHOOK_DELIVERIES.append(result)
    return result


async def resolve(payment_uuid: str, successful: bool) -> Response:
    """A financial result is saved once; paid, cancelled and expired payments cannot be paid again."""
    async with LOCKS.setdefault(payment_uuid, asyncio.Lock()):
        record = initialize(core._get_payment(payment_uuid))
        if record["status"] == "pagado":
            return HTMLResponse(core._checkout_html(record))
        if record["status"] in {"expirado", "cancelado"}:
            return core.virtualpos_error("E-036", 400)
        config = record["mock"]
        state = config.get("checkout_status") or (
            "pagado" if successful else "rechazado"
        )
        record["status"] = state
        record["updated_at"] = core._now()
        record.pop("rejected_object", None)
        if state == "pagado":
            record.update(
                authorized_at=core._now(),
                card_number=6623,
                auth_code=uuid4().hex[:6].upper(),
                installment_amount=record["amount"],
                installment_number=1,
                payment_type_code="VN",
            )
            config["financial_payments"] = 1
        else:
            record.update(
                authorized_at=None,
                card_number=6623,
                auth_code=None,
                installment_amount=0,
                installment_number=0,
                payment_type_code=None,
            )
        if state == "rechazado":
            code = config.get("rejection_code", "-6")
            record["rejected_object"] = {
                "error_code": code,
                "message": REJECTIONS.get(code, f"Rechazo simulado {code}"),
                "doc_url": "https://virtualpos.readme.io/reference/objeto-rechazo",
            }
        note(config, "authorize", state)
        record["attempts"].append(
            {
                "at": core._now(),
                "status": state,
                "rejected_object": deepcopy(record.get("rejected_object")),
            }
        )
        save(record)
    # No lock is held during HTTP delivery: the receiver must be able to query this result.
    delivery = await deliver(record)
    return HTMLResponse(core._return_post_html(record, delivery))


@app.get("/mock/virtualpos/controls")
async def controls() -> dict:
    return {
        "defaults": get_config("virtualpos"),
        "scenarios": SCENARIOS["virtualpos"],
        "resources": list(core.PAYMENTS.values()),
    }


@app.post("/mock/virtualpos/controls")
async def update_controls(data: ControlRequest) -> dict:
    """Select the next financial outcome or API fault for this Payment or future Payments."""
    if data.resource_id:
        record = initialize(core._get_payment(data.resource_id))
        if data.status and data.status not in {
            "pendiente",
            "pagado",
            "rechazado",
            "reintentando",
            "expirado",
            "cancelado",
        }:
            raise HTTPException(422, "Estado de Payment no válido")
        if (
            data.status
            and record["status"] in {"pagado", "cancelado", "expirado"}
            and record["status"] != data.status
        ):
            raise HTTPException(
                409, "Un Payment final no puede volver a pagarse; crea otro intento"
            )
        record["mock"] = configure("virtualpos", data, record["mock"])
        if data.status:
            record["status"] = data.status
            record["updated_at"] = core._now()
            if data.status == "pagado":
                record.update(
                    auth_code=record.get("auth_code") or uuid4().hex[:6].upper(),
                    authorized_at=core._now(),
                )
                record["mock"]["financial_payments"] = 1
        save(record)
        return record
    if data.status:
        raise HTTPException(
            422, "El estado solo puede cambiarse para un Payment concreto"
        )
    return configure("virtualpos", data)


@app.post("/mock/virtualpos/payments/{payment_uuid}/replay")
async def replay(payment_uuid: str) -> dict:
    """Retry the callback without running another financial authorization."""
    return await deliver(initialize(core._get_payment(payment_uuid)), retry=True)


@app.get("/mock/virtualpos/payments/{payment_uuid}/history")
async def history(payment_uuid: str) -> dict:
    """Local diagnostic endpoint: request calls, authorization attempts and known financial count."""
    record = initialize(core._get_payment(payment_uuid))
    return {
        "uuid": payment_uuid,
        "mock": record["mock"],
        "attempts": record["attempts"],
    }
