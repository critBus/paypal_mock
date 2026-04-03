from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import Optional
import logging
from datetime import datetime
from app import app
from .config import BMS_CONFIG
from .models import (
    TokenThreeDSRequest,
    TokenThreeDSResponse,
    TransactionSaleRequest,
    TransactionSaleResponse
)
from .services import bms_mock_service

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# app = FastAPI(
#     title="BMSPay Mock API",
#     description="API Mock para testing de integración con BMSPay",
#     version="1.0.0",
#     docs_url="/docs",
#     redoc_url="/redoc"
# )

# # Configurar CORS
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# Middleware para logging de requests
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = datetime.now()
    response = await call_next(request)
    process_time = (datetime.now() - start_time).total_seconds()
    logger.info(f"{request.method} {request.url.path} - {response.status_code} - {process_time:.3f}s")
    return response


# ============================================
# ENDPOINTS DE AUTENTICACIÓN
# ============================================

@app.post(
    "/api/Auth/TokenThreeDS",
    response_model=TokenThreeDSResponse,
    summary="Obtener token de autenticación 3DS",
    tags=["Authentication"]
)
async def get_threeds_token(request: TokenThreeDSRequest):
    """
    Autentica las credenciales del merchant y devuelve un token para 3D Secure.
    
    **Credenciales válidas para testing:**
    - cid: 260
    - mid: 76074
    - AppKey: 12345
    - UserName: nicolas
    - Password: password1
    """
    logger.info(f"TokenThreeDS request: cid={request.cid}, mid={request.mid}, user={request.UserName}")
    
    response, status_code = bms_mock_service.authenticate_threeds(request)
    
    if status_code != 200:
        raise HTTPException(status_code=status_code, detail=response.model_dump())
    
    return response


# ============================================
# ENDPOINTS DE TRANSACCIONES
# ============================================

@app.post(
    "/api/Transactions/Sale",
    response_model=TransactionSaleResponse,
    summary="Procesar venta/transacción",
    tags=["Transactions"]
)
async def process_sale(request: TransactionSaleRequest):
    """
    Procesa una transacción de venta con tarjeta de crédito/débito.
    
    **Datos de tarjeta para testing:**
    - CardNumber: 4111111111111111 (VISA)
    - CVN: 123
    - ExpDate: 1225 (MMYY)
    - NameOnCard: JESUS ALVAREZ
    - ZipCode: 12345
    """
    logger.info(f"Sale request: OrderRef={request.OrderReference}, Amount={request.Amount}")
    
    response, status_code = bms_mock_service.process_sale(request)
    
    if status_code != 200:
        raise HTTPException(status_code=status_code, detail=response.model_dump())
    
    return response


@app.get(
    "/api/Transactions/GetTransaction",
    response_model=TransactionSaleResponse,
    summary="Obtener detalles de transacción",
    tags=["Transactions"]
)
async def get_transaction(service_reference: str):
    """
    Recupera los detalles de una transacción usando el ServiceReferenceNumber.
    """
    logger.info(f"GetTransaction request: ServiceRef={service_reference}")
    
    response, status_code = bms_mock_service.get_transaction(service_reference)
    
    if status_code != 200 or response is None:
        raise HTTPException(status_code=404, detail={"Msg": ["Transaction not found"]})
    
    return response


# ============================================
# ENDPOINTS DE TESTING/CONTROL
# ============================================

@app.post(
    "/api/Transactions/Sale/failure/{failure_type}",
    response_model=TransactionSaleResponse,
    summary="Simular fallo de transacción",
    tags=["Testing"]
)
async def simulate_failure(failure_type: str, request: TransactionSaleRequest):
    """
    Simula diferentes tipos de fallo para testing.
    
    **Tipos de fallo disponibles:**
    - decline: Transacción declinada
    - invalid_card: Número de tarjeta inválido
    - expired_card: Tarjeta expirada
    - invalid_cvv: CVV inválido
    - unauthorized_merchant: Merchant no autorizado
    - duplicate_order: Número de orden duplicado
    """
    logger.info(f"Simulating failure: {failure_type}")
    
    response, status_code = bms_mock_service.simulate_failure(failure_type)
    
    raise HTTPException(status_code=status_code, detail=response.model_dump())


@app.get(
    "/api/Health",
    summary="Verificar estado del mock",
    tags=["Health"]
)
async def health_check():
    """Verifica que el mock API esté funcionando"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "config": {
            "cid": BMS_CONFIG.cid,
            "mid": BMS_CONFIG.mid,
            "is_test": BMS_CONFIG.is_test
        }
    }


@app.get(
    "/api/Transactions/count",
    summary="Contar transacciones procesadas",
    tags=["Testing"]
)
async def get_transaction_count():
    """Devuelve el número de transacciones procesadas"""
    return {
        "total_transactions": len(bms_mock_service.transactions),
        "active_tokens": len(bms_mock_service.tokens)
    }


# ============================================
# ENDPOINT ROOT
# ============================================

@app.get(
    "/bms/info",
    summary="Información del API Mock",
    tags=["General"]
)
async def root():
    """Información general del API Mock"""
    return {
        "name": "BMSPay Mock API",
        "version": "1.0.0",
        "documentation": "/docs",
        "health": "/api/Health",
        "endpoints": {
            "authentication": "/api/Auth/TokenThreeDS",
            "transactions": "/api/Transactions/Sale",
            "get_transaction": "/api/Transactions/GetTransaction"
        }
    }


# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(
#         "main:app",
#         host="0.0.0.0",
#         port=8000,
#         reload=True,
#         log_level="info"
#     )