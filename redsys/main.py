from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import List, Optional
import asyncio
from config import settings
from .schemas import (
    WebhookSimulationRequest,
    WebhookSimulationResponse,
    OrderStatusResponse,
    RedsysResponseCodes,
    PaymentStatus,
    RedsysResponseCode,
    FormParseRequest,
    FormParseResponse,
)
from .services import RedsysWebhookSimulator
from fastapi.responses import FileResponse
from app import app

# app = FastAPI(
#     title="Redsys Testing API",
#     description="API para simular webhooks de Redsys en entorno de testing",
#     version="1.0.0",
# )

# # CORS para permitir acceso desde el frontend
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# # Montar archivos estáticos (CSS local)
# app.mount("/static", StaticFiles(directory=str(settings.STATIC_DIR)), name="static")

# # Configurar templates
# templates = Jinja2Templates(directory=str(settings.TEMPLATES_DIR))

simulator = RedsysWebhookSimulator()

# ============================================================================
# ENDPOINTS PRINCIPALES
# ============================================================================

@app.post(
    "/parse-form",
    response_model=FormParseResponse,
    summary="Parsear Formulario Redsys",
    description="Extrae los datos del formulario HTML de Redsys"
)
async def parse_form(request: FormParseRequest):
    """
    Parsea el formulario HTML de Redsys y extrae los datos necesarios
    """
    print("Entra aqui?????????")
    result = simulator.parse_form(request.form_html)
    print("paso por el simulator.parse_form????")
    
    if result["success"]:
        return FormParseResponse(
            success=True,
            message="Formulario parseado correctamente",
            data=result["data"]
        )
    else:
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Error parseando formulario")
        )

@app.post(
    "/simulate-webhook",
    response_model=WebhookSimulationResponse,
    summary="Simular Webhook de Redsys",
    description="""
    Simula el envío de un webhook de Redsys a tu API Django.
    **Códigos de respuesta comunes:**
    - `0000-0099`: Pago exitoso
    - `0400`: Pago cancelado
    - `9915`: Cancelado por usuario
    - `0101-0199`: Errores de tarjeta
    - `9000-9999`: Errores del sistema
    """
)
async def simulate_webhook(request: WebhookSimulationRequest):
    """
    Simula un webhook de Redsys y lo envía a la API Django
    """
    print("o en este?????????")
    amount = request.amount
    currency = request.currency

    if not amount or not currency:
        raise HTTPException(
            status_code=502,
            detail=f"Error enviando webhook a Django: amount {amount} | currency {currency}"
        )
    
    
    # currency_map = {"USD": "840", "EUR": "978", "MXN": "484"}
    # currency = currency_map.get(currency, "840")
    
    # Si no se proporciona amount, intentar obtenerlo de la orden
    # if not amount:
    #     order_info = await simulator.get_order_status(request.order_number)
    #     if order_info.get("success") and order_info.get("data"):
    #         amount = str(int(float(order_info["data"].get("amount_to_pay", "0")) * 100))
    #         currency_code = order_info["data"].get("currency", "USD")
    #         currency_map = {"USD": "840", "EUR": "978", "MXN": "484"}
    #         currency = currency_map.get(currency_code, "840")
    
    # Enviar webhook simulado
    result = await simulator.send_webhook_to_django(
        order_number=request.order_number,
        response_code=request.response_code,
        amount=amount,
        currency=currency,
        simulate_signature=request.simulate_signature
    )
    
    if not result.get("success"):
        raise HTTPException(
            status_code=502,
            detail=f"Error enviando webhook a Django: {result.get('error', 'Unknown error')}"
        )
    
    payment_status = simulator.get_payment_status(request.response_code)
    
    return WebhookSimulationResponse(
        success=True,
        message=f"Webhook simulado enviado correctamente. Código: {request.response_code}",
        order_number=request.order_number,
        payment_status=payment_status,
        webhook_event_id=result.get("response_data", {}).get("id"),
        django_response_status=result.get("status_code"),
        raw_webhook_data=result.get("decoded_params"),
    )

@app.get(
    "/order-status/{order_number}",
    response_model=OrderStatusResponse,
    summary="Obtener Estado de Orden",
    description="Consulta el estado actual de una orden en la API Django"
)
async def get_order_status(order_number: str):
    """
    Obtiene el estado actual de una orden desde la API Django
    """
    result = await simulator.get_order_status(order_number)
    if not result.get("success"):
        raise HTTPException(
            status_code=404,
            detail=f"Orden no encontrada o error: {result.get('error', 'Unknown error')}"
        )
    
    data = result.get("data", {})
    return OrderStatusResponse(
        order_number=data.get("order_number", order_number),
        request_status=data.get("request_status", "unknown"),
        payment_status=data.get("payment_status"),
        amount_to_pay=data.get("amount_to_pay", "0"),
        currency=data.get("currency", "USD"),
        created=data.get("created", "2026-01-01T00:00:00Z"),
        checkout_url=data.get("checkout_url"),
    )

@app.get(
    "/response-codes",
    response_model=RedsysResponseCodes,
    summary="Códigos de Respuesta Redsys",
    description="Lista de códigos de respuesta comunes de Redsys para testing"
)
async def get_response_codes():
    """
    Retorna los códigos de respuesta de Redsys clasificados por tipo
    """
    success_codes = [
        RedsysResponseCode(code=code, description=info["description"], status=PaymentStatus(info["status"]))
        for code, info in simulator.RESPONSE_CODES.items()
        if info["status"] == "paid"
    ]
    error_codes = [
        RedsysResponseCode(code=code, description=info["description"], status=PaymentStatus(info["status"]))
        for code, info in simulator.RESPONSE_CODES.items()
        if info["status"] == "failed"
    ]
    cancel_codes = [
        RedsysResponseCode(code=code, description=info["description"], status=PaymentStatus(info["status"]))
        for code, info in simulator.RESPONSE_CODES.items()
        if info["status"] == "cancelled"
    ]
    
    return RedsysResponseCodes(
        success_codes=success_codes,
        error_codes=error_codes,
        cancel_codes=cancel_codes,
    )

@app.post(
    "/simulate-success/{order_number}",
    response_model=WebhookSimulationResponse,
    summary="Simular Pago Exitoso",
    description="Atajo para simular un pago exitoso (código 0000)"
)
async def simulate_success(order_number: str):
    """Simula un pago exitoso"""
    request = WebhookSimulationRequest(
        order_number=order_number,
        response_code="0000",
        simulate_signature=True
    )
    return await simulate_webhook(request)

@app.post(
    "/simulate-failure/{order_number}",
    response_model=WebhookSimulationResponse,
    summary="Simular Pago Fallido",
    description="Atajo para simular un pago fallido (código 0101 - Tarjeta caducada)"
)
async def simulate_failure(order_number: str):
    """Simula un pago fallido"""
    request = WebhookSimulationRequest(
        order_number=order_number,
        response_code="0101",
        simulate_signature=True
    )
    return await simulate_webhook(request)

@app.post(
    "/simulate-cancelled/{order_number}",
    response_model=WebhookSimulationResponse,
    summary="Simular Pago Cancelado",
    description="Atajo para simular un pago cancelado (código 9915)"
)
async def simulate_cancelled(order_number: str):
    """Simula un pago cancelado"""
    request = WebhookSimulationRequest(
        order_number=order_number,
        response_code="9915",
        simulate_signature=True
    )
    return await simulate_webhook(request)

# ============================================================================
# INTERFAZ WEB PARA TESTING (CON CSS LOCAL)
# ============================================================================

@app.get("/redsys", response_class=HTMLResponse, include_in_schema=False)
async def testing_ui(request: Request):
    """Interfaz web para testing de webhooks"""
    template_path = settings.TEMPLATES_DIR / "redsys.html"
    return FileResponse(template_path)
    # return templates.TemplateResponse("index.html", {
    #     "request": request,
    #     "api_base": "http://localhost:8000"
    # })

# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "django_api_url": settings.DJANGO_API_URL,
        "redsys_merchant_key_configured": bool(settings.REDSYS_TEST_MERCHANT_KEY),
        "static_dir_exists": settings.STATIC_DIR.exists(),
        "templates_dir_exists": settings.TEMPLATES_DIR.exists(),
    }

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)