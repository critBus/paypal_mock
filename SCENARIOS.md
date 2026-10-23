# Escenarios para reportes y recuperación de pagos

Inicia el mock como siempre:

```sh
uv run uvicorn main:app --reload --port 7000
```

Abre **http://localhost:7000/mock/scenarios**. El enlace también está en la página
principal y en los checkouts de PayPal y VirtualPOS. El panel permite elegir un
pago existente o la configuración de los **próximos pagos**. Cambiar un escenario
no cambia los demás pagos ni envía por sí mismo una notificación.

Para ver los resultados en los reportes, crea los pagos desde Multicore o
CrinnoPayments y selecciona aquí su ID remoto. Crear un pago directamente en el
mock sirve para probar su API, pero no crea un PaymentRequest en CrinnoPayments.
Para probar cruces entre TPV, utiliza el mismo código comercial, tienda y entorno
desde la aplicación consumidora, con correos iguales o distintos según el caso.

## PayPal

Los escenarios cubren:

- Aprobación sin `CHECKOUT.ORDER.APPROVED`, para probar la recuperación por admin o API.
- Ausencia de todos los webhooks, entregas duplicadas, firma incorrecta o retrasos.
- 404 JSON `RESOURCE_NOT_FOUND` / `INVALID_RESOURCE_ID`; se conservan `name`,
  `details` y `debug_id`. El escenario se mantiene hasta desactivarlo. No se
  atribuye un plazo fijo de caducidad real de PayPal.
- 404 HTML, JSON ilegible, servicio no disponible y demoras de respuesta: son
  resultados inconcluyentes, distintos del 404 JSON anterior.
- `INSTRUMENT_DECLINED`, `CARD_EXPIRED` y códigos personalizados por operación.
- Captura `DECLINED`, `PENDING`, rechazo del procesador `5120` y autenticación 3DS no completada.
- Captura completada con respuesta HTTP 503 y webhook omitido: el GET posterior
  muestra el cobro real, para probar que no se vuelva a capturar.
- Respuesta de captura con importe incorrecto, sin modificar el importe real
  guardado en el mock.
- Error de creación y rechazo de credenciales en OAuth. Si el cliente conserva
  un access token válido en caché, el escenario OAuth solo se verá cuando solicite otro.

El checkout aprueba la orden, pero no la cobra. Una orden sin aprobar no se
captura. Un POST repetido con el mismo `PayPal-Request-Id` devuelve el resultado
original; una nueva clave después de una captura devuelve `ORDER_ALREADY_CAPTURED`.
GET incluye la captura existente con ID, estado, importe y moneda. Una captura
pendiente o rechazada tampoco origina otra captura con una nueva clave.

La falta de 3DS mantiene `PAYER_ACTION_REQUIRED`, con el resultado de autenticación
registrado, sin emitir aprobación. No se inventa un cobro ni una aprobación.

Configurar un escenario para una orden concreta:

```sh
curl -X POST http://localhost:7000/mock/paypal/controls \
  -H 'Content-Type: application/json' \
  -d '{"resource_id":"ID_DE_ORDEN","scenario":"approved_no_webhook"}'
```

Después se aprueba desde el checkout y se ejecuta la recuperación desde
CrinnoPayments. Para preparar los próximos pagos, omite `resource_id`.

Resolver una captura pendiente, sin otro POST `/capture`:

```sh
curl -X POST http://localhost:7000/mock/paypal/orders/ID_DE_ORDEN/resolve-capture \
  -H 'Content-Type: application/json' \
  -d '{"status":"COMPLETED","notify":true}'
```

También admite `DECLINED`. Para recuperar notificaciones o simular una aprobación
que llega tarde, selecciona «Flujo normal» y utiliza el botón **Reenviar notificación**.
El endpoint equivalente es:

```sh
curl -X POST http://localhost:7000/mock/paypal/orders/ID_DE_ORDEN/replay \
  -H 'Content-Type: application/json' -d '{"event":"approval"}'
```

`event` también admite `capture`. El reenvío no ejecuta otro cobro. El escenario
vigente sigue aplicándose: una notificación configurada como omitida seguirá omitida.

## VirtualPOS

El panel fuerza el resultado al utilizar el checkout, incluso si se pulsa «Pago
correcto». Incluye rechazos `-5`, `-7`, `-8`, `-1017`, `-1018`, estados `pendiente`,
`reintentando`, `expirado`, `cancelado`, errores API `E-003`/`E-021`/`E-027`, UUID o
importe incorrectos, JSON ilegible y callbacks omitidos, duplicados o con UUID equivocado.
Los códigos de rechazo también pueden configurarse explícitamente.

Las negativas financieras se devuelven en `payment.order.rejected_object`, con
`error_code`, `message` y `doc_url`. Los errores de operación se devuelven en
`error.error_code`, `message` y `doc_url`, con código HTTP de error. Un error API
no equivale a un rechazo bancario.

Ejemplo para provocar un fallo de autenticación bancaria:

```sh
curl -X POST http://localhost:7000/mock/virtualpos/controls \
  -H 'Content-Type: application/json' \
  -d '{"resource_id":"UUID_DEL_PAYMENT","scenario":"reject_authentication"}'
```

El callback continúa siendo un POST con **solo `uuid`**, como en el flujo real.
El consumidor debe consultar el Payment para conocer el resultado; el webhook
no proporciona por sí mismo el estado pagado o fallido.

Un rechazo permite reintentar con el mismo UUID. Un Payment pagado, expirado o
cancelado no puede cobrarse de nuevo. El checkout acepta importes enteros desde
1000 CLP y hasta el máximo int32, siguiendo la validación documentada; los datos
incorrectos devuelven un objeto Error, en lugar de `detail` genérico de FastAPI.

Los endpoints locales de diagnóstico son:

- `GET /mock/virtualpos/controls`: escenarios, valores predeterminados y Payments.
- `GET /mock/virtualpos/payments/{uuid}/history`: llamadas e intentos de autorización locales.
- `POST /mock/virtualpos/payments/{uuid}/replay`: reenvío del callback sin otra autorización.
- `GET /admin/virtualpos/webhook-deliveries`: resultados de entrega.

El historial local no pretende implementar el contrato de los endpoints remotos
`/logs` o `/attempts`, cuyo contenido no utiliza la integración actual.

## Errores avanzados y uso único

Operaciones disponibles: PayPal `oauth`, `create`, `get`, `capture`; VirtualPOS
`create`, `get`, `checkout`. Cada pago conserva sus propios contadores, separados
de los de otros pagos. Las operaciones anteriores a crear un pago usan los
valores predeterminados.

Ejemplo: una única consulta fallida, seguida de consultas normales:

```sh
curl -X POST http://localhost:7000/mock/paypal/controls \
  -H 'Content-Type: application/json' \
  -d '{"resource_id":"ID_DE_ORDEN","scenario":"normal","faults":{"get":{"kind":"error","name":"SERVICE_UNAVAILABLE","issue":"SERVICE_UNAVAILABLE","http_status":503,"times":1}}}'
```

`times: -1` mantiene el fallo hasta cambiarlo. `times: 0` lo desactiva. Los tipos
son `error`, `html404`, `invalid_json`, `delay`, `wrong_amount`, `wrong_currency`,
`wrong_id`, `wrong_reference`. Para simular un timeout utiliza `kind: "delay"`
con `delay_seconds` mayor que el timeout del cliente, hasta 30 segundos. La
respuesta puede continuar procesándose aunque el cliente deje de esperarla.

`webhook_delay_seconds` admite de 0 a 20 segundos. En PayPal se puede ajustar por
separado `approval_webhook_mode`; `webhook_mode` controla las notificaciones de
captura. Para volver al comportamiento normal, aplica `scenario: "normal"` al
pago y/o a los próximos pagos, según lo que quieras cambiar. Los contadores y el
historial financiero no se borran al cambiar de escenario.

El campo opcional `status`, solo para recursos existentes, permite preparar un
estado sin emitir notificación. En PayPal, `COMPLETED` crea evidencia de una captura
simulada completa si no existía. No se permite reabrir recursos en un estado final.
Las órdenes con el mismo código comercial siguen siendo órdenes distintas para
el proveedor: impedir dobles cobros entre esos intentos corresponde a CrinnoPayments.

## Persistencia, ejecución y pruebas

Las órdenes Orders v2, Payments de VirtualPOS y sus escenarios se guardan en
`data/scenarios.sqlite3`. Es SQLite de la biblioteca estándar y no añade
servicios ni dependencias. Configura `MOCK_SCENARIOS_DB_PATH` para cambiar la ruta.
La apertura es diferida; importar el módulo no crea el archivo.

Las nuevas órdenes conservan su configuración al reiniciar. Los datos de procesos
anteriores que solo estaban en memoria no pueden recuperarse después de reiniciarlos.
Los Payment Links de PayPal conservan su almacenamiento anterior; los cambios de
persistencia y escenarios de PayPal se centran en Orders v2.

Usa **un worker** para las simulaciones de concurrencia: los locks de captura son
locales al proceso. El resultado financiero se guarda antes de notificar. Los
reenvíos y consultas no incrementan el número de capturas o autorizaciones reales
simuladas. Las entregas de webhooks se conservan en memoria para diagnóstico del
proceso; los pagos y sus historiales de llamadas e intentos son persistentes.

`uv run pytest tests` utiliza bases temporales y transportes simulados. Las pruebas
comprueban códigos y estructuras, aislamiento de fallos, persistencia entre procesos,
concurrencia, idempotencia, pérdida de respuesta, resolución de capturas pendientes,
reintentos de VirtualPOS, callbacks y controles del panel.

BMS conserva sus eventos como `unconfigured` si no se han indicado explícitamente
`BMS_MOCK_WEBHOOK_URL` y `BMS_MOCK_WEBHOOK_TOKEN`; ya no usa valores de entrega
implícitos. Tropipay mantiene sus controles y los rechazos configurables por tarjeta
existentes en el checkout.

Referencias: [PayPal Orders v2](https://developer.paypal.com/api/orders/v2/),
[Payment VirtualPOS](https://virtualpos.readme.io/reference/objeto-payment),
[Rechazos VirtualPOS](https://virtualpos.readme.io/reference/objeto-rechazo),
[Errores VirtualPOS](https://virtualpos.readme.io/reference/objeto-error).
