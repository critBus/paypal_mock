from fastapi import FastAPI, HTTPException, Depends, Header, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl, validator
from datetime import datetime, timedelta, timezone
import uuid
import random
import re
from typing import Optional, Annotated, Dict, Any
import os
import json
import hashlib
import httpx

from app import app

# from captura_general.captura_general import *

#app = FastAPI(
#    title="Paylands Mock API",
#    description="API de simulación para integración con Paylands",
#    version="1.0.0"
#)

# Token secreto (en producción usar variables de entorno)
SECRET_TOKEN = os.getenv("PAYLANDS_MOCK_TOKEN", "eltokenausaraqui")

# Base de datos en memoria para almacenar transacciones
in_memory_db: Dict[str, Dict[str, Any]] = {}

# Dependencia para verificar el token de autorización
async def verify_token(authorization: Annotated[str, Header(...)]) -> None:
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Invalid authorization scheme. Expected 'Bearer <token>'"
        )
    token = authorization.split(" ")[1]
    print(f"SECRET_TOKEN {SECRET_TOKEN}")
    if token != SECRET_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing authorization token"
        )

# Mapeo de moneda a código ISO numérico
currency_map = {
    "EUR": "978",
    "USD": "840"
}

class PaylandsRequest(BaseModel):
    signature: str
    amount: float
    operative: str
    secure: bool
    customer_ext_id1: Optional[str] = ""
    service: str
    currency: str
    description: str
    additional1: Optional[str] = ""
    url_post: HttpUrl
    url_ok: HttpUrl
    url_ko: HttpUrl
    template_uuid1: Optional[str] = ""
    dcc_template_uuid1: Optional[str] = ""
    source_uuid1: Optional[str] = ""
    save_card: bool
    reference: str
    dynamic_descriptor1: Optional[str] = ""
    expires_in: int
    @validator('currency')
    def validate_currency(cls, v):
        if not v.upper() in currency_map:
            raise ValueError('Only EUR and USD currency is supported in mock mode')
        return v.upper()
    @validator('amount')
    def validate_amount(cls, v):
        if v <= 0:
            raise ValueError('Amount must be greater than zero')
        if v > 1000000:  # Límite máximo razonable
            raise ValueError('Amount exceeds maximum allowed value')
        return round(v, 2)  # Normalizar a 2 decimales
    @validator('expires_in')
    def validate_expires_in(cls, v):
        if v < 60 or v > 86400:  # Entre 1 minuto y 24 horas
            raise ValueError('expires_in must be between 60 and 86400 seconds')
        return v
    @validator('reference')
    def validate_reference(cls, v):
        if not re.match(r'^[A-Za-z0-9\-_]{1,50}$', v):
            raise ValueError('Invalid reference format. Only alphanumeric, hyphens and underscores allowed (max 50 chars)')
        return v
    @validator('url_post', 'url_ok', 'url_ko', pre=True)
    def strip_urls(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v
    @validator('signature')
    def validate_signature(cls, v):
        if len(v) < 20:
            raise ValueError('Signature too short')
        if not re.match(r'^[A-Za-z0-9+/=]{20,}$', v):
            raise ValueError('Invalid signature format')
        return v

def calculate_validation_hash(order: dict, client: dict, 
                            #   extra_data: Optional[dict], 
                              signature: str) -> str:
    """Calcula el validation_hash según el algoritmo de Paylands"""
    print(f"signature {signature}")
    data_dict = {
        "order": order,
        "client": client
    }
    # if extra_data is not None:
    #     data_dict["extra_data"] = extra_data
    
    # Convertir a JSON sin espacios y sin escapar caracteres Unicode
    data_json = json.dumps(data_dict, ensure_ascii=False, separators=(',', ':'))
    to_hash = data_json + signature
    return hashlib.sha256(to_hash.encode('utf-8')).hexdigest()

def generate_successful_order(initial_order: dict, request: PaylandsRequest, now: datetime, tz) -> dict:
    """Genera un order con estado SUCCESS para el webhook"""
    # Formatear fecha con zona horaria UTC+2
    current_time_str = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    client_timezone_str = now.strftime("%Y-%m-%dT%H:%M:%S+0200")
    
    # Generar UUIDs para la transacción
    transaction_uuid = str(uuid.uuid4()).upper()
    source_uuid = str(uuid.uuid4()).upper()

    # return {
    #     "order": {
    #         "uuid": "BB4460B8-86DD-4F8B-B260-F07127AB5193",
    #         "created": "2026-04-10T22:54:34+0200",
    #         "updated": "2026-04-10T22:56:40+0200",
    #         "created_from_client_timezone": "2026-04-10T22:54:34+0200",
    #         "amount": 100,
    #         "currency": "978",
    #         "paid": true,
    #         "status": "SUCCESS",
    #         "safe": false,
    #         "refunded": 0
    #     },
    #     "client": {
    #         "uuid": "A2065E66-BC2E-4C35-9CB7-35A74E5A5590"
    #     },
    #     "validation_hash": "eb5392635e05476061da97927eebc358b7d97387eef89a31ed5ffd5b09baa8de"
    #     }
    
    return {
        "uuid": initial_order["uuid"],
        "created": initial_order["created"],
        "updated": current_time_str,
        "created_from_client_timezone": client_timezone_str,
        "amount": request.amount,
        "currency": initial_order["currency"],
        "paid": True,
        "status": "SUCCESS",
        "safe": True,
        "refunded": 0,
        "additional": "227610373340",
        "service": "CREDORAX",
        "service_uuid": initial_order["service_uuid"],
        "customer": "user42",
        "cof_txnid": f"202232016000{random.randint(100, 999)}",
        "transactions": [
            {
                "uuid": transaction_uuid,
                "created": initial_order["created"],
                "updated": current_time_str,
                "created_from_client_timezone": client_timezone_str,
                "operative": "AUTHORIZATION",
                "amount": request.amount,
                "authorization": str(random.randint(100000, 999999)),
                "processor_id": "XZZ01d4d229b0d5dB40RPKQCOSFNBGBH",
                "status": "SUCCESS",
                "error": "NONE",
                "source": {
                    "object": "CARD",
                    "uuid": source_uuid,
                    "type": "CREDIT",
                    "token": ''.join(random.choices('0123456789abcdef', k=64)),
                    "brand": "VISA",
                    "country": "MT",
                    "holder": "Miguel C",
                    "bin": 401881,
                    "last4": "0036",
                    "is_saved": True,
                    "expire_month": "12",
                    "expire_year": "34",
                    "additional": None,
                    "bank": "BANK OF VALLETTA P.L.C",
                    "prepaid": False,
                    "validation_date": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "creation_date": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "brand_description": None,
                    "origin": "PAYMENT_CARD",
                    "cof": {
                        "is_available": True
                    }
                },
                "antifraud": None,
                "device": {
                    "fingerprint": "495973560",
                    "user_agent": "Mozilla/5.0 (X11; Linux x86_64; rv:106.0) Gecko/20100101 Firefox/106.0"
                },
                "error_details": None,
                "bizum": {
                    "account": "ES51XXXXXXXXXXXXXXXX0001",
                    "phone_number": "346XXXXX306"
                }
            }
        ],
        "token": None,
        "ip": "127.0.0.1",
        "reference": request.reference,
        "dynamic_descriptor": None,
        "threeds_data": {
            "version": "2.1",
            "flow": "FRICTIONLESS",
            "sca_requested": False,
            "status": "Y",
            "eci": "06",
            "exemption": None
        },
        "dcc": {
            "fee": "3.00 %",
            "change": 0.099415,
            "mode": "LOCAL",
            "selection": "CARD",
            "card_currency": "NOK",
            "merchant_currency": "EUR",
            "ecb_change": None
        }
    }

@app.post("/payment", summary="Simular creación de pago en Paylands")
async def mock_paylands_payment(request: PaylandsRequest, _: Annotated[None, Depends(verify_token)]):
    # Generar timestamps con zona horaria UTC+2 (horario de verano de España)
    tz = timezone(timedelta(hours=2))
    now = datetime.now(tz)
    current_time_str = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    client_timezone_str = now.strftime("%Y-%m-%dT%H:%M:%S+0200")
    
    # Generar identificadores únicos
    order_uuid = str(uuid.uuid4()).upper()
    client_uuid = str(uuid.uuid4()).upper()
    service_uuid = str(uuid.uuid4()).upper()
    
    # Generar token de 64 caracteres hexadecimales
    token = ''.join(random.choices('0123456789abcdef', k=64))
    
    # Preparar datos del order para la respuesta
    order_data = {
        "uuid": order_uuid,
        "created": current_time_str,
        "created_from_client_timezone": client_timezone_str,
        "amount": request.amount,
        "currency": currency_map.get(request.currency, "978"),
        "paid": False,
        "status": "CREATED",
        "safe": False,
        "refunded": 0,
        "additional": None,
        "service": "CREDORAX",
        "service_uuid": service_uuid,
        "customer": None,
        "cof_txnid": None,
        "transactions": [],
        "ip": None,
        "reference": request.reference,
        "dynamic_descriptor": None,
        "threeds_data": None,
        "token": token,
    }
    
    client_data = {
        "uuid": client_uuid
    }
    
    # Calcular validation_hash para la respuesta inicial
    validation_hash = calculate_validation_hash(order_data, client_data, 
                                                # None, 
                                                request.signature)
    
    # Almacenar transacción en memoria
    in_memory_db[token] = {
        "request": request.dict(),
        "order": order_data,
        "client": client_data,
        "status": "CREATED",
        "created_at": now,
        "signature": request.signature,
        "url_post": str(request.url_post)
    }
    
    return {
        "message": "OK",
        "code": 200,
        "current_time": current_time_str,
        "order": order_data,
        "client": client_data,
        # "extra_data": None,
        "validation_hash": validation_hash
    }

@app.get("/payment/process/{token}", response_class=HTMLResponse, summary="Página de procesamiento de pago")
async def payment_process_page(token: str, request: Request):
    if token not in in_memory_db:
        return HTMLResponse(
            content=f"""
            <html>
                <head>
                    <title>Error de Pago</title>
                    <style>
                        body {{ font-family: Arial, sans-serif; text-align: center; padding: 50px; }}
                        .error {{ color: #dc3232; font-size: 1.2em; }}
                    </style>
                </head>
                <body>
                    <h1>Token de pago no válido</h1>
                    <p class="error">El token proporcionado no existe o ha expirado</p>
                    <p>Token: {token}</p>
                </body>
            </html>
            """,
            status_code=404
        )
    
    transaction = in_memory_db[token]
    if transaction["status"] == "PAID":
        return HTMLResponse(
            content=f"""
            <html>
                <head>
                    <title>Pago Realizado</title>
                    <style>
                        body {{ font-family: Arial, sans-serif; text-align: center; padding: 50px; }}
                        .success {{ color: #28a745; font-size: 1.2em; }}
                    </style>
                </head>
                <body>
                    <h1>Pago ya realizado</h1>
                    <p class="success">Esta transacción ya ha sido procesada exitosamente</p>
                    <p>Referencia: {transaction['request']['reference']}</p>
                </body>
            </html>
            """,
            status_code=200
        )
    
    # Formulario para procesar el pago
    return HTMLResponse(
        content=f"""
        <html>
            <head>
                <title>Confirmar Pago</title>
                <style>
                    body {{
                        font-family: Arial, sans-serif;
                        max-width: 600px;
                        margin: 0 auto;
                        padding: 20px;
                    }}
                    .container {{
                        border: 1px solid #ddd;
                        border-radius: 8px;
                        padding: 30px;
                        box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                    }}
                    h1 {{
                        color: #0d6efd;
                        text-align: center;
                    }}
                    .amount {{
                        font-size: 2em;
                        font-weight: bold;
                        color: #28a745;
                        text-align: center;
                        margin: 20px 0;
                    }}
                    .details {{
                        background-color: #f8f9fa;
                        padding: 15px;
                        border-radius: 5px;
                        margin: 20px 0;
                    }}
                    .btn {{
                        display: block;
                        width: 100%;
                        padding: 12px;
                        background-color: #0d6efd;
                        color: white;
                        border: none;
                        border-radius: 5px;
                        font-size: 1.1em;
                        cursor: pointer;
                        margin: 20px 0;
                        transition: background-color 0.3s;
                    }}
                    .btn:hover {{
                        background-color: #0b5ed7;
                    }}
                    .reference {{
                        text-align: center;
                        color: #6c757d;
                        margin-top: 20px;
                    }}
                </style>
            </head>
            <body>
                <div class="container">
                    <h1>Confirmar Pago</h1>
                    <div class="amount">
                        {transaction['request']['amount']:.2f} {transaction['request']['currency']}
                    </div>
                    <div class="details">
                        <p><strong>Descripción:</strong> {transaction['request']['description']}</p>
                        <p><strong>Referencia:</strong> {transaction['request']['reference']}</p>
                    </div>
                    <form action="/internal/process-payment/{token}" method="post">
                        <input type="hidden" name="token" value="{token}">
                        <button type="submit" class="btn">PAGAR AHORA</button>
                    </form>
                    <p class="reference">
                        Transacción: {transaction['order']['uuid']}
                    </p>
                </div>
            </body>
        </html>
        """,
        status_code=200
    )

@app.post("/internal/process-payment/{token}", response_class=HTMLResponse)
async def process_payment(token: str, request: Request):
    if token not in in_memory_db:
        return HTMLResponse(
            content=f"""
            <html>
                <head>
                    <title>Error de Pago</title>
                    <style>
                        body {{ font-family: Arial, sans-serif; text-align: center; padding: 50px; }}
                        .error {{ color: #dc3232; font-size: 1.2em; }}
                    </style>
                </head>
                <body>
                    <h1>Token de pago no válido</h1>
                    <p class="error">El token proporcionado no existe o ha expirado</p>
                    <p>Token: {token}</p>
                </body>
            </html>
            """,
            status_code=404
        )
    
    transaction = in_memory_db[token]
    
    # Establecer zona horaria UTC+2
    tz = timezone(timedelta(hours=2))
    now = datetime.now(tz)
    
    # Actualizar order a estado exitoso
    successful_order = generate_successful_order(
        transaction["order"], 
        PaylandsRequest(**transaction["request"]), 
        now,
        tz
    )
    
    # Datos para el webhook
    webhook_data = {
        "message": "OK",
        "code": 200,
        "current_time": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "order": successful_order,
        "client": transaction["client"],
        # "extra_data": {
        #     "halcash": {
        #         "sender_name": "sender",
        #         "secret_key": "1234",
        #         "expiry_date": (now + timedelta(days=1)).strftime("%Y-%m-%d")
        #     }
        # },
        "validation_hash": ""  # Lo calcularemos a continuación
    }
    
    # Calcular validation_hash para el webhook
    webhook_data["validation_hash"] = calculate_validation_hash(
        webhook_data["order"],
        webhook_data["client"],
        # webhook_data["extra_data"],
        transaction["signature"]
    )
    
    # Actualizar estado en la base de datos
    transaction["status"] = "PAID"
    transaction["order"] = successful_order
    
    # Enviar webhook
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                transaction["url_post"],
                json=webhook_data,
                timeout=10.0
            )
            webhook_status = response.status_code
    except Exception as e:
        webhook_status = f"Error: {str(e)}"
    
    # Redirigir a url_ok
    return Response(
        status_code=303,
        headers={"Location": str(transaction["request"]["url_ok"])}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)