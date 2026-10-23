"""Deterministic scenarios shared by the PayPal and VirtualPOS local mocks."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app import app, templates
from mock_storage import ResourceStore


class Fault(BaseModel):
    """One operation's fault, consumed per call; -1 repeats until explicitly cleared."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "error",
        "html404",
        "invalid_json",
        "delay",
        "wrong_amount",
        "wrong_currency",
        "wrong_id",
        "wrong_reference",
    ] = "error"
    name: str = Field(default="UNPROCESSABLE_ENTITY", max_length=100)
    issue: str = Field(default="INSTRUMENT_DECLINED", max_length=100)
    http_status: int = Field(default=422, ge=400, le=599)
    times: int = Field(default=-1, ge=-1, le=1000)
    delay_seconds: float = Field(default=0, ge=0, le=30)


class ControlRequest(BaseModel):
    """Configure future resources or one existing payment without touching unrelated payments."""

    model_config = ConfigDict(extra="forbid")
    resource_id: str | None = Field(default=None, max_length=127)
    scenario: str | None = None
    faults: dict[str, Fault] | None = None
    webhook_mode: (
        Literal["normal", "suppress", "duplicate", "wrong_uuid", "bad_signature"] | None
    ) = None
    approval_webhook_mode: (
        Literal["normal", "suppress", "duplicate", "bad_signature"] | None
    ) = None
    webhook_delay_seconds: float | None = Field(default=None, ge=0, le=20)
    status: str | None = None
    rejection_code: str | None = Field(default=None, pattern=r"^-\d{1,4}$")
    capture_result: (
        Literal["completed", "pending", "declined", "lost_response"] | None
    ) = None
    processor_code: str | None = Field(default=None, max_length=4)
    three_ds_status: Literal["N", "R", "U", "C"] | None = None


SCENARIOS = {
    "paypal": {
        "normal": {"label": "Flujo normal", "config": {}},
        "approved_no_webhook": {
            "label": "Aprobación sin webhook: recuperación manual/API",
            "config": {"approval_webhook_mode": "suppress"},
        },
        "no_webhook": {
            "label": "Sin notificaciones",
            "config": {"webhook_mode": "suppress", "approval_webhook_mode": "suppress"},
        },
        "duplicate_webhook": {
            "label": "Notificaciones duplicadas",
            "config": {
                "webhook_mode": "duplicate",
                "approval_webhook_mode": "duplicate",
            },
        },
        "invalid_signature": {
            "label": "Firma incorrecta del webhook",
            "config": {
                "webhook_mode": "bad_signature",
                "approval_webhook_mode": "bad_signature",
            },
        },
        "unavailable": {
            "label": "Orden no disponible: 404 RESOURCE_NOT_FOUND",
            "config": {"unavailable": "RESOURCE_NOT_FOUND"},
        },
        "invalid_resource": {
            "label": "404 INVALID_RESOURCE_ID",
            "config": {"unavailable": "INVALID_RESOURCE_ID"},
        },
        "html404": {
            "label": "404 HTML inconcluyente",
            "config": {"faults": {"get": {"kind": "html404"}}},
        },
        "invalid_json": {
            "label": "Consulta con respuesta ilegible",
            "config": {"faults": {"get": {"kind": "invalid_json"}}},
        },
        "get_unavailable": {
            "label": "Consulta: servicio no disponible (503)",
            "config": {
                "faults": {
                    "get": {
                        "name": "SERVICE_UNAVAILABLE",
                        "http_status": 503,
                        "issue": "SERVICE_UNAVAILABLE",
                    }
                }
            },
        },
        "capture_declined": {
            "label": "Captura: INSTRUMENT_DECLINED",
            "config": {"faults": {"capture": {"issue": "INSTRUMENT_DECLINED"}}},
        },
        "capture_card_expired": {
            "label": "Captura: CARD_EXPIRED",
            "config": {"faults": {"capture": {"issue": "CARD_EXPIRED"}}},
        },
        "capture_denied": {
            "label": "Captura recibida pero DECLINED",
            "config": {"capture_result": "declined"},
        },
        "capture_pending": {
            "label": "Captura PENDING: cobro sin confirmar",
            "config": {"capture_result": "pending"},
        },
        "capture_lost_response": {
            "label": "Cobro completado pero respuesta HTTP fallida",
            "config": {"capture_result": "lost_response", "webhook_mode": "suppress"},
        },
        "processor_declined": {
            "label": "Rechazo del procesador (5120)",
            "config": {"capture_result": "declined", "processor_code": "5120"},
        },
        "three_ds_failed": {
            "label": "3DS no completado: continúa esperando al cliente",
            "config": {"three_ds_status": "N", "approval_webhook_mode": "suppress"},
        },
        "wrong_amount": {
            "label": "Captura completada con importe de respuesta incorrecto",
            "config": {
                "faults": {
                    "capture": {"kind": "wrong_amount"},
                    "get": {"kind": "wrong_amount"},
                }
            },
        },
        "create_error": {
            "label": "Error al crear la orden",
            "config": {"faults": {"create": {"issue": "PAYEE_ACCOUNT_RESTRICTED"}}},
        },
        "oauth_error": {
            "label": "Credenciales rechazadas (OAuth)",
            "config": {
                "faults": {
                    "oauth": {
                        "name": "INVALID_CLIENT",
                        "issue": "INVALID_CLIENT",
                        "http_status": 401,
                    }
                }
            },
        },
    },
    "virtualpos": {
        "normal": {"label": "Flujo normal", "config": {}},
        "reject_authentication": {
            "label": "Rechazo -5: autenticación bancaria",
            "config": {"checkout_status": "rechazado", "rejection_code": "-5"},
        },
        "reject_blocked_card": {
            "label": "Rechazo -7: tarjeta bloqueada",
            "config": {"checkout_status": "rechazado", "rejection_code": "-7"},
        },
        "reject_expired_card": {
            "label": "Rechazo -8: tarjeta vencida",
            "config": {"checkout_status": "rechazado", "rejection_code": "-8"},
        },
        "reject_security": {
            "label": "Rechazo -1017: validación de seguridad",
            "config": {"checkout_status": "rechazado", "rejection_code": "-1017"},
        },
        "reject_back": {
            "label": "Rechazo -1018: botón volver",
            "config": {"checkout_status": "rechazado", "rejection_code": "-1018"},
        },
        "pending": {
            "label": "Pendiente de autorización",
            "config": {"checkout_status": "pendiente"},
        },
        "retrying": {
            "label": "Autorización reintentando",
            "config": {"checkout_status": "reintentando"},
        },
        "expired": {
            "label": "Pago expirado",
            "config": {"checkout_status": "expirado"},
        },
        "cancelled": {
            "label": "Pago pendiente cancelado",
            "config": {"checkout_status": "cancelado"},
        },
        "no_webhook": {
            "label": "Pago sin notificación",
            "config": {"webhook_mode": "suppress"},
        },
        "duplicate_webhook": {
            "label": "Notificación duplicada",
            "config": {"webhook_mode": "duplicate"},
        },
        "wrong_callback_uuid": {
            "label": "UUID incorrecto en notificación",
            "config": {"webhook_mode": "wrong_uuid"},
        },
        "verify_error": {
            "label": "Consulta de verificación falla: E-021",
            "config": {"faults": {"get": {"issue": "E-021", "http_status": 400}}},
        },
        "create_error": {
            "label": "Error al crear el Payment: E-003",
            "config": {"faults": {"create": {"issue": "E-003", "http_status": 400}}},
        },
        "checkout_error": {
            "label": "Error al crear el checkout: E-027",
            "config": {"faults": {"checkout": {"issue": "E-027", "http_status": 400}}},
        },
        "wrong_amount": {
            "label": "Respuesta con importe incorrecto",
            "config": {"faults": {"get": {"kind": "wrong_amount"}}},
        },
        "wrong_uuid": {
            "label": "Respuesta con UUID incorrecto",
            "config": {"faults": {"get": {"kind": "wrong_id"}}},
        },
        "invalid_json": {
            "label": "Respuesta ilegible al consultar",
            "config": {"faults": {"get": {"kind": "invalid_json"}}},
        },
    },
}
DEFAULTS = {provider: ResourceStore(provider, "controls") for provider in SCENARIOS}


def default_config() -> dict:
    """All faults and notifications are normal unless selected explicitly."""
    return {
        "scenario": "normal",
        "faults": {},
        "webhook_mode": "normal",
        "approval_webhook_mode": "normal",
        "webhook_delay_seconds": 0,
    }


def get_config(provider: str) -> dict:
    """Return a private copy so one order cannot consume another order's injected fault."""
    return deepcopy(DEFAULTS[provider].get("defaults", default_config()))


def resource_config(provider: str) -> dict:
    """Snapshot the current defaults and initialize independently counted request history."""
    return {**get_config(provider), "calls": {}, "history": []}


def configure(provider: str, data: ControlRequest, current: dict | None = None) -> dict:
    """Validate controls before changing resources or durable defaults."""
    unsupported = (provider == "paypal" and data.webhook_mode == "wrong_uuid") or (
        provider == "virtualpos"
        and (
            data.webhook_mode == "bad_signature"
            or data.approval_webhook_mode is not None
            or data.capture_result is not None
            or data.processor_code is not None
            or data.three_ds_status is not None
        )
    )
    if unsupported:
        raise HTTPException(422, detail="Control no disponible para este TPV")
    if data.scenario is not None and data.scenario not in SCENARIOS[provider]:
        raise HTTPException(422, detail="Escenario desconocido para este TPV")
    config = deepcopy(current or get_config(provider))
    if data.scenario is not None:
        config = {
            **default_config(),
            **deepcopy(SCENARIOS[provider][data.scenario]["config"]),
            "scenario": data.scenario,
            **{
                key: value
                for key, value in config.items()
                if key
                in {"calls", "history", "financial_captures", "financial_payments"}
            },
        }
    for key, value in data.model_dump(exclude_none=True).items():
        if key not in {"scenario", "status", "resource_id"}:
            config[key] = value
    actions = (
        {"create", "get", "capture", "oauth"}
        if provider == "paypal"
        else {"create", "get", "checkout"}
    )
    if set(config["faults"]) - actions:
        raise HTTPException(422, detail="Operación no disponible para este TPV")
    config["faults"] = {
        action: Fault(**value).model_dump()
        for action, value in config["faults"].items()
    }
    if not current and data.resource_id is None:
        DEFAULTS[provider]["defaults"] = config
    return config


def note(config: dict, operation: str, result: str) -> None:
    """Keep per-resource counters and a bounded audit of simulated operations."""
    calls = config.setdefault("calls", {})
    calls[operation] = calls.get(operation, 0) + 1
    config.setdefault("history", []).append(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            "result": result,
        }
    )
    config["history"] = config["history"][-200:]


def consume_fault(config: dict, operation: str) -> dict | None:
    """Decrement one-shot faults before returning a response or waiting."""
    fault = config.get("faults", {}).get(operation)
    if not fault or fault.get("times", -1) == 0:
        return None
    result = deepcopy(fault)
    if config.get("history") and config["history"][-1]["operation"] == operation:
        config["history"][-1]["fault"] = {
            key: result.get(key) for key in ("kind", "name", "issue", "http_status")
        }
    if fault.get("times", -1) > 0:
        fault["times"] -= 1
    return result


async def fault_response(provider: str, fault: dict | None) -> Response | None:
    """Emit transport-like failures; payload mismatches are applied separately after success."""
    if not fault:
        return None
    kind = fault.get("kind", "error")
    if kind == "delay":
        await asyncio.sleep(fault.get("delay_seconds", 0))
    elif kind == "html404":
        return HTMLResponse("<h1>Mock upstream route not found</h1>", status_code=404)
    elif kind == "invalid_json":
        return Response(
            "{mock:invalid-json", media_type="application/json", status_code=200
        )
    elif kind == "error":
        if provider == "paypal":
            return paypal_error(
                fault.get("name", "UNPROCESSABLE_ENTITY"),
                fault.get("issue", "INSTRUMENT_DECLINED"),
                fault.get("http_status", 422),
            )
        return virtualpos_error(
            fault.get("issue", "E-021"), fault.get("http_status", 400)
        )
    return None


def paypal_error(name: str, issue: str, status: int) -> JSONResponse:
    """Use PayPal's structured error envelope, including a diagnostic ID."""
    from uuid import uuid4

    debug_id = uuid4().hex[:13]
    return JSONResponse(
        {
            "name": name,
            "message": "Mock PayPal operation could not be completed.",
            "debug_id": debug_id,
            "details": [
                {"issue": issue, "description": f"Forced mock scenario: {issue}"}
            ],
        },
        status_code=status,
        headers={"Paypal-Debug-Id": debug_id},
    )


def virtualpos_error(code: str, status: int = 400) -> JSONResponse:
    """Keep API errors separate from financial rejections, as documented by VirtualPOS."""
    messages = {
        "E-001": "Credencial del comercio ausente.",
        "E-002": "Firma de la solicitud ausente.",
        "E-003": "Importe de pago no válido.",
        "E-010": "El importe debe ser numérico.",
        "E-011": "El importe debe ser un entero desde 1000 CLP y dentro del rango int32.",
        "E-036": "El pago no admite una nueva autorización en su estado actual.",
        "E-020": "No se encuentra el Payment solicitado.",
        "E-021": "No se pudo recuperar el Payment.",
        "E-027": "Medio de pago no habilitado para el comercio.",
    }
    return JSONResponse(
        {
            "error": {
                "error_code": code,
                "message": messages.get(code, f"Incidencia simulada {code}."),
                "doc_url": "https://virtualpos.readme.io/reference/objeto-error",
            }
        },
        status_code=status,
    )


def corrupt_payload(payload: dict, fault: dict | None, *, provider: str) -> dict:
    """Alter only the returned data: stored financial truth remains available for later verification."""
    result = deepcopy(payload)
    kind = (fault or {}).get("kind")
    if kind not in {"wrong_amount", "wrong_currency", "wrong_id", "wrong_reference"}:
        return result
    if provider == "virtualpos":
        order = result["payment"]["order"]
        key = {
            "wrong_amount": "amount",
            "wrong_currency": "currency",
            "wrong_id": "uuid",
            "wrong_reference": "merchant_internal_code",
        }[kind]
        order[key] = (
            order.get("amount", 0) + 1 if kind == "wrong_amount" else "MOCK-MISMATCH"
        )
    else:
        for unit in result.get("purchase_units", []):
            if kind == "wrong_reference":
                unit["reference_id"] = "MOCK-MISMATCH"
            amounts = [
                unit.get("amount"),
                *(
                    capture.get("amount")
                    for capture in unit.get("payments", {}).get("captures", [])
                ),
            ]
            for amount in filter(None, amounts):
                if kind == "wrong_amount":
                    from decimal import Decimal

                    amount["value"] = str(Decimal(amount["value"]) + 1)
                elif kind == "wrong_currency":
                    amount["currency_code"] = (
                        "EUR" if amount["currency_code"] != "EUR" else "USD"
                    )
        if kind == "wrong_id":
            result["id"] = "MOCK-MISMATCH"
    return result


@app.get("/mock/scenarios", response_class=HTMLResponse)
async def scenario_dashboard(request: Request):
    """Expose scenario controls without requiring shell calls or editing module globals."""
    return templates.TemplateResponse(
        request=request, name="scenarios.html", context={"catalog": SCENARIOS}
    )
