# Plaid Mock para CrinnoPayments

Simulador local de los endpoints Plaid usados por CrinnoPayments. Los datos son
ficticios y el módulo no contacta a Plaid ni realiza movimientos de dinero.

## Inicio y configuración

Arrancar la aplicación habitual desde la raíz del proyecto:

```sh
uv run uvicorn main:app --host 127.0.0.1 --port 7000
```

En la configuración Plaid de CrinnoPayments usar:

- `scope`: `sandbox`.
- `merchant_api_url`: `http://127.0.0.1:7000`.
- `client_id` y `secret`: valores de prueba no vacíos.
- `notification_url`: URL de CrinnoPayments accesible desde el mock,
  por ejemplo `http://127.0.0.1:8001/api/plaid/webhooks/notifications/`.
- Para Payments Europe, un `recipient_id` ficticio y un país/currency compatibles.
- Para Transfer, `US`/`USD` y un `link_customization_name` ficticio no vacío.

El módulo queda registrado al importar `main`. La referencia interactiva está
junto con las demás APIs en `/docs`.

Variables opcionales (el proyecto ya carga `.env`):

| Variable | Valor por defecto / función |
| --- | --- |
| `PLAID_MOCK_DB_PATH` | `data/plaid.sqlite3`; persiste cuentas, tokens, sesiones, pagos, clave de firma y entregas |
| `PLAID_MOCK_PUBLIC_BASE_URL` | Vacía: usa el origen de la petición; configurar el origen accesible por el navegador cuando se usa un proxy o contenedor |
| `PLAID_MOCK_STRICT_AUTH` | `false`; si es `true`, exige las credenciales siguientes |
| `PLAID_MOCK_CLIENT_ID` | `mock-plaid-client` |
| `PLAID_MOCK_SECRET` | `mock-plaid-secret` |

Todas las llamadas de API exigen `PLAID-CLIENT-ID` y `PLAID-SECRET`, o sus
campos equivalentes en el JSON. Incluso sin autenticación estricta, las
credenciales separan el estado de cada cliente. Usar siempre el mismo par
para recuperar recursos creados anteriormente. La página Hosted Link usa el
token de sesión de la URL y no requiere esos encabezados.

## Vincular una cuenta

Desde CrinnoPayments crear una sesión en `/api/plaid/bank-link-sessions/`
con `customer_id`, `products: ["transactions"]` y `country_code: "US"`.
La respuesta contiene una URL del servidor mock:

```text
http://127.0.0.1:7000/mock-plaid/hosted-link/link-sandbox-...
```

Abrirla en el navegador y pulsar **Autorizar cuenta o pago**. El mock crea
un Item con una cuenta, genera un token público y envía `SESSION_FINISHED`
con `status: SUCCESS` y `public_tokens`. CrinnoPayments verifica su firma,
intercambia el token y guarda la conexión. La pantalla permite cancelar
sin crear una cuenta. Para enlaces de pago también se puede simular un fallo.
La redirección final usa `hosted_link.completion_redirect_uri` si se configuró.

Los saldos simulados son 3500 disponibles / 3600 actuales. Se generan tres
transacciones fechadas en los últimos tres días: salario, supermercado y
transferencia. Consultar un rango de fechas que incluya esos días. Se
respetan `count`, `offset`, `start_date` y `end_date`.

`/link/token/get` permite recuperar los resultados sin webhook; los
resultados finalizados caducan después de seis horas. Los tokens públicos
caducan en 30 minutos y solo se pueden intercambiar una vez. La URL caduca
según `hosted_link.url_lifetime_seconds`. Repetir la autorización de una
sesión finalizada no crea otra cuenta ni otro pago.

## Endpoints implementados

Todos los endpoints de API usan POST con JSON:

- `/institutions/get`.
- `/link/token/create`, `/link/token/get`.
- `/item/public_token/exchange`, `/item/get`, `/item/remove`.
- `/accounts/get`, `/accounts/balance/get`.
- `/transactions/get`, `/statements/list`.
- `/webhook_verification_key/get`.
- `/user/create`.
- `/payment_initiation/payment/create`, `/payment_initiation/payment/get`.
- `/transfer/intent/create`, `/transfer/intent/get`, `/transfer/get`.
- `/sandbox/transfer/simulate`, `/transfer/event/sync`.

Statements requiere `products: ["statements"]` (o combinado con Transactions),
`country_codes: ["US"]` y `statements: {"start_date": "2026-01-01",
"end_date": "2026-02-01"}`. Devuelve metadatos ficticios de un estado bancario;
no se implementa descarga PDF ni extracción real.

En Transfer, autorizar el enlace crea una transferencia `pending`. Los
estados de `/sandbox/transfer/simulate` son `posted`, `settled`,
`funds_available`, `failed`, `returned` y `cancelled`. El consumidor sandbox
de CrinnoPayments puede avanzar automáticamente el pago exitoso. El stream
de eventos tiene cursor `after_id`, paginación `count` y `has_more`.
Los identificadores, importe, moneda, referencia y metadata se conservan
para las verificaciones de CrinnoPayments.

## Simular errores y nueva autorización

Con los encabezados de credenciales, enviar:

```http
POST /mock-plaid/items/{item_id}/simulate
Content-Type: application/json

{"outcome": "login_required"}
```

Los outcomes disponibles son:

- `login_required`: devuelve `ITEM_LOGIN_REQUIRED` al consultar datos y
  envía un webhook `ITEM / ERROR`. `/item/get` permite inspeccionar el error.
- `revoke`: invalida el acceso y envía `USER_PERMISSION_REVOKED`.
- `transactions_pending`: las consultas devuelven `PRODUCT_NOT_READY`.
- `transactions_ready`: vuelve a habilitar las transacciones.

Para recuperar `login_required`, usar `relink` en CrinnoPayments y
completar la nueva URL. El mock elimina el error del mismo Item y no genera
otro token público. Una conexión revocada exige una vinculación nueva.

## Firma y reintentos de webhooks

El mock firma los bytes JSON enviados con ES256, un `iat` actual y
`request_body_sha256`. `/webhook_verification_key/get` expone su clave
pública JWK. La clave privada persiste en SQLite para que los recibos puedan
verificarse aunque el mock se reinicie. No hay que desactivar la verificación
de firma de CrinnoPayments.

La simulación guarda un `webhook_id`. Si no hay redirección, se devuelve en
la respuesta de `/mock-plaid/hosted-link/{token}/simulate`. Para inspeccionar
la entrega y repetirla, con credenciales y JSON `{}`:

- `POST /mock-plaid/webhooks/{webhook_id}/get`.
- `POST /mock-plaid/webhooks/{webhook_id}/retry`.

Cada entrega registra intentos, código HTTP y estado. Un fallo de entrega
no revierte una autorización o un pago ya simulado. El reintento firma
nuevamente el mismo cuerpo. No se programan reintentos automáticos.

## Compatibilidad y límites

CrinnoPayments admite esta URL local solo en sandbox, si su origen coincide
con `merchant_api_url` y su ruta empieza por `/mock-plaid/hosted-link/`.
En producción continúa requiriendo la URL HTTPS de Plaid.

Este módulo cubre el subconjunto consumido por la integración, no toda la
API Plaid. No simula OAuth, bancos reales, decisiones de crédito, entrega
SMS/email, Multi-Item Link ni descarga de documentos. Los endpoints de
simulación y los tokens son para desarrollo local.

```sh
uv run pytest tests/test_plaid_mock.py -q
```

Referencias: [Hosted Link](https://plaid.com/docs/link/hosted-link/),
[Transfer](https://plaid.com/docs/api/products/transfer/) y
[Payment Initiation](https://plaid.com/docs/api/products/payment-initiation/).
