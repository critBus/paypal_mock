from fastapi import FastAPI, Request, HTTPException, Header
from pydantic import BaseModel, Field
from typing import Optional
from typing import List, Dict, Any
import os
from dotenv import load_dotenv
import uuid
from fastapi import Path as FastAPIPath
import json
import re
from fastapi import Header
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path
from fastapi.templating import Jinja2Templates
import httpx
from typing import List, Literal, Optional

from app import app



class Amount(BaseModel):
    currency_code: str = Field(..., min_length=3, max_length=3, example="USD")
    value: str = Field(..., pattern=r"^\d+\.\d{2}$", example="4.00")

class PurchaseUnit(BaseModel):
    reference_id: str = Field(..., min_length=1, max_length=127, example="FKS00000009")
    amount: Amount

class ExperienceContext(BaseModel):
    payment_method_preference: Literal["IMMEDIATE_PAYMENT_REQUIRED"] = "IMMEDIATE_PAYMENT_REQUIRED"
    landing_page: Literal["GUEST_CHECKOUT"] = "GUEST_CHECKOUT"
    # shipping_preference: Literal["GET_FROM_FILE"] = "GET_FROM_FILE"
    # user_action: Literal["PAY_NOW"] = "PAY_NOW"
    return_url: str
    cancel_url: str

class PayPalSource(BaseModel):
    experience_context: ExperienceContext

class PaymentSource(BaseModel):
    paypal: PayPalSource

class CaptureRequest(BaseModel):
    intent: Literal["CAPTURE"] = "CAPTURE"
    payment_source: PaymentSource
    purchase_units: List[PurchaseUnit] = Field(..., min_items=1, max_items=10)

# Modelos Pydantic para validación
# class AmountModel(BaseModel):
#     currency_code: str
#     value: float

# class PurchaseUnits(BaseModel):
#     invoice_id: str
#     amount: AmountModel
    

# class CreatePaymentPaypalLinkRequest(BaseModel):
#     purchase_units: PurchaseUnits
DATA_PAYPAL={
    "amount":0,
    "currency":"USD",
    "orders_id":"xxxxx",
    "intentos_get_order":0,
    "fallar_primer_intento_get_order":False,
    "intentos_capture_order":0,
    "fallar_primer_intento_capture_order":False,
    "fallar_captura":False,
    "fallar_estado_get_order":False,# primer intento
    "intentos_fallar_estado_get_order":0
}

def build_mock_response_checkout(purchase_units: PurchaseUnit):
    print("intenta hacer el mock")
    reference_id = purchase_units.reference_id
    amount = purchase_units.amount.value
    currency = purchase_units.amount.currency_code
    orders_id=str(uuid.uuid4())
    pay_id=str(uuid.uuid4())
    wh_id=str(uuid.uuid4())
    DATA_PAYPAL.update(
    {
    "amount":amount,
    "currency":currency,
    "orders_id":orders_id,
    "intentos_get_order":0,
    "intentos_capture_order":0,
    "intentos_fallar_estado_get_order":0
    }
        )
    return {
"id": orders_id,
"status": "PAYER_ACTION_REQUIRED",
"payment_source": {
"paypal": { }
},
"links": [
{
"href": f"https://api-m.paypal.com/v2/checkout/orders/{orders_id}",
"rel": "self",
"method": "GET"
},
{
"href": f"https://www.paypal.com/checkoutnow?token={orders_id}",
"rel": "payer-action",
"method": "GET"
}
]
}

# @app.post("/v2/checkout/orders2")
# async def create_payment_link_paypal(
#     request: CreatePaymentPaypalLinkRequest,
# ):
    

#     # Si todo está bien, devolver respuesta simulada
#     print("Fue una respuesta correcta")
#     return build_mock_response_checkout(request.purchase_units)
#     #return MOCK_RESPONSE


@app.post("/v1/oauth2/token")
async def create_payment_link_paypal(
    request: Request,
):
    

    # Si todo está bien, devolver respuesta simulada
    print("Fue una respuesta correcta")
    return {
  "scope": "https://uri.paypal.com/services/invoicing",
  "access_token": str(uuid.uuid4()),
  "token_type": "Bearer",
  "app_id": "APP-80W284485P519543T",
  "expires_in": 31668,
  "nonce": "2020-04-03T15:35:36ZaYZlGvEkV4yVSz8g6bAKFoGSEzuy3CQcz3ljhibXXXX"
}





@app.post("/v2/checkout/orders")
async def capture_payment(request: CaptureRequest):
    """
    Endpoint para procesar pagos PayPal con modo CAPTURE
    - Valida estructura completa del payload
    - Verifica URLs válidas
    - Valida formato de moneda y valor
    - Verifica restricciones de negocio específicas
    """
    try:
        # Aquí iría la lógica de integración con PayPal
        # Ejemplo de respuesta simulada:
        return build_mock_response_checkout(request.purchase_units[0])
        # return {
        #     "status": "APPROVED",
        #     "payment_id": "PAY-1234567890",
        #     "links": [
        #         {
        #             "href": str(request.payment_source.paypal.experience_context.return_url),
        #             "rel": "return_url",
        #             "method": "GET"
        #         }
        #     ]
        # }
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Error procesando pago con PayPal: {str(e)}"
        )



@app.post("/v2/checkout/orders/{order_id}/capture")
async def capture_order(
    order_id: str = FastAPIPath(..., description="ID de la orden a capturar"),
    authorization: str = Header(..., description="Bearer token for authentication"),
    paypal_request_id: str = Header(..., alias="PayPal-Request-Id", description="Unique request ID for idempotency")
):
    """
    Endpoint para simular la captura de un pago en PayPal
    - Recibe el ID de la orden en la URL
    - No requiere cuerpo en la petición
    - Retorna un estado 201 con los detalles de la captura completada
    """
    print("Entro a captruar orden!!!!!!!!!")
    # Validar formato del Bearer token
    if not authorization.startswith("Bearer ") or len(authorization.split("Bearer ")[1].strip()) == 0:
        print("Problema con el bereader token")
        raise HTTPException(
            status_code=401,
            detail="Invalid authorization header. Expected format: 'Bearer <token>'"
        )
    
    # Validar que el PayPal-Request-Id no esté vacío
    if not paypal_request_id \
        or len(paypal_request_id.strip()) == 0 \
         or len(paypal_request_id.strip()) >107 :
        print("Problema con el paypal_request_id")
        raise HTTPException(
            status_code=400,
            detail="Missing or empty PayPal-Request-Id header required for idempotency"
        )
    
    if DATA_PAYPAL["fallar_captura"]:
        return JSONResponse(status_code=422, 
                            content={
  "name": "UNPROCESSABLE_ENTITY",
  "links": [
    {
      "rel": "information_link",
      "href": "https://developer.paypal.com/api/rest/reference/orders/v2/errors/#TRANSACTION_REFUSED",
      "method": "GET"
    }
  ],
  "details": [
    {
      "issue": "TRANSACTION_REFUSED",
      "description": "The request was refused"
    }
  ],
  "message": "The requested action could not be performed, semantically incorrect, or failed business validation.",
  "debug_id": "36140384ed9ae"
})

    if DATA_PAYPAL["fallar_primer_intento_capture_order"] and DATA_PAYPAL["intentos_capture_order"]==0:
        DATA_PAYPAL["intentos_capture_order"]=1
        raise HTTPException(
            status_code=502,
            detail=f"Error procesando pago con PayPal:"
        )

    from datetime import datetime, timedelta
    current_time = datetime.utcnow()
    
    # Obtener los datos del pago anterior si existen
    amount = DATA_PAYPAL.get("amount", "1.00")
    currency = DATA_PAYPAL.get("currency", "USD")
    
    capture_id = f"{str(uuid.uuid4())[:16].upper()}"
    
    response = {
        "id": order_id,
        "status": "COMPLETED",
        "payment_source": {
            "paypal": {
                "email_address": "example@gmail.com",
                "account_id": "XXXXXXXXXXXX",
                "account_status": "VERIFIED",
                "name": {
                    "given_name": "Pedro",
                    "surname": "Julio Díaz",
                    "middle_name": "Alberto"
                },
                "address": {
                    "country_code": "UY"
                }
            }
        },
        "purchase_units": [
            {
                "reference_id": "P00000002",
                "shipping": {
                    "name": {
                        "full_name": "Pedro Julio Dias"
                    },
                    "address": {
                        "address_line_1": "Av. Libertador en algun lado",
                        "address_line_2": "####, Apto ###",
                        "admin_area_2": "Montevideo",
                        "admin_area_1": "Departamento de Montevideo",
                        "postal_code": "333333",
                        "country_code": "UY"
                    }
                },
                "payments": {
                    "captures": [
                        {
                            "id": capture_id,
                            "status": "COMPLETED",
                            "amount": {
                                "currency_code": currency,
                                "value": amount
                            },
                            "final_capture": True,
                            "seller_protection": {
                                "status": "ELIGIBLE",
                                "dispute_categories": [
                                    "ITEM_NOT_RECEIVED",
                                    "UNAUTHORIZED_TRANSACTION"
                                ]
                            },
                            "seller_receivable_breakdown": {
                                "gross_amount": {
                                    "currency_code": currency,
                                    "value": amount
                                },
                                "paypal_fee": {
                                    "currency_code": currency,
                                    "value": str(round(float(amount) * 0.035, 2))
                                },
                                "net_amount": {
                                    "currency_code": currency,
                                    "value": str(round(float(amount) * 0.965, 2))
                                },
                                "receivable_amount": {
                                    "currency_code": "EUR",
                                    "value": str(round(float(amount) * 0.83, 2))
                                },
                                "exchange_rate": {
                                    "source_currency": currency,
                                    "target_currency": "EUR",
                                    "value": "0.828857004096"
                                }
                            },
                            "links": [
                                {
                                    "href": f"https://api.paypal.com/v2/payments/captures/{capture_id}",
                                    "rel": "self",
                                    "method": "GET"
                                },
                                {
                                    "href": f"https://api.paypal.com/v2/payments/captures/{capture_id}/refund",
                                    "rel": "refund",
                                    "method": "POST"
                                },
                                {
                                    "href": f"https://api.paypal.com/v2/checkout/orders/{order_id}",
                                    "rel": "up",
                                    "method": "GET"
                                }
                            ],
                            "create_time": current_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "update_time": current_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                        }
                    ]
                }
            }
        ],
        "payer": {
            "name": {
                "given_name": "Julio",
                "surname": "Pedro Díaz",
                "middle_name": "miguel"
            },
            "email_address": "example@example.com",
            "payer_id": "##########",
            "address": {
                "country_code": "UY"
            }
        },
        "links": [
            {
                "href": f"https://api.paypal.com/v2/checkout/orders/{order_id}",
                "rel": "self",
                "method": "GET"
            }
        ]
    }
    
    return JSONResponse(status_code=201, content=response)

@app.get("/v2/checkout/orders/{order_id}")
async def get_order_details(order_id: str):
    """
    Endpoint para simular la obtención de detalles de una orden de PayPal
    """
    from datetime import datetime
    # Verificar si el order_id coincide con el almacenado en DATA_PAYPAL
    if order_id != DATA_PAYPAL["orders_id"]:
        raise HTTPException(
            status_code=404,
            detail="Order not found"
        )
    if DATA_PAYPAL["fallar_primer_intento_get_order"] and DATA_PAYPAL["intentos_get_order"]==0:
        print("get ordenr va a dar 404")
        DATA_PAYPAL["intentos_get_order"]=1
        raise HTTPException(
            status_code=404,
            detail="Order not found"
        )
    
    # Formatear la fecha actual en formato ISO 8601
    current_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    status="APPROVED"
    print(f'DATA_PAYPAL["fallar_estado_get_order"] {DATA_PAYPAL["fallar_estado_get_order"]}')
    print(f'DATA_PAYPAL["intentos_fallar_estado_get_order"] {DATA_PAYPAL["intentos_fallar_estado_get_order"]}')
    if DATA_PAYPAL["fallar_estado_get_order"] and DATA_PAYPAL["intentos_fallar_estado_get_order"]==0:
        DATA_PAYPAL["intentos_fallar_estado_get_order"]=1
        print("Puso otro estado")
        status="OTRO"
    print("geto orden todo ok")
    # Construir la respuesta simulada
    return {
        "id": order_id,
        "status": status,
        "intent": "CAPTURE",
        "payment_source": {
            "paypal": {
                "name": {
                    "given_name": "John",
                    "surname": "Doe"
                },
                "email_address": "customer@example.com",
                "account_id": "QYR5Z8XDVJNXQ"
            }
        },
        "purchase_units": [
            {
                "reference_id": "d9f80740-38f0-11e8-b467-0ed5f89f718b",
                "amount": {
                    "currency_code": DATA_PAYPAL["currency"],
                    "value": DATA_PAYPAL["amount"]
                }
            }
        ],
        "payer": {
            "name": {
                "given_name": "John",
                "surname": "Doe"
            },
            "email_address": "customer@example.com",
            "payer_id": "QYR5Z8XDVJNXQ"
        },
        "create_time": current_time,
        "links": [
            {
                "href": f"https://api-m.paypal.com/v2/checkout/orders/{order_id}",
                "rel": "self",
                "method": "GET"
            },
            {
                "href": f"https://www.paypal.com/checkoutnow?token={order_id}",
                "rel": "approve",
                "method": "GET"
            },
            {
                "href": f"https://api-m.paypal.com/v2/checkout/orders/{order_id}",
                "rel": "update",
                "method": "PATCH"
            },
            {
                "href": f"https://api-m.paypal.com/v2/checkout/orders/{order_id}/capture",
                "rel": "capture",
                "method": "POST"
            }
        ]
    }