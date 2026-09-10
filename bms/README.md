# BMSPay: Payment Links en el mock

El módulo conserva las rutas de venta con tarjeta y 3DS. Añade el contrato usado por
el flujo alojado de CrinnoPayments:

| Operación | Ruta |
| --- | --- |
| Crear enlace | `POST /api/PaymentLinks/AddPaymentLink` |
| Consultar por ID o factura | `GET /api/PaymentLinks/GetPaymentLink?id=...` |
| Desactivar | `PUT /api/PaymentLinks/DisablePaymentLink` |
| Listar | `GET /api/PaymentLinks/GetPaymentLinks` |
| Checkout navegable | `GET /payments/link/{id}` |

También admite `POST/GET /api/PaymentLinks`, `GET /api/PaymentLinks/{id}` y el alias
`GetPaymentLinkById`. Las consultas aceptan las credenciales `request.*` en query
que envía CrinnoPayments y, por compatibilidad con la guía anterior, JSON en GET.
Los errores de negocio se devuelven con HTTP 200 y `ResponseCode`/`Msg` en la raíz,
sin el envoltorio `detail` de FastAPI.

## Conexión con CrinnoPayments

Ejecutar el mock con `uvicorn main:app` desde la raíz del proyecto. Configurar en
`BMSConfiguration.merchant_api_url` la URL base del mock, sin `/api`.

Variables de entorno opcionales del proceso:

- `BMS_MOCK_DB_PATH`: archivo SQLite; por defecto `data/bms.sqlite3`.
- `BMS_MOCK_PUBLIC_URL`: URL pública base para los enlaces de checkout. Si no se
  proporciona, se usa la URL base de la petición que crea el enlace.

**CrinnoPayments valida que `PaymentLink.Link` sea HTTPS.** Para probar la integración
completa, publicar el mock mediante HTTPS (proxy o servidor TLS) y definir, por ejemplo,
`BMS_MOCK_PUBLIC_URL=https://mock.example`. Debe ser una dirección real que alcance
este mock desde el navegador. La conexión backend→mock puede usar una URL HTTP interna.
No cambiar sólo el esquema a HTTPS si el servidor no ofrece TLS. El mock aislado sigue
permitiendo HTTP para pruebas con TestClient o curl.

Credenciales existentes del mock (`bms/config.py`):

```json
{
  "mid": "76074",
  "cid": "260",
  "AppKey": "12345",
  "AppType": "1",
  "UserName": "nicolas",
  "Password": "password1"
}
```

Se aceptan números o cadenas para los identificadores. Para crear, añadir al JSON:

```json
{
  "PaymentLink": {
    "Amount": "10.50",
    "Description": "Pedido de prueba",
    "InvoiceNumber": "ORDER-1",
    "AllowedPaymentSchedule": "OnlySingle"
  }
}
```

El alcance de este mock es el pago único de importe fijo. `Amount` es el total final,
no se agrega ningún recargo y se rechazan importes vacíos, no positivos o con más de
dos decimales. No se simulan enlaces de importe abierto ni cobros recurrentes.

## Completar o rechazar un pago

Abrir `PaymentLink.Link`. La pantalla permite simular éxito, rechazo o cancelar y
volver a la tienda. Si el enlace lleva `returnUrl` (también acepta `returnURL`), los
tres botones vuelven a ese mismo destino del frontend. Sólo se aceptan destinos HTTP/HTTPS.

También se puede operar sin navegador:

```http
POST /mock-bms/payment-links/{id}/simulate
Content-Type: application/json

{"outcome": "success"}
```

Los resultados posibles son `success`, `decline` y `cancel`.

- Un enlace nuevo tiene `Status = 0` (Unpaid), `Active = true` y fechas de pago/desactivación vacías.
- Éxito asigna `Status = 1` (Paid), `PaidOn` y un `ServiceReferenceNumber` estable.
- Repetir el éxito no genera otro cobro ni otra referencia.
- Rechazar o abandonar el checkout mantiene el enlace pendiente para otro intento.
- `DisablePaymentLink` marca `Active = false` y `DisabledOn`; no modifica un pago confirmado.
- Una página abierta antes de desactivar tampoco puede completar el pago después.

El estado se guarda en SQLite, compartido entre procesos que usen el mismo archivo.
Las transiciones de pago y desactivación son atómicas. Se puede consultar el resultado
desde el admin o el endpoint de solicitud de CrinnoPayments. El mock genera webhooks
con los formatos publicados de BMSPay para los intentos aprobados y rechazados. La referencia simulada se consulta dentro de `PaymentLink`; las rutas
anteriores de transacciones siguen consultando las ventas directas.

## Webhooks hacia CrinnoPayments

En `BMSConfiguration` establecer `redirect_url` con la página de espera del frontend
y `webhook_token` con un token de prueba compartido con el mock. La recepción guarda
cada entrega, incluidas firmas incorrectas y duplicados; la señal encola la asociación
por PaymentLinkId/InvoiceNumber y la verificación antes de consultar el pago. El retorno del
navegador sólo abre esa página; el estado lo confirma el backend consultando el enlace.

Exportar las variables **en el proceso que ejecuta el mock** (no basta con añadirlas
al archivo `.env`, porque este módulo las lee del entorno del proceso):

```sh
export BMS_MOCK_WEBHOOK_URL='http://127.0.0.1:8000/api/bms/webhooks/notifications/'
export BMS_MOCK_WEBHOOK_TOKEN='token-local-compartido'
export BMS_MOCK_WEBHOOK_VERSION='1'
export BMS_MOCK_WEBHOOK_ATTEMPTS='3'
```

Sustituir el host/puerto por el de CrinnoPayments; el endpoint es común para todas las configuraciones. Si el mock está en
un contenedor, usar un host alcanzable desde él. La URL HTTP se admite para pruebas
locales; BMSPay real exige HTTPS. En BMSPay real estos valores se configuran en el
portal **Business Settings → Developers → Webhooks**, con una suscripción activa Sale.

Al simular `success` o `decline`, se guarda el evento junto al estado del enlace en
una transacción SQLite. Se envía después como POST JSON con cabecera `x-signature`,
en segundo plano para permitir que el navegador vuelva al frontend mientras espera
confirmación. `cancel`, desactivar el enlace y repetir un pago ya completado no
emiten una nueva notificación de transacción.

- Versión 1 predeterminada: `Request` y `Response`, con PaymentLinkId/InvoiceNumber.
- Versión 2: añade EventId estable, MerchantId, IsTest, Source, Stage y Outcome. Se
  puede probar en el mock; en BMSPay real su disponibilidad requiere activación.
- Se consideran entregadas las respuestas HTTP 2xx. Cualquier otra respuesta o error
  de red reintenta con el mismo payload, con un segundo de espera entre intentos.
  `BMS_MOCK_WEBHOOK_ATTEMPTS` permite entre 1 y 10 intentos totales por envío; por defecto 3.
- Si falta URL/token, el evento queda `unconfigured`. Un fallo de entrega no revierte
  el pago: el backend puede confirmarlo mediante polling.
- Se guardan payload, estado de entrega, intentos y último código HTTP; no el token
  ni cuerpos de respuesta del receptor. Los eventos se conservan al reiniciar.

Controles exclusivos del mock:

```text
GET  /mock-bms/payment-links/{id}/webhooks
POST /mock-bms/payment-links/{id}/webhooks/{event_id}/send
```

El GET devuelve los eventos y su historial resumido de entrega. El POST devuelve
202 y envía de nuevo el mismo evento, incluso si ya estaba entregado: sirve para
probar deduplicación, recuperar fallos o enviar eventos creados antes de configurar
el receptor. Tras reiniciar el mock, los eventos pendientes se pueden reenviar con
este control; no hay recuperación automática de entregas interrumpidas al reiniciar.
La simulación devuelve `mock_webhook_event_ids` para localizarlos. Estos controles
no forman parte de la API real de BMSPay.

Para una prueba completa:

1. Aplicar las migraciones BMS de CrinnoPayments y arrancar su servidor y Huey.
2. Configurar URL/credenciales del mock en BMSConfiguration, `use_payment_link=true`,
   `redirect_url` y el mismo `webhook_token` en ambos proyectos.
3. Crear una solicitud y abrir el `checkout_url` retornado, que ya incluye `returnUrl`.
4. Simular éxito. El frontend regresa a su página de espera; comprobar el evento
   recibido y que BMSOrderPayment y BMSOrderRequest pasan a pagados/completados.
5. Reenviar el evento y verificar que no se repite la notificación de pago al consumidor.
   Probar otro pedido rechazado: su enlace seguirá pendiente y admite otro intento.

## Vencimiento y fallos temporales

El mock no implementa un TTL nativo: es Huey en CrinnoPayments quien llama a
`DisablePaymentLink` cuando vence `expire_pending_after`. Para probarlo, crear una
solicitud con `use_payment_link = true`, duración `PT1M` y el consumidor Huey activo.
Después, el checkout debe indicar que el enlace está desactivado.

Para comprobar los reintentos del backend:

```http
POST /mock-bms/payment-links/{id}/failure
Content-Type: application/json

{"operation": "disable", "response_code": 300, "remaining": 2}
```

Las dos siguientes desactivaciones devolverán un error de negocio 300 sin cambiar
el estado. La tercera podrá desactivar. También se admite `operation = retrieve`;
los códigos configurables son 1, 11, 28, 300, 404 y 506. `remaining = 0` elimina el fallo
pendiente de esa operación. Los controles son locales al mock y no pertenecen a la
API real. Su configuración se persiste pero no aparece en las respuestas públicas.

## Pruebas

```sh
.venv/bin/pytest tests/test_bms_payment_links.py -q
```

Las pruebas usan SQLite temporal, sin cobros ni llamadas a proveedores externos.

## Referencias oficiales

- [Retorno desde checkout](https://documentation.bmspay.com/core-apis/payment-links/)
- [Autenticación y recepción](https://documentation.bmspay.com/webhooks/receiving/)
- [Payloads v1 y v2](https://documentation.bmspay.com/webhooks/reference/)
