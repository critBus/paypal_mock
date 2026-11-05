# Noon local simulator

## Scope and implementation plan

This provider simulates the subset consumed by CrinnoPayments: Key authorization; INITIATE, GET by ID/reference,
CAPTURE and pre-authorization CANCEL; hosted SALE/AUTHORIZE checkout; native initiation expiry; signed v2 receipts;
explicit reconciliation, replay and failure scenarios. It does not implement real card processing, settlement,
refunds, authorization reversal, subscriptions or marketplace splits. Other providers remain unchanged.

Use provider-owned SQLite storage with atomic mutations, committed state before callbacks, unique merchant references,
capture-reference deduplication and retained event snapshots. Callbacks are intercepted in automated tests and every
Noon test uses temporary storage. Never reset existing data or call a real provider to verify this module.

Provide a local dashboard/checkout and deterministic controls for pending/failed/cancelled/authorized/captured states,
unknown outcomes, response corruption and webhook errors. Use the existing HTTP listener for both the provider API and local checkout. CrinnoPayments allows HTTP only
with its development settings; production/staging still require HTTPS. An HTTPS tunnel can be configured if needed.

Verification: protocol/precision/auth/state/idempotency/expiry/persistence tests, intercepted JWS deliveries,
CrinnoPayments gateway/service integration against the local HTTP mock, existing provider regressions and browser
checkout/control workflow. Runtime local checks use isolated SQLite and a local callback collector only.

Official references (checked 2026-11-04):
[INITIATE](https://docs.noonpayments.com/payment-api/reference/initiate),
[GET by reference](https://docs.noonpayments.com/payment-api/reference/get-order-by-reference),
[CAPTURE](https://docs.noonpayments.com/payment-api/reference/capture),
[Webhook v2](https://docs.noonpayments.com/webhook/v2).
The mock's JWS envelope is matched to CrinnoPayments' current Noon verifier; real account/sandbox certification remains
necessary. `pay` is a configured mock route, not proof that a real Noon account supports that category.

## Run and configure CrinnoPayments

From this mock checkout:

```sh
uv run uvicorn main:app --reload --host 127.0.0.1 --port 7000
```

In CrinnoPayments use `config.settings.development`. Its `ENVIRONMENT="development"` explicitly permits HTTP API,
callback and hosted checkout URLs; production/staging require HTTPS even when DEBUG is true.

Create/enable a **local sandbox** Noon configuration owned by the intended enabled/approved AppClient:

| Configuration field | Local mock value |
| --- | --- |
| merchant_api_url | `http://localhost:7000` |
| business_identifier | `mock-business` |
| application_identifier | `mock-app` |
| application_key | `mock-application-key` (synthetic credential) |
| webhook_secret | `mock-noon-webhook-secret` (synthetic credential) |
| webhook_key_id | `mock-noon-key` |
| category | `pay` (configured by this mock only) |
| notification_url | `http://localhost:8001/api/noon/webhooks/notifications/` |
| redirect_url | Your local consuming application's return URL |

Create the payment through CrinnoPayments as usual; use `notify_customer:false` for synthetic customer records. Keep
the Huey consumer running: HTTP 200 from the callback means receipt, not successful asynchronous reconciliation.
Webhooks Center/legacy event publication still follows the normal CrinnoPayments flow and needs its local setup.
Noon credentials in this table are deliberately fake and should not be used with the real provider.

Open **http://localhost:7000/mock-noon** for controls, or open the checkout_url returned by CrinnoPayments:

- SALE: successful checkout becomes CAPTURED; exact totalCapturedAmount equals the order amount.
- AUTHORIZE: successful checkout becomes AUTHORIZED with no captured funds. Capture through CrinnoPayments' API/admin;
  a browser success/redirect does not capture it. Repeated capture does not create another financial transition.
- Cancel only unpaid/pre-authorization links through CrinnoPayments. Authorized/captured/unknown outcomes reject CANCEL.
- initiationValidity expires an unpaid order when it is next read/opened; an expired link cannot pay. No background
  expiry daemon is simulated. Opening an already authorized/captured checkout does not pay again.
- Pending capture can be resolved to CAPTURED in the control panel without another CAPTURE POST. Deliberately changing
  a terminal state requires the explicit late-state checkbox; this is a test control, not a real provider operation.

SQLite persists in `data/noon.sqlite3`. Restarting preserves orders, token URLs, control settings, event snapshots,
capture reservations and delivery attempts. Imports do not create files. No dependency was added.

## Optional environment overrides

Export variables **before starting/restarting the mock** (the existing Settings class does not automatically export
arbitrary `.env` entries for provider modules):

| Variable | Default / purpose |
| --- | --- |
| NOON_MOCK_CHECKOUT_BASE_URL | `http://localhost:7000`; change for another port, Docker/LAN or HTTPS tunnel |
| NOON_MOCK_DB_PATH | `data/noon.sqlite3`, resolved from project directory |
| NOON_MOCK_BUSINESS_IDENTIFIER | `mock-business` |
| NOON_MOCK_APPLICATION_IDENTIFIER | `mock-app` |
| NOON_MOCK_APPLICATION_KEY | `mock-application-key` |
| NOON_MOCK_WEBHOOK_SECRET | `mock-noon-webhook-secret` |
| NOON_MOCK_WEBHOOK_KEY_ID | `mock-noon-key` |
| NOON_MOCK_CATEGORIES | `pay`; comma-separated mock routes |
| NOON_MOCK_WEBHOOK_URL | Unset: use the exact per-order configuration.webhookUrl; empty: disable delivery |

For a custom port, for example:

```sh
export NOON_MOCK_CHECKOUT_BASE_URL=http://localhost:7017
uv run uvicorn main:app --reload --host 127.0.0.1 --port 7017
```

Configure `merchant_api_url` to the same HTTP origin. If using containers, localhost refers to that container;
use a reachable host name in each URL. No production HTTPS or signature validation should be disabled.

## Scenarios and recovery

The panel applies controls to **future orders** or a selected existing order. Controls replace only that target's
settings; applying them does not itself change a payment or send a callback. `normal` clears faults for that target.

Available presets: normal, no_webhook, duplicate_webhook, invalid_signature, wrong_kid, wrong_algorithm,
malformed_webhook, capture_pending, capture_declined, capture_lost_response, initiate_lost_response, missing_checkout,
get_unavailable, invalid_json, wrong_amount, wrong_currency, wrong_id, wrong_reference, captured_amount_mismatch.

For a lost INITIATE response, CrinnoPayments recovers through GET by merchant reference; it must not create again.
For capture_lost_response, the server commits CAPTURED but returns HTTP 503 and suppresses the webhook. Sync/read the
original payment to recover; never blindly submit another capture. A definitive INITIATE api_error creates no order.
Wrong response fields do not corrupt stored truth: remove/consume the fault and a new GET returns the real order.

Advanced controls through `POST /mock-noon/controls` or `/docs`:

```json
{
  "order_id": "YOUR_NOON_ORDER_ID",
  "scenario": "normal",
  "faults": {
    "CAPTURE": {"kind": "delay", "delay_seconds": 65, "times": 1}
  },
  "webhook_mode": "suppress"
}
```

This commits capture but delays its response beyond CrinnoPayments' 60-second read timeout. Reconcile after the server
finishes. Other fault kinds: api_error, invalid_json, lost_response, wrong_amount, wrong_currency, wrong_id,
wrong_reference, missing_order, missing_checkout and captured_amount_mismatch. Select INITIATE, GET, GET_BY_REFERENCE,
CAPTURE or CANCEL. `times:1` is consumed per call for an existing order; `times:-1` persists until controls are reset.
Future-order defaults seed independent per-order counters. A rejected INITIATE has no order to attach a counter to,
so future defaults continue applying until reset. api_error also accepts result_code and http_status.

`POST /mock-noon/orders/{id}/simulate` accepts outcome (success or an explicit remote state), notify (default true)
and force (default false). Real API CAPTURE/CANCEL enforce their normal constraints regardless of control-panel force.

`POST /mock-noon/orders/{id}/replay` accepts an optional event_id and mode. It reuses the original event ID/payload,
including delayed AUTHORIZED snapshots after CAPTURED, without submitting another payment. The panel's replay button
explicitly selects normal delivery so a previously suppressed/bad-signature event can be redelivered correctly.
Only the latest event is chosen when no ID is given. mode can also be suppress, duplicate, bad_signature, wrong_kid,
wrong_alg or malformed. Duplicate deliveries share the same event ID/JWS body. Webhooks use JSON `{ "data": "JWS" }`
with HS256, kid and iat, matching the current CrinnoPayments verifier.

Inspect `/mock-noon/webhook-deliveries` for event IDs, HTTP acknowledgement, suppression and connection errors.
Responses/secrets are not logged. Failed deliveries remain visible and events can be replayed manually; the mock does
not reproduce Noon's full production retry timetable. Polling/reconciliation remains necessary for suppressed receipts.

## Verification

```sh
uv run pytest -q tests/test_noon.py
uv run ruff check noon tests/test_noon.py tests/conftest.py
```

All callbacks are intercepted; every Noon store uses a temporary database. Coverage includes exact three-decimal
amounts, authentication/category errors, native expiry, pending/rejected states, full capture, concurrent/repeated
capture, cancellation boundaries, unknown outcomes, response corruption, original-event replay, signature failures
and restart persistence. Tests never reset the running mock's data or contact CrinnoPayments/provider endpoints.

Completed verification (2026-11-04):

- All **193 tests** in this mock's `tests/` suite passed, including 53 Noon cases and existing provider regressions.
  Existing config/BMS deprecation warnings remain. Ruff checks/format and diff whitespace checks passed for new code;
  unrelated legacy files were not reformatted.
- Six local HTTP integration cases ran the real CrinnoPayments gateway/services and v2 verifier against an isolated
  mock process, including both actual outgoing event models with queue transport isolated. They covered sale,
  authorization/full capture, unpaid cancellation, lost initiation/capture responses and invalid signatures.
- Chrome DevTools MCP exercised scenario selection/persistence, SALE checkout becoming CAPTURED, AUTHORIZE checkout
  stopping at AUTHORIZED, disabled repeated payment and the return link. Screens used synthetic orders and an isolated
  SQLite file; callbacks went only to an owned local collector. CrinnoPayments' admin saved/reloaded HTTP API/callback
  URLs in development. Production/staging rejection and DEBUG independence passed backend tests.
- No real provider account, remote database or existing mock data was used. This verifies the current consumer
  contract, not real Noon sandbox certification or the provider's complete banking/routing/retry behavior.
