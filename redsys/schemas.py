from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime
from enum import Enum

class PaymentStatus(str, Enum):
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AUTHORIZED = "authorized"

class RedsysResponseCode(BaseModel):
    code: str
    description: str
    status: PaymentStatus

class WebhookSimulationRequest(BaseModel):
    order_number: str = Field(..., description="Número de orden a simular")
    response_code: str = Field(default="0000", description="Código de respuesta de Redsys")
    amount: Optional[str] = Field(default=None, description="Monto de la transacción")
    currency: Optional[str] = Field(default="840", description="Código de moneda")
    transaction_type: Optional[str] = Field(default="0", description="Tipo de transacción")
    merchant_code: Optional[str] = Field(default=None, description="Código de comercio")
    terminal: Optional[str] = Field(default=None, description="Terminal")
    simulate_signature: bool = Field(default=True, description="Si True, genera signature válida")

class WebhookSimulationResponse(BaseModel):
    success: bool
    message: str
    order_number: str
    payment_status: str
    webhook_event_id: Optional[str] = None
    django_response_status: int
    raw_webhook_data: Optional[Dict[str, Any]] = None

class OrderStatusResponse(BaseModel):
    order_number: str
    request_status: str
    payment_status: Optional[str]
    amount_to_pay: str
    currency: str
    created: datetime
    checkout_url: Optional[str]

class RedsysResponseCodes(BaseModel):
    success_codes: list[RedsysResponseCode]
    error_codes: list[RedsysResponseCode]
    cancel_codes: list[RedsysResponseCode]

class ParsedFormData(BaseModel):
    """Datos parseados del formulario HTML de Redsys"""
    order_number: str
    payment_id: str
    signature_version: str
    merchant_parameters: str
    signature: str
    action_url: str
    decoded_params: Optional[Dict[str, Any]] = None

class FormParseRequest(BaseModel):
    """Request para parsear formulario HTML"""
    form_html: str = Field(..., description="HTML completo del formulario de Redsys")

class FormParseResponse(BaseModel):
    """Respuesta con datos parseados del formulario"""
    success: bool
    message: str
    data: Optional[ParsedFormData] = None