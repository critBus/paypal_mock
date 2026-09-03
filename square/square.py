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

import base64
import hashlib
import hmac

from app import app

# from paypal.paypal import *

# # Cargar variables de entorno (opcional si usas .env)
load_dotenv()

# Configuración desde variables de entorno
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "adqwe")
SQUARE_VERSION = os.getenv("SQUARE_VERSION", "2025-09-24")#"2024-06-12")  # Ejemplo de versión válida
LOCATION_ID = os.getenv("LOCATION_ID", "location-id-2")

SQUARE_WEBHOOK_SIGNATURE_KEY = os.getenv("SQUARE_WEBHOOK_SIGNATURE_KEY", "signature-key-2")
SQUARE_WEBHOOK_SUBSCRIPTION_ID = os.getenv("SQUARE_WEBHOOK_SUBSCRIPTION_ID", "subcription-id-2")
SQUARE_WEBHOOK_NOTIFICATION_URL = os.getenv(
    "SQUARE_WEBHOOK_NOTIFICATION_URL",
    "http://localhost:8001/api/square/webhooks/notifications/",
)
SQUARE_WEBHOOK_TARGET_URL = os.getenv(
    "WEBHOOK_SQUARE__TARGET_URL",
    SQUARE_WEBHOOK_NOTIFICATION_URL,
)
SQUARE_MERCHANT_ID = os.getenv("SQUARE_MERCHANT_ID", "mock-square-merchant")
SQUARE_ENVIRONMENT = os.getenv("SQUARE_ENVIRONMENT", "Sandbox")


def serialize_square_webhook(payload: dict[str, Any]) -> bytes:
    """Serializa una sola vez el cuerpo que se firma y se envía."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def generate_square_webhook_signature(
    *,
    raw_body: bytes,
    signature_key: str,
    notification_url: str,
) -> str:
    """Genera X-Square-HmacSha256-Signature igual que Square."""
    message = notification_url.encode("utf-8") + raw_body
    digest = hmac.new(
        key=signature_key.encode("utf-8"),
        msg=message,
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


# Modelos Pydantic para validación
class PriceMoney(BaseModel):
    amount: int
    currency: str

class QuickPay(BaseModel):
    name: str
    price_money: PriceMoney
    location_id: str

class CreatePaymentLinkRequest(BaseModel):
    idempotency_key: str
    quick_pay: QuickPay

def build_mock_response(quick_pay: QuickPay):
    name = quick_pay.name
    amount = quick_pay.price_money.amount
    currency = quick_pay.price_money.currency
    # location_id = LOCATION_ID
    location_id = quick_pay.location_id
    orders_id=str(uuid.uuid4())
    return {
        "payment_link": {
            "id": str(uuid.uuid4())[:15],
            "version": 1,
            "order_id": orders_id,
            "url": f"https://square.link/u/{orders_id}",
            "long_url": f"https://checkout.square.site/{orders_id}",
            "created_at": "2022-04-25T23:58:01Z"
        },
        "related_resources": {
            "orders": [
                {
                    "id": orders_id,
                    "location_id": location_id,
                    "source": {
                        "name": "Test Online Checkout Application"
                    },
                    "line_items": [
                        {
                            "uid": str(uuid.uuid4()),
                            "name": name,
                            "quantity": "1",
                            "item_type": "ITEM",
                            "base_price_money": {
                                "amount": amount,
                                "currency": currency
                            },
                            "variation_total_price_money": {
                                "amount": amount,
                                "currency": currency
                            },
                            "gross_sales_money": {
                                "amount": amount,
                                "currency": currency
                            },
                            "total_tax_money": {
                                "amount": 0,
                                "currency": currency
                            },
                            "total_discount_money": {
                                "amount": 0,
                                "currency": currency
                            },
                            "total_money": {
                                "amount": amount,
                                "currency": currency
                            }
                        }
                    ],
                    "fulfillments": [
                        {
                            "uid": "bBpNrxjdQxGQP16sTmdzi",
                            "type": "DIGITAL",
                            "state": "PROPOSED"
                        }
                    ],
                    "net_amounts": {
                        "total_money": {
                            "amount": amount,
                            "currency": currency
                        },
                        "tax_money": {
                            "amount": 0,
                            "currency": currency
                        },
                        "discount_money": {
                            "amount": 0,
                            "currency": currency
                        },
                        "tip_money": {
                            "amount": 0,
                            "currency": currency
                        },
                        "service_charge_money": {
                            "amount": 0,
                            "currency": currency
                        }
                    },
                    "created_at": "2022-03-03T00:53:15.829Z",
                    "updated_at": "2022-03-03T00:53:15.829Z",
                    "state": "DRAFT",
                    "version": 1,
                    "total_money": {
                        "amount": amount,
                        "currency": currency
                    },
                    "total_tax_money": {
                        "amount": 0,
                        "currency": currency
                    },
                    "total_discount_money": {
                        "amount": 0,
                        "currency": currency
                    },
                    "total_tip_money": {
                        "amount": 0,
                        "currency": currency
                    },
                    "total_service_charge_money": {
                        "amount": 0,
                        "currency": currency
                    }
                }
            ]
        }
    }

MOCK_RESPONSE={
  "payment_link": {
    "id": "LAKPOYULKNZP32P3",
    "url": "https://sandbox.square.link/u/Mo1OF6aw",
    "version": 1,
    "long_url": "https://connect.squareupsandbox.com/v2/online-checkout/sandbox-testing-panel/MLHQYGC5RZ43B/LAKPOYULKNZP32P3",
    "order_id": "4PgKqnTxPOvX2uyHRpCAloVR1e4F",
    "created_at": "2025-10-05T16:39:11Z"
  },
  "related_resources": {
    "orders": [
      {
        "id": "4PgKqnTxPOvX2uyHRpCAloVR1e4F",
        "state": "DRAFT",
        "source": {
          "name": "Sandbox for sq0idp-5DCjCOafXfQLTAJ_2wuVPA"
        },
        "version": 1,
        "created_at": "2025-10-05T16:39:11.416Z",
        "line_items": [
          {
            "uid": "fE8aqd6r0PHLYcyYVHvqg",
            "name": "pago a sandbox",
            "quantity": "1",
            "item_type": "ITEM",
            "total_money": {
              "amount": 52100,
              "currency": "USD"
            },
            "total_tax_money": {
              "amount": 0,
              "currency": "USD"
            },
            "base_price_money": {
              "amount": 52100,
              "currency": "USD"
            },
            "gross_sales_money": {
              "amount": 52100,
              "currency": "USD"
            },
            "total_discount_money": {
              "amount": 0,
              "currency": "USD"
            },
            "total_service_charge_money": {
              "amount": 0,
              "currency": "USD"
            },
            "variation_total_price_money": {
              "amount": 52100,
              "currency": "USD"
            }
          }
        ],
        "updated_at": "2025-10-05T16:39:11.416Z",
        "location_id": "LJ78CCDHFT8AC",
        "net_amounts": {
          "tax_money": {
            "amount": 0,
            "currency": "USD"
          },
          "tip_money": {
            "amount": 0,
            "currency": "USD"
          },
          "total_money": {
            "amount": 52100,
            "currency": "USD"
          },
          "discount_money": {
            "amount": 0,
            "currency": "USD"
          },
          "service_charge_money": {
            "amount": 0,
            "currency": "USD"
          }
        },
        "total_money": {
          "amount": 52100,
          "currency": "USD"
        },
        "fulfillments": [
          {
            "uid": "KACfmPhJPlP1llSV5yZo1B",
            "type": "DIGITAL",
            "state": "PROPOSED"
          }
        ],
        "total_tax_money": {
          "amount": 0,
          "currency": "USD"
        },
        "total_tip_money": {
          "amount": 0,
          "currency": "USD"
        },
        "net_amount_due_money": {
          "amount": 52100,
          "currency": "USD"
        },
        "total_discount_money": {
          "amount": 0,
          "currency": "USD"
        },
        "total_service_charge_money": {
          "amount": 0,
          "currency": "USD"
        }
      }
    ]
  }
}

def get_quick_pay_from_request(body: dict[str, Any]) -> QuickPay:
    """Normaliza los formatos quick_pay y order usados por Square."""
    if quick_pay_data := body.get("quick_pay"):
        price_money = quick_pay_data.get("price_money") or {}
        return QuickPay(
            name=quick_pay_data.get("name", "item"),
            price_money=PriceMoney(
                amount=price_money.get("amount", 0),
                currency=price_money.get("currency", "USD"),
            ),
            location_id=quick_pay_data.get("location_id", LOCATION_ID),
        )

    order_data = body.get("order") or {}
    line_items = order_data.get("line_items") or []
    if not line_items:
        raise HTTPException(
            status_code=422,
            detail="Se requiere quick_pay o al menos un elemento en order.line_items",
        )

    amount = 0
    currency = None
    for index, line_item in enumerate(line_items):
        price_money = line_item.get("base_price_money") or {}
        item_amount = price_money.get("amount")
        item_currency = price_money.get("currency")

        if item_amount is None or not item_currency:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Falta amount o currency en "
                    f"order.line_items[{index}].base_price_money"
                ),
            )

        if currency is not None and item_currency != currency:
            raise HTTPException(
                status_code=422,
                detail="Todos los line_items deben utilizar la misma moneda",
            )

        try:
            quantity = int(line_item.get("quantity", "1"))
            amount += int(item_amount) * quantity
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Cantidad inválida en order.line_items[{index}]",
            ) from exc

        currency = item_currency

    first_item = line_items[0]
    return QuickPay(
        name=first_item.get("name") or order_data.get("ticket_name") or "item",
        price_money=PriceMoney(amount=amount, currency=currency),
        location_id=order_data.get("location_id", LOCATION_ID),
    )

@app.post("/v2/online-checkout/payment-links")
async def create_payment_link(
    request: Request,
    authorization: Optional[str] = Header(None),
    square_version: Optional[str] = Header(None, alias="Square-Version")
):
    # Validar Square-Version
    if square_version != SQUARE_VERSION:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid Square-Version. Expected '{SQUARE_VERSION}', got '{square_version}'"
        )

    # Validar Authorization
    expected_auth = f"Bearer {ACCESS_TOKEN}"
    if authorization != expected_auth:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing Authorization header"
        )

    # if request.quick_pay.location_id != LOCATION_ID:
    #     raise HTTPException(
    #         status_code=400,
    #         detail=f"Invalid location_id. Expected '{LOCATION_ID}', got '{request.quick_pay.location_id}'"
    #     )

    # Si todo está bien, leer body y devolver respuesta simulada con URL local
    # body = await request.json()
    # # Extraer quick_pay si existe
    # quick_pay = None
    # if isinstance(body, dict) and "quick_pay" in body:
    #     qp = body["quick_pay"]
    #     quick_pay = QuickPay(
    #         name=qp.get("name", "item"),
    #         price_money=PriceMoney(amount=qp.get("price_money", {}).get("amount", 0), currency=qp.get("price_money", {}).get("currency", "USD")),
    #         location_id=qp.get("location_id", LOCATION_ID)
    #     )

    # # Construir respuesta usando build_mock_response
    # resp = build_mock_response(quick_pay if quick_pay else QuickPay(name="item", price_money=PriceMoney(amount=0, currency="USD"), location_id=LOCATION_ID))

    body = await request.json()
    quick_pay = get_quick_pay_from_request(body)
    resp = build_mock_response(quick_pay)

    order_id = resp["payment_link"]["order_id"]

    # El link devuelto debe abrir el checkout local del mock.
    base_url = str(request.base_url).rstrip("/")
    checkout_url = f"{base_url}/mock-square/checkout/{order_id}"
    resp["payment_link"]["url"] = checkout_url
    resp["payment_link"]["long_url"] = checkout_url

    # Guardar exactamente el importe normalizado desde quick_pay u order.line_items.
    # Este registro es la fuente usada posteriormente para construir el webhook.
    payment_links[order_id] = {
        "order_id": order_id,
        "payment_id": str(uuid.uuid4()),
        "payment_link_id": resp["payment_link"].get("id"),
        "amount": quick_pay.price_money.amount,
        "currency": quick_pay.price_money.currency,
        "name": quick_pay.name,
        "location_id": quick_pay.location_id,
        "payment_version": 0,
        "order_version": 1,
        "order_state": "OPEN",
        "deleted": False,
        "deleted_at": None,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }

    print(
        "Payment link creado: "
        f"{order_id} -> {checkout_url} "
        f"({quick_pay.price_money.amount} {quick_pay.price_money.currency})"
    )
    return JSONResponse(status_code=200, content=resp)


def validate_square_api_headers(
    authorization: Optional[str],
    square_version: Optional[str],
) -> None:
    """Valida los headers comunes usados por los endpoints API del mock."""
    if square_version != SQUARE_VERSION:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid Square-Version. Expected '{SQUARE_VERSION}', got '{square_version}'",
        )

    expected_auth = f"Bearer {ACCESS_TOKEN}"
    if authorization != expected_auth:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing Authorization header",
        )


def get_payment_link_by_id(payment_link_id: str) -> Optional[Dict[str, Any]]:
    """Busca el registro interno usando el ID de PaymentLink asignado por Square."""
    return next(
        (
            info
            for info in payment_links.values()
            if info.get("payment_link_id") == payment_link_id
        ),
        None,
    )


@app.get("/v2/orders/{order_id}")
async def retrieve_order(
    order_id: str,
    authorization: Optional[str] = Header(None),
    square_version: Optional[str] = Header(None, alias="Square-Version"),
):
    """Simula Orders API -> RetrieveOrder, usado antes de expirar un link."""
    validate_square_api_headers(authorization, square_version)

    info = payment_links.get(order_id)
    if not info:
        return JSONResponse(
            status_code=404,
            content={
                "errors": [
                    {
                        "category": "INVALID_REQUEST_ERROR",
                        "code": "NOT_FOUND",
                        "detail": f"Order {order_id} not found",
                    }
                ]
            },
        )

    money = {
        "amount": info.get("amount", 0),
        "currency": info.get("currency", "USD"),
    }
    return JSONResponse(
        status_code=200,
        content={
            "order": {
                "id": order_id,
                "location_id": info.get("location_id", LOCATION_ID),
                "state": info.get("order_state", "OPEN"),
                "version": int(info.get("order_version", 1)),
                "created_at": info.get("created_at"),
                "updated_at": info.get("updated_at", info.get("created_at")),
                "total_money": money,
                "net_amounts": {
                    "total_money": money,
                    "tax_money": {"amount": 0, "currency": money["currency"]},
                    "discount_money": {"amount": 0, "currency": money["currency"]},
                    "tip_money": {"amount": 0, "currency": money["currency"]},
                    "service_charge_money": {"amount": 0, "currency": money["currency"]},
                },
            }
        },
    )


@app.delete("/v2/online-checkout/payment-links/{payment_link_id}")
async def delete_payment_link(
    payment_link_id: str,
    authorization: Optional[str] = Header(None),
    square_version: Optional[str] = Header(None, alias="Square-Version"),
):
    """Simula Checkout API -> DeletePaymentLink.

    Square cancela el Order asociado y elimina/invalida el checkout link.
    """
    validate_square_api_headers(authorization, square_version)

    info = get_payment_link_by_id(payment_link_id)
    if not info or info.get("deleted"):
        return JSONResponse(
            status_code=404,
            content={
                "errors": [
                    {
                        "category": "INVALID_REQUEST_ERROR",
                        "code": "NOT_FOUND",
                        "detail": f"Payment link {payment_link_id} not found",
                    }
                ]
            },
        )

    if info.get("order_state") == "COMPLETED":
        return JSONResponse(
            status_code=409,
            content={
                "errors": [
                    {
                        "category": "INVALID_REQUEST_ERROR",
                        "code": "BAD_REQUEST",
                        "detail": "The payment link belongs to a completed order.",
                    }
                ]
            },
        )

    now = datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
    info["deleted"] = True
    info["deleted_at"] = now
    info["updated_at"] = now
    info["order_state"] = "CANCELED"
    info["order_version"] = int(info.get("order_version", 1)) + 1

    print(
        "Payment link eliminado: "
        f"{payment_link_id} -> order {info['order_id']} (CANCELED)"
    )

    return JSONResponse(
        status_code=200,
        content={
            "id": payment_link_id,
            "cancelled_order_id": info["order_id"],
        },
    )


# Carpeta donde se guardarán los registros (opcional, puedes cambiarla)
LOGS_DIR = Path("webhook_logs")
LOGS_DIR.mkdir(exist_ok=True)

@app.api_route("/api/square/webhooks/notifications", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def square_webhook_handler(request: Request):
    # Generar un nombre único para el archivo de log
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    unique_suffix = str(uuid.uuid4())[:8]
    log_filename = LOGS_DIR / f"registro_wh_{timestamp}_{unique_suffix}.txt"

    # Capturar datos
    url = str(request.url)
    method = request.method
    headers = dict(request.headers)
    
    try:
        body = await request.body()
        if body:
            try:
                json_body = json.loads(body.decode("utf-8"))
                payload_str = json.dumps(json_body, indent=2, ensure_ascii=False)
                payload_type = "JSON"
            except json.JSONDecodeError:
                payload_str = body.decode("utf-8", errors="replace")
                payload_type = "raw"
        else:
            payload_str = "(vacío)"
            payload_type = "none"
    except Exception as e:
        payload_str = f"(error al leer cuerpo: {e})"
        payload_type = "error"

    # Formato del mensaje a imprimir y guardar
    log_lines = [
        f"Fecha y hora (UTC): {datetime.utcnow().isoformat()}",
        f"URL: {url}",
        f"Método: {method}",
        f"Headers:",
        json.dumps(headers, indent=2, ensure_ascii=False),
        f"Tipo de payload: {payload_type}",
        f"Payload:",
        payload_str,
        "\n" + "="*80 + "\n"
    ]

    full_log = "\n".join(log_lines)

    # Imprimir en consola
    print(full_log)

    # Guardar en archivo
    try:
        with open(log_filename, "w", encoding="utf-8") as f:
            f.write(full_log)
    except Exception as e:
        print(f"[ERROR AL GUARDAR LOG]: {e}")

    # Siempre responder con 200 OK
    return JSONResponse(status_code=200, content={"status": "ok"})





TEMPLATES = Jinja2Templates(directory="templates")


@app.get("/mock-square/checkout/{order_id}", response_class=HTMLResponse)
async def mock_checkout_page(request: Request, order_id: str):
    info = payment_links.get(order_id)
    if not info:
        return HTMLResponse(status_code=404, content=f"<h1>Order {order_id} no encontrado</h1>")
    if info.get("deleted") or info.get("order_state") == "CANCELED":
        return HTMLResponse(
            status_code=410,
            content=(
                "<h1>Payment link no disponible</h1>"
                "<p>Este link de pago fue eliminado o expiró y ya no puede utilizarse.</p>"
            ),
        )
    if info.get("order_state") == "COMPLETED":
        return HTMLResponse(
            status_code=410,
            content=(
                "<h1>Payment link ya utilizado</h1>"
                "<p>La orden asociada a este link ya fue pagada.</p>"
            ),
        )

    return TEMPLATES.TemplateResponse("checkout.html", {
        "request": request,
        "order_id": order_id,
        "name": info.get("name"),
        "amount": info.get("amount"),
        "currency": info.get("currency")
    })


@app.post("/mock-square/checkout/{order_id}/simulate")
async def simulate_checkout(request: Request, order_id: str):
    """Simula el pago y envía un webhook de Square correctamente firmado."""
    info = payment_links.get(order_id)
    if not info:
        raise HTTPException(status_code=404, detail="order_id no encontrado")
    if info.get("deleted") or info.get("order_state") == "CANCELED":
        raise HTTPException(
            status_code=410,
            detail="El payment link fue eliminado o expiró y ya no puede pagarse.",
        )
    if info.get("order_state") == "COMPLETED":
        raise HTTPException(
            status_code=409,
            detail="La orden asociada a este payment link ya está pagada.",
        )

    if not SQUARE_WEBHOOK_SIGNATURE_KEY:
        raise HTTPException(
            status_code=500,
            detail="SQUARE_WEBHOOK_SIGNATURE_KEY no está configurada",
        )
    if not SQUARE_WEBHOOK_SUBSCRIPTION_ID:
        raise HTTPException(
            status_code=500,
            detail="SQUARE_WEBHOOK_SUBSCRIPTION_ID no está configurada",
        )

    request_data = await request.json()
    outcome = str(request_data.get("outcome") or "failure").lower()
    if outcome not in {"success", "failure"}:
        raise HTTPException(
            status_code=400,
            detail="outcome debe ser 'success' o 'failure'",
        )

    status = "COMPLETED" if outcome == "success" else "FAILED"
    event_time = datetime.utcnow().isoformat(timespec="milliseconds") + "Z"

    # Un pago completado deja el Order en estado terminal COMPLETED.
    # Para un intento fallido mantenemos el Order OPEN, por lo que todavía
    # puede expirar/eliminarse posteriormente como ocurriría con un link pendiente.
    if outcome == "success":
        info["order_state"] = "COMPLETED"
        info["order_version"] = int(info.get("order_version", 1)) + 1
        info["updated_at"] = event_time

    payment_id = info["payment_id"]
    payment_version = int(info.get("payment_version", 0)) + 1
    info["payment_version"] = payment_version

    money = {
        "amount": info.get("amount", 0),
        "currency": info.get("currency", "USD"),
    }
    payload = {
        "merchant_id": SQUARE_MERCHANT_ID,
        "type": "payment.updated",
        "event_id": str(uuid.uuid4()),
        "created_at": event_time,
        "data": {
            "type": "payment",
            "id": payment_id,
            "object": {
                "payment": {
                    "id": payment_id,
                    "created_at": info["created_at"],
                    "updated_at": event_time,
                    "order_id": order_id,
                    "status": status,
                    "amount_money": money,
                    "total_money": money,
                    "version": payment_version,
                }
            },
        },
    }

    # Es indispensable enviar exactamente estos mismos bytes. Usar json=payload
    # haría que httpx pudiera serializar el JSON de otra manera tras firmarlo.
    raw_body = serialize_square_webhook(payload)
    signature = generate_square_webhook_signature(
        raw_body=raw_body,
        signature_key=SQUARE_WEBHOOK_SIGNATURE_KEY,
        notification_url=SQUARE_WEBHOOK_NOTIFICATION_URL,
    )
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "mock-square/1.0",
        "X-Square-HmacSha256-Signature": signature,
        "Square-Subscription-Id": SQUARE_WEBHOOK_SUBSCRIPTION_ID,
        "Square-Environment": SQUARE_ENVIRONMENT,
    }

    result = {
        "sent": False,
        "target": SQUARE_WEBHOOK_TARGET_URL,
        "signed_notification_url": SQUARE_WEBHOOK_NOTIFICATION_URL,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                SQUARE_WEBHOOK_TARGET_URL,
                content=raw_body,
                headers=headers,
            )
            result.update(
                sent=True,
                status_code=response.status_code,
                response_text=response.text,
            )
        except httpx.HTTPError as exc:
            result["error"] = str(exc)

    # La llamada del checkout funcionó, pero dejamos visible si Django rechazó
    # el evento; esto facilita distinguir el mock del resultado del webhook.
    return JSONResponse(
        status_code=200,
        content={"result": result, "payload": payload},
    )

# Colores por tipo de evento
COLOR_MAP = {
    "order.created": "#e3f2fd",          # Azul claro
    "order.fulfillment.updated": "#f3e5f5",  # Morado claro
    "payment.updated": "#e8f5e9",        # Verde claro
    "default": "#ffffff",
    "order.updated": "#6ab1e4", 
}

# Almacenamiento en memoria
webhook_records: List[Dict[str, Any]] = []

# Almacenamiento en memoria de payment links creados: order_id -> detalles
payment_links: Dict[str, Dict[str, Any]] = {}

def parse_log_file(file_path: Path) -> Dict[str, Any]:
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Extraer bloques con expresiones regulares
    header_match = re.search(r"Headers:\s*({.*?})\s*Tipo de payload", content, re.DOTALL)
    payload_match = re.search(r"Payload:\s*({.*?})\s*={10,}", content, re.DOTALL)

    if not header_match or not payload_match:
        raise ValueError(f"Formato inválido en {file_path}")

    headers = json.loads(header_match.group(1))
    payload = json.loads(payload_match.group(1))

    event_type = payload.get("type")
    data = payload.get("data", {}).get("object", {})

    # Extraer campos comunes
    record = {
        "file_name": file_path.name,
        "event_type": event_type,
        "order_id": None,
        "status": None,
        "version": None,
        "created_at": payload.get("created_at"),
        "headers": headers,
        "payload": payload,
        "color": COLOR_MAP.get(event_type, COLOR_MAP["default"])
    }

    # Extraer order_id
    if "order_id" in payload.get("data", {}):
        record["order_id"] = payload["data"]["order_id"]
    elif "order_id" in data.get("order_created", {}):
        record["order_id"] = data["order_created"]["order_id"]
    elif "order_id" in data.get("order_updated", {}):
        record["order_id"] = data["order_updated"]["order_id"]
    elif "order_id" in data.get("order_fulfillment_updated", {}):
        record["order_id"] = data["order_fulfillment_updated"]["order_id"]
    elif "order_id" in data.get("payment", {}):
        record["order_id"] = data["payment"]["order_id"]

    # Extraer status y version
    if event_type == "payment.updated":
        payment = data.get("payment", {})
        record["status"] = payment.get("status")
        record["version"] = payment.get("version")
    elif event_type == "payment.created":
        payment = data.get("payment", {})
        record["status"] = payment.get("status")
        record["version"] = payment.get("version")
        print(f'payment.get("version") {payment.get("version")}')
    elif event_type == "order.created":
        order = data.get("order_created", {})
        record["status"] = order.get("state")
        record["version"] = order.get("version")
    elif event_type == "order.updated":
        order = data.get("order_updated", {})
        record["status"] = order.get("state")
        record["version"] = order.get("version")
    elif event_type == "order.fulfillment.updated":
        fulfillment = data.get("order_fulfillment_updated", {})
        record["status"] = fulfillment.get("state")
        record["version"] = fulfillment.get("version")
    else:
        # Intentar genérico
        for obj in data.values():
            if isinstance(obj, dict):
                record["status"] = obj.get("state") or obj.get("status")
                record["version"] = obj.get("version")
                break

    return record

def load_all_logs():
    global webhook_records
    webhook_records.clear()
    if not LOGS_DIR.exists():
        LOGS_DIR.mkdir()
        return

    files = [f for f in LOGS_DIR.glob("registro_wh_*.txt") if f.is_file()]
    print(f"cantidad de registros {len(files)}")
    for file in files:
        try:
            record = parse_log_file(file)
            webhook_records.append(record)
        except Exception as e:
            print(f"Error al parsear {file}: {e}")

    # Ordenar por created_at
    def parse_dt(dt_str):
        if not dt_str:
            return datetime.min
        try:
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except:
            return datetime.min

    webhook_records.sort(key=lambda r: parse_dt(r["created_at"]))

# Cargar al inicio
load_all_logs()

@app.get("/square", response_class=HTMLResponse)
async def show_dashboard(request: Request):
    # Agrupar por order_id y luego por type
    grouped = {}
    for rec in webhook_records:
        oid = rec["order_id"] or "sin_order_id"
        if oid not in grouped:
            grouped[oid] = []
        grouped[oid].append(rec)

    # Mantener el orden global por created_at
    for oid in grouped:
        grouped[oid].sort(key=lambda r: r["created_at"] or "")

    return TEMPLATES.TemplateResponse("square.html", {
        "request": request,
        "grouped_records": grouped
    })

@app.post("/send-webhook")
async def send_webhook(request: Request):
    data = await request.json()
    indices = data.get("indices", [])
    target_url = os.getenv("WEBHOOK_TARGET_URL")
    if not target_url:
        raise HTTPException(status_code=400, detail="WEBHOOK_TARGET_URL no configurada")

    results = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for idx in indices:
            if idx < 0 or idx >= len(webhook_records):
                continue
            rec = webhook_records[idx]
            try:
                response = await client.post(
                    target_url,
                    json=rec["payload"],
                    headers={k: v for k, v in rec["headers"].items() if k.lower() not in ["host", "content-length"]}
                )

                # Imprimir el código de estado
                print(f"Código de respuesta: {response.status_code}")

                # Intentar imprimir el cuerpo como JSON
                try:
                    response_json = response.json()
                    print("Cuerpo de la respuesta (JSON):")
                    import json
                    print(json.dumps(response_json, indent=2, ensure_ascii=False))
                except Exception as e:
                    print(f"No se pudo parsear el cuerpo como JSON. Error: {e}")
                    # Opcional: imprimir el cuerpo como texto plano si no es JSON
                    print("Cuerpo de la respuesta (texto):")
                    print(response.text)

                results.append({
                    "index": idx,
                    "file": rec["file_name"],
                    "status_code": response.status_code,
                    "success": response.status_code in (200, 201, 202, 204)
                })
            except Exception as e:
                results.append({
                    "index": idx,
                    "file": rec["file_name"],
                    "error": str(e),
                    "success": False
                })

    return JSONResponse({"results": results})


@app.api_route("/webhooks/events/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def square_webhook_handler(request: Request):
    # Generar un nombre único para el archivo de log
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    unique_suffix = str(uuid.uuid4())[:8]
    log_filename = LOGS_DIR / f"registro_llamada_interna_{timestamp}_{unique_suffix}.txt"

    # Capturar datos
    url = str(request.url)
    method = request.method
    headers = dict(request.headers)
    
    try:
        body = await request.body()
        if body:
            try:
                json_body = json.loads(body.decode("utf-8"))
                payload_str = json.dumps(json_body, indent=2, ensure_ascii=False)
                payload_type = "JSON"
            except json.JSONDecodeError:
                payload_str = body.decode("utf-8", errors="replace")
                payload_type = "raw"
        else:
            payload_str = "(vacío)"
            payload_type = "none"
    except Exception as e:
        payload_str = f"(error al leer cuerpo: {e})"
        payload_type = "error"

    # Formato del mensaje a imprimir y guardar
    log_lines = [
        f"Fecha y hora (UTC): {datetime.utcnow().isoformat()}",
        f"URL: {url}",
        f"Método: {method}",
        f"Headers:",
        json.dumps(headers, indent=2, ensure_ascii=False),
        f"Tipo de payload: {payload_type}",
        f"Payload:",
        payload_str,
        "\n" + "="*80 + "\n"
    ]

    full_log = "\n".join(log_lines)

    # Imprimir en consola
    print(full_log)

    # Guardar en archivo
    try:
        with open(log_filename, "w", encoding="utf-8") as f:
            f.write(full_log)
    except Exception as e:
        print(f"[ERROR AL GUARDAR LOG]: {e}")

    # Siempre responder con 200 OK
    return JSONResponse(status_code=200, content={"status": "ok"})


@app.api_route("/api/paypal/webhooks/notifications", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def square_webhook_handler(request: Request):
    # Generar un nombre único para el archivo de log
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    unique_suffix = str(uuid.uuid4())[:8]
    log_filename = LOGS_DIR / f"registro_wh_py_{timestamp}_{unique_suffix}.txt"

    # Capturar datos
    url = str(request.url)
    method = request.method
    headers = dict(request.headers)
    
    try:
        body = await request.body()
        if body:
            try:
                json_body = json.loads(body.decode("utf-8"))
                payload_str = json.dumps(json_body, indent=2, ensure_ascii=False)
                payload_type = "JSON"
            except json.JSONDecodeError:
                payload_str = body.decode("utf-8", errors="replace")
                payload_type = "raw"
        else:
            payload_str = "(vacío)"
            payload_type = "none"
    except Exception as e:
        payload_str = f"(error al leer cuerpo: {e})"
        payload_type = "error"

    # Formato del mensaje a imprimir y guardar
    log_lines = [
        f"Fecha y hora (UTC): {datetime.utcnow().isoformat()}",
        f"URL: {url}",
        f"Método: {method}",
        f"Headers:",
        json.dumps(headers, indent=2, ensure_ascii=False),
        f"Tipo de payload: {payload_type}",
        f"Payload:",
        payload_str,
        "\n" + "="*80 + "\n"
    ]

    full_log = "\n".join(log_lines)

    # Imprimir en consola
    print(full_log)

    # Guardar en archivo
    try:
        with open(log_filename, "w", encoding="utf-8") as f:
            f.write(full_log)
    except Exception as e:
        print(f"[ERROR AL GUARDAR LOG]: {e}")

    # Siempre responder con 200 OK
    return JSONResponse(status_code=200, content={"status": "ok"})
