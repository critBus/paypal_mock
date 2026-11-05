### Instalar dependencias

Con el `pyproject.toml` ya creado:

`uv sync`

Esto instalará todo lo que esté en tu `pyproject.toml` y fijará versiones en `uv.lock`.

---

### Ejecutar FastAPI con uv

Por ejemplo:

`uv run uvicorn main:app --reload`

`uv run uvicorn main:app --reload --port 7000`

---

# Docs

`http://127.0.0.1:7000/docs`


### Persistencia del mock de Revolut

Las órdenes (incluidos sus tokens de checkout, pagos, reembolsos y duración de
caducidad) y los webhooks (URL, eventos y clave de firma) se guardan automáticamente
en `data/revolut.sqlite3`. Los cambios se confirman antes de responder o enviar
una notificación. Reiniciar el servidor o usar `--reload` conserva estos datos.

Se utiliza `sqlite3`, incluido en Python: no hay dependencias adicionales y se
sigue ejecutando con `uv run uvicorn main:app --reload --port 7000`. La base se
crea automáticamente en el primer acceso y está excluida de Git.

Para cambiar su ubicación, configura `REVOLUT_MOCK_DB_PATH` en `.env` o en el
entorno, preferiblemente con una ruta absoluta. La ruta por defecto se calcula
desde el directorio del proyecto, independientemente del directorio de ejecución.
En contenedores, guarda ese archivo en un volumen persistente.

Para empezar de cero, detén el servidor y elimina el archivo SQLite. Los datos
que estaban únicamente en memoria antes de esta versión deberán crearse una
última vez. PayPal Orders v2 y VirtualPOS también conservan sus datos; consulta los escenarios de prueba descritos más abajo.

Pruebas: `uv run pytest tests`. Usan bases temporales y no modifican los datos
locales.

## Plaid

El simulador incorpora Hosted Link, autorización bancaria, cuentas, saldos,
transacciones, estados bancarios, pagos europeos, Transfer y webhooks ES256.
Configuración y ejemplos en [plaid/README.md](plaid/README.md).

## Escenarios de pagos y reportes

Abre [el panel de escenarios](http://localhost:7000/mock/scenarios) para forzar
fallos de PayPal y VirtualPOS por pago o para los próximos pagos: 404 estructurado,
rechazos, capturas pendientes, pérdida de respuesta, errores de API y webhooks
omitidos/duplicados/incorrectos. Los escenarios y estos pagos sobreviven a reinicios.

Ejemplos, controles avanzados, persistencia y guía de pruebas en [SCENARIOS.md](SCENARIOS.md).

El flujo de enlaces PayPal también admite reconciliación por eventos y Transaction Search,
retrasos independientes, entrega omitida y reenvío con el mismo ID de evento.
Configuración y endpoints en [SCENARIOS.md](SCENARIOS.md#enlaces-paypal-reconciliación-sin-webhooks).

## Stripe webhooks

The Stripe mock persists payments, managed webhook endpoints and delivery attempts
in `data/stripe.sqlite3`. It signs each delivery with the destination's own secret,
as described in [Stripe's webhook documentation](https://docs.stripe.com/webhooks).

Two listener modes can be used together:

- Managed endpoints registered through `/v1/webhook_endpoints` receive their enabled
  event types, signed with the secret returned when the endpoint was created.
- The environment listener receives events at `STRIPE_MOCK_WEBHOOK_URL` (default:
  `http://localhost:8001/api/stripe/webhooks/notifications/`), signed with
  `STRIPE_MOCK_WEBHOOK_SECRET` (default: `whsec_mock_crinnopayments`). Configure the
  same secret in CrinnoPayments' Stripe configuration when using this listener.

A registered endpoint does not replace the environment listener. Even if both use
one URL, different secrets require separate deliveries so configurations trusting
either secret can process their own orders. An identical URL/secret pair receives
only one delivery per event. Set `STRIPE_MOCK_WEBHOOK_URL` to an empty string to use
managed endpoints only. Export these variables in the server environment before
starting the mock; restart the process if it is not running with `--reload`.

Inspect `/mock-stripe/webhook-deliveries` locally to check delivery attempts.
An HTTP 200 only confirms receipt: CrinnoPayments validates and processes the event
asynchronously. Check its Stripe webhook event/error records if an order remains
pending. For an existing mock payment, send
`POST /mock-stripe/payment-intents/{payment_intent_id}/webhook` with an empty JSON
object to trigger a fresh notification after correcting the listener configuration;
this does not create or capture a payment. Confirm the stored PaymentIntent status
before resending. Manual capture remains `requires_capture` until explicitly captured.

Regression checks: `uv run pytest -q tests/test_stripe_webhooks.py`. All callbacks
are intercepted and Stripe storage uses a temporary database.

## CodeGraph

CodeGraph provides local symbol and relationship queries for this multi-provider
mock. It is an optional external developer tool, independent of Python dependencies.
Use the team's `@colbymchenry/codegraph` distribution (validated here with CLI 1.6.2);
do not install a different package with the same name. The CLI and its runtime must
be visible on PATH in the terminal and agent client process.

From this checkout:

```sh
codegraph status .
uv run python -m tools.codegraph_hook --strict
codegraph query stripe_simulate_checkout --json
codegraph node stripe_simulate_checkout --file stripe/stripe.py
codegraph explore "Stripe checkout webhook delivery"
codegraph sync .
```

The standard-library-only helper initializes this project's index if absent and
otherwise synchronizes it. It resolves the project from its own location, even
when invoked from another working directory. Pass
`--executable /absolute/path/to/codegraph` for an installation outside PATH (its
runtime still needs PATH). `--strict` returns a failure status when CodeGraph is
missing or fails; without it, updates warn without blocking local workflows.
No Git hooks are installed automatically.

The index and all runtime files live in the ignored `.codegraph/` directory.
Every checkout owns its own index: never copy or share CrinnoPayments' index.
Check freshness before analysis and synchronize after source changes.

[.mcp.json](.mcp.json) supplies a project-scoped stdio server for clients supporting
that format. The MCP server watches source changes while it runs. Clients using
another MCP configuration format need their own adapter; the CLI remains usable
through [AGENTS.md](AGENTS.md). Restart/reload the client's MCP connection after
registering it; an index on disk does not imply an active MCP connection.

## Noon

The local Noon simulator supports CrinnoPayments hosted SALE/AUTHORIZE checkout, full capture, unpaid cancellation,
reconciliation by ID/reference, native expiry and signed v2 callbacks. Open **http://localhost:7000/mock-noon**.
Orders, scenarios and event snapshots persist independently in `data/noon.sqlite3`.

Use CrinnoPayments' development settings for HTTP API, callback and checkout URLs. HTTPS remains required outside
that environment. Fake credentials, configuration, failure scenarios and examples are in [noon/README.md](noon/README.md).
