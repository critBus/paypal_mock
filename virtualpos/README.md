# Mock VirtualPOS v3

Este módulo reproduce el flujo mínimo de Payments + Web Checkout descrito por
VirtualPOS. El almacenamiento es en memoria y se reinicia con el proceso.

## Endpoints

| Método | Ruta | Uso |
| --- | --- | --- |
| `POST` | `/v3/payment` o `/v3/payment/` | Crear Payment en estado `pendiente` |
| `POST` | `/v3/payment/{uuid}/webcheckout` | Obtener el enlace de pago |
| `GET` | `/v3/payment/{uuid}` | Consultar el Payment y su estado |
| `GET` | `/virtualpos/checkout/{uuid}` | Abrir la pantalla con pago correcto/fallido |
| `GET` | `/admin/virtualpos/payments` | Inspeccionar Payments en memoria |
| `GET` | `/admin/virtualpos/webhook-deliveries` | Inspeccionar entregas de webhook |
| `POST` | `/admin/virtualpos/reset` | Limpiar el estado del mock |

`return_url` y `callback_url` se aceptan en Base64, como exige la API real. Para
facilitar pruebas manuales, también se aceptan como URL HTTP(S) sin codificar.

Al pulsar uno de los botones del checkout, el mock:

1. cambia el estado a `pagado` o `rechazado`;
2. hace `POST application/x-www-form-urlencoded` al `callback_url` con `uuid`;
3. genera un formulario que hace `POST` al `return_url`, también con `uuid`.

Un pago rechazado conserva el mismo enlace y puede reintentarse.

## Configuración opcional

| Variable | Valor por defecto | Descripción |
| --- | --- | --- |
| `MOCK_VIRTUALPOS_PUBLIC_URL` | URL de la petición | Base pública usada para generar el enlace de pago |
| `MOCK_VIRTUALPOS_STRICT_AUTH` | `false` | Si es `true`, exige `Authorization` y `Signature` |
| `MOCK_VIRTUALPOS_WEBHOOK_TIMEOUT` | `10` | Timeout en segundos para el webhook |

Ejemplo de creación:

```bash
RETURN_URL=$(printf 'http://localhost:8001/payment/result' | base64 -w0)
CALLBACK_URL=$(printf 'http://localhost:8001/payment/webhook' | base64 -w0)

curl -X POST http://localhost:7000/v3/payment/ \
  -H 'Content-Type: application/json' \
  -H 'Authorization: test-api-key' \
  -H 'Signature: test-signature' \
  -d "{\"amount\":9990,\"email\":\"user@example.com\",\"social_id\":\"12345678-9\",\"first_name\":\"John\",\"last_name\":\"Doe\",\"phone\":\"56912345678\",\"description\":\"Pago de prueba\",\"merchant_internal_code\":\"OC_TEST_1234\",\"merchant_internal_channel\":\"portal_pagos\",\"return_url\":\"$RETURN_URL\",\"callback_url\":\"$CALLBACK_URL\"}"
```

Con el `uuid` de la respuesta:

```bash
curl -X POST http://localhost:7000/v3/payment/UUID/webcheckout \
  -H 'Content-Type: application/json' \
  -d "{\"return_url\":\"$RETURN_URL\",\"callback_url\":\"$CALLBACK_URL\",\"payment_method\":\"all\"}"
```

Abre en el navegador el campo `url` de esta última respuesta.
