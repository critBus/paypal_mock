# simulador_paypal.py
import uuid

import base64
import json
import requests
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
import zlib
from cryptography.hazmat.primitives.asymmetric import padding

from app import app

from bms.main import *
from redsys.main import *
from paypal.paypal import *
from tropipay.tropipay import *
from captura_general.captura_general import *

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from config import settings
from fastapi.middleware.cors import CORSMiddleware




def generate_event_body(event_type, orders_id, amount, currency):
    """
    Genera el cuerpo de un evento de webhook basado en el tipo de evento
    """
    import datetime
    base_id = str(uuid.uuid4())[:20]
    print(f"event_type {event_type}")

    # Configuración común para todos los eventos
    event_base = {
        "id": f"WH-{base_id}",
        "create_time": datetime.datetime.utcnow().isoformat() + "Z",
        "resource_type": "sale",
        "event_type": event_type,
        "resource": {
            "id": orders_id,
            "amount": {
                "total": amount,
                "currency": currency
            },
            "create_time": datetime.datetime.utcnow().isoformat() + "Z",
            "update_time": datetime.datetime.utcnow().isoformat() + "Z",
            "links": [
                {
                    "href": f"https://api.paypal.com/v1/payments/sale/{orders_id}",
                    "rel": "self",
                    "method": "GET"
                },
                {
                    "href": f"https://api.paypal.com/v1/payments/sale/{orders_id}/refund",
                    "rel": "refund",
                    "method": "POST"
                }
            ]
        },
        "links": [
            {
                "href": f"https://api.paypal.com/v1/notifications/webhooks-events/WH-{base_id}",
                "rel": "self",
                "method": "GET",
                "encType": "application/json"
            },
            {
                "href": f"https://api.paypal.com/v1/notifications/webhooks-events/WH-{base_id}/resend",
                "rel": "resend",
                "method": "POST",
                "encType": "application/json"
            }
        ],
        "event_version": "1.0"
    }

    # Configuración específica por tipo de evento
    if event_type == "PAYMENT.SALE.COMPLETED":
        event_base["summary"] = f"A successful sale payment was made for $ {amount} {currency}"
        event_base["resource"]["state"] = "completed"
        event_base["resource"]["payment_mode"] = "INSTANT_TRANSFER"
        event_base["resource"]["protection_eligibility"] = "ELIGIBLE"
        event_base["resource"][
            "protection_eligibility_type"] = "ITEM_NOT_RECEIVED_ELIGIBLE,UNAUTHORIZED_PAYMENT_ELIGIBLE"
        event_base["resource"]["parent_payment"] = f"PAY-{str(uuid.uuid4())[:18].upper()}"
        event_base["resource"]["clearing_time"] = (datetime.datetime.utcnow() + datetime.timedelta(days=7)).strftime(
            "%Y-%m-%dT07:00:00Z")

    elif event_type == "PAYMENT.SALE.DENIED":
        print("entro a denegado")
        event_base["summary"] = f"A {currency} {amount} sale payment was denied"
        event_base["resource"]["state"] = "denied"
        event_base["resource"]["payment_mode"] = "INSTANT_TRANSFER"
        event_base["resource"]["protection_eligibility"] = "INELIGIBLE"
        event_base["resource"]["parent_payment"] = f"PAY-{str(uuid.uuid4())[:18].upper()}"

    elif event_type == "PAYMENT.SALE.REVERSED":
        event_base["summary"] = f"A $ {amount} {currency} sale payment was reversed"
        event_base["resource"]["state"] = "completed"
        # Para eventos reversados, el monto es negativo
        event_base["resource"]["amount"]["total"] = f"-{amount}"
        # event_base["resource"]["id"] = f"77{str(uuid.uuid4())[:14].upper()}G"  # Formato similar al ejemplo

    elif event_type == "PAYMENT.SALE.REFUNDED":
        event_base["summary"] = f"A $ {amount} {currency} sale payment was refunded"
        event_base["resource"]["state"] = "refunded"
        event_base["resource"]["parent_payment"] = f"PAY-{str(uuid.uuid4())[:18].upper()}"
        event_base["resource"]["sale_id"] = orders_id

    # En la función generate_event_body, agrega este nuevo caso elif:
    elif event_type == "CHECKOUT.ORDER.APPROVED":
        event_base["summary"] = "An order has been approved by buyer"
        event_base["resource_type"] = "checkout-order"
        event_base["resource"] = {
            "id": orders_id,
            "intent": "CAPTURE",
            "status": "APPROVED",
            "payment_source": {
                "paypal": {
                    "email_address": "example@correo.com",
                    "account_id": "#########",
                    "account_status": "VERIFIED",
                    "name": {
                        "given_name": "Julio",
                        "surname": "Peres Díaz"
                    },
                    "address": {
                        "country_code": "UY"
                    }
                }
            },
            "purchase_units": [
                {
                    "reference_id": "P00000002",
                    "amount": {
                        "currency_code": currency,
                        "value": amount
                    },
                    "payee": {
                        "email_address": "corre@coree.com",
                        "merchant_id": "LK75TNJQ7PKQU"
                    },
                    "shipping": {
                        "name": {
                            "full_name": "Su nombre"
                        },
                        "address": {
                            "address_line_1": "Su direccio 1",
                            "address_line_2": "#####, Apto ###",
                            "admin_area_2": "Montevideo",
                            "admin_area_1": "Departamento de Montevideo",
                            "postal_code": "88888",
                            "country_code": "UY"
                        }
                    },
                    "supplementary_data": {
                        "tax_nexus": []
                    }
                }
            ],
            "payer": {
                "name": {
                    "given_name": "Nombre",
                    "surname": "El nombre"
                },
                "email_address": "elcorreo@otro.me",
                "payer_id": "Y2F63SA7SCQC4",
                "address": {
                    "country_code": "UY"
                }
            },
            "create_time": datetime.datetime.utcnow().isoformat() + "Z",
            "links": [
                {
                    "href": f"https://api.paypal.com/v2/checkout/orders/{orders_id}",
                    "rel": "self",
                    "method": "GET"
                },
                {
                    "href": f"https://api.paypal.com/v2/checkout/orders/{orders_id}",
                    "rel": "update",
                    "method": "PATCH"
                },
                {
                    "href": f"https://api.paypal.com/v2/checkout/orders/{orders_id}/capture",
                    "rel": "capture",
                    "method": "POST"
                }
            ]
        }

    return event_base


# Almacenamiento en memoria para los webhooks registrados
registered_webhooks = {}


# Generar certificado autofirmado al inicio
def generate_certificate():
    import datetime
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, u"US"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, u"CA"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, u"San Francisco"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"PayPal Simulator"),
        x509.NameAttribute(NameOID.COMMON_NAME, u"localhost"),
    ])

    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        private_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.datetime.utcnow()
    ).not_valid_after(
        datetime.datetime.utcnow() + datetime.timedelta(days=365)
    ).add_extension(
        x509.SubjectAlternativeName([x509.DNSName(u"localhost")]),
        critical=False,
    ).sign(private_key, hashes.SHA256(), default_backend())

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    )

    return cert_pem.decode(), private_key, str(uuid.uuid4())[:10]


# Generar certificado y clave privada
CERT_PEM, PRIVATE_KEY, CERT_ID = generate_certificate()


@app.get("/cert/{certi_id}", response_class=PlainTextResponse)
async def get_certificate(certi_id: str):
    """Endpoint que simula la entrega del certificado de PayPal"""
    return CERT_PEM


@app.post("/register-webhook")
async def register_webhook(
        webhook_id: str = Form(...),
        target_url: str = Form(...),
        event_type: str = Form("PAYMENT.SALE.COMPLETED")  # Valor por defecto
):
    """Registra un webhook para simulación con un tipo de evento específico"""
    import datetime
    webhook_uuid = str(uuid.uuid4())
    registered_webhooks[webhook_uuid] = {
        "webhook_id": webhook_id,
        "target_url": target_url,
        "event_type": event_type,
        "created_at": datetime.datetime.utcnow().isoformat()
    }
    return {"webhook_uuid": webhook_uuid}


@app.post("/trigger-webhook/{webhook_uuid}")
async def trigger_webhook(webhook_uuid: str):
    """Dispara un webhook simulado a la URL registrada"""
    import datetime
    if webhook_uuid not in registered_webhooks:
        return {"error": "Webhook no encontrado"}, 404

    webhook_data = registered_webhooks[webhook_uuid]
    webhook_id = webhook_data["webhook_id"]
    target_url = webhook_data["target_url"]
    event_type = webhook_data["event_type"]  # Obtener el tipo de evento solicitado

    # Generar el cuerpo del evento basado en el tipo seleccionado
    event_body = generate_event_body(
        event_type,
        DATA_PAYPAL["orders_id"],
        DATA_PAYPAL["amount"],
        DATA_PAYPAL["currency"]
    )

    event_json = json.dumps(event_body).encode('utf-8')
    print(event_json)
    # Generar parámetros para la firma
    transmission_id = str(uuid.uuid4())
    timestamp = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    crc = zlib.crc32(event_json) & 0xFFFFFFFF

    # Crear mensaje para firmar
    message = f"{transmission_id}|{timestamp}|{webhook_id}|{crc}"
    print(f"Mensaje firmado: {message}")

    # Firmar el mensaje
    signature = PRIVATE_KEY.sign(
        message.encode('utf-8'),
        padding.PKCS1v15(),
        hashes.SHA256()
    )
    signature_base64 = base64.b64encode(signature).decode('utf-8')

    # Headers del webhook
    headers = {
        'Content-Type': 'application/json',
        'PayPal-Transmission-Id': transmission_id,
        'PayPal-Transmission-Time': timestamp,
        'PayPal-Cert-Url': f"http://127.0.0.1:7000/cert/{CERT_ID}",
        'PayPal-Transmission-Sig': signature_base64,
        'PayPal-Auth-Algo': 'SHA256withRSA'
    }

    # Enviar webhook
    try:
        response = requests.post(
            target_url,
            data=event_json,
            headers=headers,
            timeout=5
        )
        return {
            "status": "success" if response.status_code in [200, 201] else "failed",
            "status_code": response.status_code,
            "response": response.text[:200] + "..." if len(response.text) > 200 else response.text,
            "headers_sent": headers,
            "event_sent": event_body,
            "event_type": event_type  # Incluir el tipo de evento en la respuesta
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "event_type": event_type
        }


@app.get("/", response_class=HTMLResponse)
async def home():
    """Página HTML para configurar y disparar webhooks"""
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Simulador de Webhooks de PayPal</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
                line-height: 1.6;
            }
            .container {
                background-color: #f8f9fa;
                border-radius: 8px;
                padding: 25px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }
            h1 {
                color: #003087;
                text-align: center;
                margin-bottom: 30px;
            }
            .form-group {
                margin-bottom: 20px;
            }
            label {
                display: block;
                margin-bottom: 8px;
                font-weight: bold;
                color: #333;
            }
            input[type="text"], select {
                width: 100%;
                padding: 10px;
                border: 1px solid #ddd;
                border-radius: 4px;
                font-size: 16px;
            }
            button {
                background-color: #003087;
                color: white;
                border: none;
                padding: 12px 24px;
                font-size: 16px;
                border-radius: 4px;
                cursor: pointer;
                width: 100%;
                transition: background-color 0.3s;
            }
            button:hover {
                background-color: #00215e;
            }
            .result {
                margin-top: 25px;
                padding: 15px;
                border-radius: 4px;
                display: none;
            }
            .success {
                background-color: #d4edda;
                color: #155724;
                border: 1px solid #c3e6cb;
            }
            .error {
                background-color: #f8d7da;
                color: #721c24;
                border: 1px solid #f5c6cb;
            }
            .info {
                background-color: #d1ecf1;
                color: #0c5460;
                border: 1px solid #bee5eb;
            }
            pre {
                background-color: #f1f1f1;
                padding: 10px;
                border-radius: 4px;
                overflow-x: auto;
                margin-top: 10px;
                font-size: 14px;
            }
            .note {
                background-color: #e8f4fd;
                border-left: 4px solid #007bff;
                padding: 12px 15px;
                margin: 20px 0;
                border-radius: 0 4px 4px 0;
            }
            .event-types {
                display: flex;
                flex-wrap: wrap;
                gap: 10px;
                margin-top: 15px;
            }
            .event-type {
                background: #e9ecef;
                padding: 8px 12px;
                border-radius: 4px;
                font-size: 14px;
            }
            .event-type.completed { color: #28a745; }
            .event-type.denied { color: #dc3545; }
            .event-type.reversed { color: #ffc107; }
            .event-type.refunded { color: #17a2b8; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Simulador de Webhooks de PayPal</h1>
            <div class="note">
                <strong>Importante:</strong> Este simulador te permite probar localmente la validación de webhooks de PayPal.
                Necesitas tener tu aplicación corriendo en la URL objetivo con el WEBHOOK_ID correcto.
            </div>

            <div class="note">
                <strong>Tipos de eventos disponibles:</strong>
                <div class="event-types">
                    <span class="event-type completed">PAYMENT.SALE.COMPLETED</span>
                    <span class="event-type denied">PAYMENT.SALE.DENIED</span>
                    <span class="event-type reversed">PAYMENT.SALE.REVERSED</span>
                    <span class="event-type refunded">PAYMENT.SALE.REFUNDED</span>
                </div>
            </div>

            <form id="webhookForm" class="form-group">
                <div class="form-group">
                    <label for="webhookId">Webhook ID (igual que en tu aplicación):</label>
                    <input type="text" id="webhookId" name="webhook_id" 
                           value="2PF391667E866582P" required>
                    <small>Este debe coincidir con el WEBHOOK_ID configurado en tu aplicación</small>
                </div>
                <div class="form-group">
                    <label for="targetUrl">URL Objetivo (donde tu app escucha webhooks):</label>
                    <input type="text" id="targetUrl" name="target_url" 
                           value="http://localhost:8000/api/paypal/webhooks/notifications/" required>
                    <small>Ejemplo: http://localhost:8888/webhook</small>
                </div>
                <div class="form-group">
                    <label for="eventType">Tipo de Evento a Simular:</label>
                    <select id="eventType" name="event_type">
                        <option value="PAYMENT.SALE.COMPLETED">✅ Pago Completado</option>
                        <option value="PAYMENT.SALE.DENIED">❌ Pago Denegado</option>
                        <option value="PAYMENT.SALE.REVERSED">🔄 Pago Revertido</option>
                        <option value="PAYMENT.SALE.REFUNDED">💰 Pago Reembolsado</option>
                        <option value="CHECKOUT.ORDER.APPROVED">🛒 Order Aprobado</option>
                    </select>
                </div>
                <button type="submit">Registrar y Disparar Webhook</button>
            </form>
            <div id="result" class="result"></div>
        </div>
        <script>
            document.getElementById('webhookForm').addEventListener('submit', async (e) => {
                e.preventDefault();
                const resultDiv = document.getElementById('result');
                resultDiv.style.display = 'block';
                resultDiv.className = 'result info';
                resultDiv.innerHTML = '<p>Registrando webhook y enviando solicitud...</p>';
                try {
                    // 1. Registrar el webhook primero
                    const registerResponse = await fetch('/register-webhook', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded',
                        },
                        body: new URLSearchParams({
                            'webhook_id': document.getElementById('webhookId').value,
                            'target_url': document.getElementById('targetUrl').value,
                            'event_type': document.getElementById('eventType').value
                        })
                    });
                    const registerData = await registerResponse.json();
                    if (!registerResponse.ok) {
                        throw new Error(registerData.detail || 'Error al registrar webhook');
                    }
                    // 2. Disparar el webhook con el UUID recibido
                    const triggerResponse = await fetch(`/trigger-webhook/${registerData.webhook_uuid}`, {
                        method: 'POST'
                    });
                    const triggerData = await triggerResponse.json();
                    // Mostrar resultados
                    if (triggerResponse.ok && (triggerData.status === 'success' || triggerData.status_code)) {
                        resultDiv.className = 'result success';
                        resultDiv.innerHTML = `
                            <h3>¡Éxito! Webhook ${triggerData.event_type} procesado correctamente</h3>
                            <p>Código de estado: ${triggerData.status_code}</p>
                            <p>Respuesta del servidor:</p>
                            <pre>${JSON.stringify(JSON.parse(triggerData.response || '{}'), null, 2)}</pre>
                            <p>Webhook ID utilizado: ${document.getElementById('webhookId').value}</p>
                            <p>Tipo de evento: ${triggerData.event_type}</p>
                        `;
                    } else {
                        resultDiv.className = 'result error';
                        resultDiv.innerHTML = `
                            <h3>Error al procesar el webhook ${triggerData.event_type}</h3>
                            <p>Código de estado: ${triggerData.status_code || 'N/A'}</p>
                            <p>Mensaje de error:</p>
                            <pre>${JSON.stringify(triggerData, null, 2)}</pre>
                            <div class="note">
                                <p><strong>Posibles causas:</strong></p>
                                <ul>
                                    <li>El WEBHOOK_ID no coincide con el configurado en tu aplicación</li>
                                    <li>La URL objetivo no está accesible o no responde correctamente</li>
                                    <li>Problemas en la implementación de validación de firma</li>
                                    <li>Tipo de evento no soportado por tu aplicación</li>
                                </ul>
                            </div>
                        `;
                    }
                } catch (error) {
                    resultDiv.className = 'result error';
                    resultDiv.innerHTML = `
                        <h3>Error en la simulación</h3>
                        <p>${error.message}</p>
                        <pre>${error.stack}</pre>
                    `;
                }
            });
        </script>
    </body>
    </html>
    """


if __name__ == "__main__":
    import uvicorn

    print("Iniciando simulador de PayPal Webhooks...")
    print("Accede a http://localhost:8000 para configurar y disparar webhooks")
    uvicorn.run(app, host="0.0.0.0", port=8000)