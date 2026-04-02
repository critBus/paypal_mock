from fastapi import FastAPI, Request, HTTPException, Header
from pydantic import BaseModel, Field
from typing import Optional
from typing import List, Dict, Any
import os
from dotenv import load_dotenv
import uuid

import json
import re
from datetime import datetime
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path
from fastapi.templating import Jinja2Templates
import httpx
from typing import List, Literal, Optional
from app import app

from fastapi import FastAPI, Request, HTTPException, Header, Form
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
import os
from dotenv import load_dotenv
import uuid
import json
import re
from datetime import datetime, timedelta
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path
from fastapi.templating import Jinja2Templates
import httpx



# ==================================================
# CONFIGURACIÓN DE CUENTAS VÁLIDAS
# ==================================================
# Agrega aquí las cuentas que quieres que funcionen
VALID_CREDENTIALS: Dict[str, Dict[str, Any]] = {
    "client_001": {
        "client_secret": "secret_001",
        "app_name": "App Test 1",
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
            "ALLOW_GET_POS_MOVEMENT_BY_CREDENTIALS"
        ]
    },
    "client_002": {
        "client_secret": "secret_002",
        "app_name": "App Test 2",
        "scopes": [
            "ALLOW_PAYMENT_IN",
            "ALLOW_PAYMENT_OUT",
            "ALLOW_GET_BALANCE"
        ]
    },
    "client_demo": {
        "client_secret": "demo_secret",
        "app_name": "Demo App",
        "scopes": ["ALLOW_PAYMENT_IN"]
    }
}

# ==================================================
# ESTADO GLOBAL PARA TESTING (Errores de Un Solo Uso)
# ==================================================
class TestState:
    def __init__(self):
        self.force_error = False
        self.error_type = "invalid_client"
        self.error_code = 401
        self.active_tokens: Dict[str, Dict[str, Any]] = {}
        
        # ✅ NUEVO: Contador de errores por endpoint (solo 1 uso)
        self.error_usage_count: Dict[str, int] = {
            "paymentcards": 0,
            "token": 0
        }
        self.max_error_uses = 1  # Solo 1 vez por endpoint
    
    def reset(self):
        self.force_error = False
        self.error_type = "invalid_client"
        self.error_code = 401
        self.error_usage_count = {
            "paymentcards": 0,
            "token": 0
        }
    
    def can_use_error(self, endpoint: str) -> bool:
        """Verifica si aún se puede usar el error forzado para este endpoint"""
        return (
            self.force_error and 
            self.error_usage_count.get(endpoint, 0) < self.max_error_uses
        )
    
    def consume_error(self, endpoint: str):
        """Marca el error como usado para este endpoint"""
        self.error_usage_count[endpoint] = self.error_usage_count.get(endpoint, 0) + 1
    
    def get_error_usage(self, endpoint: str) -> int:
        """Obtiene cuántas veces se ha usado el error en este endpoint"""
        return self.error_usage_count.get(endpoint, 0)

TEST_STATE = TestState()

# ==================================================
# MODELOS PYDANTIC
# ==================================================
class TokenRequest(BaseModel):
    grant_type: str
    client_id: str
    client_secret: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str

class ErrorResponse(BaseModel):
    error: Dict[str, Any]

class ForceErrorRequest(BaseModel):
    force_error: bool
    error_type: Optional[str] = "invalid_client"
    error_code: Optional[int] = 401
    max_uses: Optional[int] = 1  # Nuevo parámetro

# ==================================================
# ENDPOINT DE LOGIN / TOKEN
# ==================================================
@app.post("/api/v3/access/token")
async def get_access_token(request: TokenRequest):
    """
    Simula la obtención de token de acceso de Tropipay
    POST /api/v3/access/token
    Body: grant_type, client_id, client_secret
    """
    # ✅ Verificar error forzado (también de un solo uso)
    if TEST_STATE.can_use_error("token"):
        TEST_STATE.consume_error("token")
        return create_error_response(
            error_type=TEST_STATE.error_type,
            error_code=TEST_STATE.error_code
        )
    
    # Validar grant_type
    if request.grant_type != "client_credentials":
        return create_error_response(
            error_type="invalid_request",
            error_code=400,
            message="grant_type debe ser 'client_credentials'"
        )
    
    # Validar credenciales
    if request.client_id not in VALID_CREDENTIALS:
        return create_error_response(
            error_type="invalid_client",
            error_code=401,
            message="Credential not found"
        )
    
    client_data = VALID_CREDENTIALS[request.client_id]
    
    if client_data["client_secret"] != request.client_secret:
        return create_error_response(
            error_type="invalid_client",
            error_code=401,
            message="Invalid client secret"
        )
    
    # Generar tokens
    access_token = f"mock_access_{uuid.uuid4().hex[:32]}"
    refresh_token = f"mock_refresh_{uuid.uuid4().hex[:32]}"
    
    # Guardar token activo
    TEST_STATE.active_tokens[access_token] = {
        "client_id": request.client_id,
        "app_name": client_data["app_name"],
        "scopes": client_data["scopes"],
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(seconds=1772217884)
    }
    
    # Retornar respuesta exitosa
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": 1772217884,
        "scope": " ".join(client_data["scopes"])
    }

def create_error_response(error_type: str, error_code: int, message: str = None):
    """Crea una respuesta de error estándar"""
    messages = {
        "invalid_request": "Missing or invalid parameters.",
        "invalid_client": "Invalid client credentials.",
        "forbidden": "Unauthorized access."
    }
    
    return JSONResponse(
        status_code=error_code,
        content={
            "error": {
                "type": "VALIDATION_ERROR",
                "code": error_type.upper(),
                "message": message or messages.get(error_type, "Error desconocido"),
                "details": [],
                "i18n": "Parámetros inválidos"
            }
        }
    )

# ==================================================
# ENDPOINT DE ADMINISTRACIÓN (Forzar errores)
# ==================================================
TEMPLATES = Jinja2Templates(directory="templates")

@app.get("/admin/test-control", response_class=HTMLResponse)
async def test_control_panel(request: Request):
    """Panel de control para testing"""
    return TEMPLATES.TemplateResponse("test_control.html", {
        "request": request,
        "force_error": TEST_STATE.force_error,
        "error_type": TEST_STATE.error_type,
        "error_code": TEST_STATE.error_code,
        "valid_credentials": VALID_CREDENTIALS,
        "active_tokens_count": len(TEST_STATE.active_tokens),
        "error_usage_paymentcards": TEST_STATE.get_error_usage("paymentcards"),
        "error_usage_token": TEST_STATE.get_error_usage("token"),
        "max_uses": TEST_STATE.max_error_uses
    })

@app.post("/admin/test-control")
async def update_test_control(request: Request):
    """Actualizar configuración de testing"""
    data = await request.json()
    TEST_STATE.force_error = data.get("force_error", False)
    TEST_STATE.error_type = data.get("error_type", "invalid_client")
    TEST_STATE.error_code = data.get("error_code", 401)
    TEST_STATE.max_error_uses = data.get("max_uses", 1)
    
    # Resetear contadores si se activa el error
    if TEST_STATE.force_error:
        TEST_STATE.error_usage_count = {
            "paymentcards": 0,
            "token": 0
        }
    
    return JSONResponse({
        "status": "ok",
        "force_error": TEST_STATE.force_error,
        "error_type": TEST_STATE.error_type,
        "error_code": TEST_STATE.error_code,
        "max_uses": TEST_STATE.max_error_uses,
        "message": "Error forzado activado - Se usará SOLO UNA VEZ por endpoint"
    })

@app.get("/admin/test-control/status")
async def get_test_status(request: Request):
    """Obtener estado actual del testing"""
    return JSONResponse({
        "force_error": TEST_STATE.force_error,
        "error_type": TEST_STATE.error_type,
        "error_code": TEST_STATE.error_code,
        "active_tokens": len(TEST_STATE.active_tokens),
        "valid_accounts": len(VALID_CREDENTIALS),
        "error_usage": TEST_STATE.error_usage_count,
        "max_uses": TEST_STATE.max_error_uses
    })

@app.delete("/admin/test-control/tokens")
async def clear_active_tokens(request: Request):
    """Limpiar todos los tokens activos"""
    TEST_STATE.active_tokens.clear()
    return JSONResponse({"status": "ok", "message": "Tokens limpiados"})

@app.post("/admin/test-control/reset")
async def reset_test_state(request: Request):
    """Resetear todo el estado de testing"""
    TEST_STATE.reset()
    return JSONResponse({"status": "ok", "message": "Estado reseteado"})


# ==================================================
# ENDPOINTS EXISTENTES (Payment Links, Webhooks, etc.)
# ==================================================
@app.post("/api/v3/paymentcards")
async def create_payment_link(request: Request, authorization: Optional[str] = Header(None)):
    """
    Simula creación de payment link (requiere token válido)
    ✅ El error forzado solo ocurre UNA VEZ
    """
    # Validar token si se requiere
    if authorization:
        token = authorization.replace("Bearer ", "")
        if token not in TEST_STATE.active_tokens:
            return JSONResponse(
                status_code=401,
                content={"error": {"type": "UNAUTHORIZED", "message": "Token inválido o expirado"}}
            )
    
    # ✅ VERIFICAR ERROR FORZADO (SOLO UNA VEZ)
    if TEST_STATE.can_use_error("paymentcards"):
        TEST_STATE.consume_error("paymentcards")
        return create_error_response(
            error_type=TEST_STATE.error_type,
            error_code=TEST_STATE.error_code
        )
    
    # Si ya se usó el error, continuar normal (aunque force_error siga True)
    return {
        "id": str(uuid.uuid4()),
        "lang": "es",
        "state": 1,
        "amount": 33800,
        "origin": 2,
        "userId": str(uuid.uuid4()),
        "concept": "Solicitud de pago para el artículo: MERCFSB-M677-HHRP.",
        "currency": "USD",
        "favorite": False,
        "force3ds": False,
        "giftcard": None,
        "reasonId": 3,
        "shortUrl": f"https://tppay.me/{uuid.uuid4().hex[:8]}",
        "accountId": 1024,
        "createdAt": datetime.utcnow().isoformat() + "Z",
        "hasClient": True,
        "reference": f"S{str(uuid.uuid4())[:8].upper()}",
        "saveToken": False,
        "singleUse": True,
        "updatedAt": datetime.utcnow().isoformat() + "Z",
        "urlFailed": "https://example.com/payment-failed",
        "payment3DS": 1,
        "paymentUrl": f"https://tppay.me/{uuid.uuid4().hex[:8]}",
        "urlSuccess": "https://example.com/payment-success",
        "description": "Solicitud de pago para el artículo: MERCFSB-M677-HHRP.",
        "serviceDate": datetime.utcnow().strftime("%Y-%m-%dT00:00:00.000Z"),
        "credentialId": 140470,
        "bankOrderCode": str(uuid.uuid4().int)[:20],
        "expirationDate": None,
        "expirationDays": 0,
        "paymentcardType": 4,
        "urlNotification": "https://yoursite.com/api/tpp/webhooks/notifications/",
        "strictAddressCheck": False,
        "destinationCurrency": "EUR",
        "strictPostalCodeCheck": False,
        "qrImage": "data:image/png;base64,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"
    }
