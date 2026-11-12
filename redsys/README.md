# Redsys local mock

The existing `/redsys` manual HTML-form/webhook simulator remains available. Follow its **PayGold hosted links**
link or open `/mock/redsys/paygold` for persisted hosted links, scenarios and callback delivery statuses.

## Run and configure

Start the shared mock normally:

```sh
uv run uvicorn main:app --reload --port 7000
```

Configure CrinnoPayments' Redsys account with:

| Field | Local value |
| --- | --- |
| Scope | `sandbox` |
| Merchant API URL | `http://localhost:7000/sis/realizarPago` |
| REST API base URL | `http://localhost:7000` (code adds the REST path) |
| Notification URL | `http://localhost:8001/api/redsys/webhooks/notifications/` or your full active tunnel URL |
| Merchant code / terminal | Any nine-digit mock merchant code and terminal 1–999; use the same configuration for the request |
| Currency | `USD` / `840` or `EUR` / `978` |
| Merchant key | The same **SHA-256 signing key** as `REDSYS_TEST_MERCHANT_KEY` in the mock environment |
| Use PayGold links | Enabled for native hosted links; disabled for the existing HTML form |
| Success / cancel URLs | Your consumer's complete URLs, including their distinct query parameters |
| Custom expiration | Enable and choose a duration in the existing CrinnoPayments widget, or disable for 24 hours |

Use development settings in CrinnoPayments for local HTTP. No ngrok is needed when both local processes can reach
one another. Configure a reachable hostname when the services run in separate containers/machines.

The default signing key is Redsys' published sandbox SHA-256 fixture. An existing `.env` overrides this default:
replace an old placeholder/API key with the sandbox **signing key**, and match it in CrinnoPayments. Do not use an
API Key, a SHA-512 key or live credentials. Only `HMAC_SHA256_V1` is implemented. `REDSYS_MOCK_MERCHANT_KEY` optionally
overrides the key for REST/form checkout, signed responses and its callbacks; the old manual simulator continues
using `REDSYS_TEST_MERCHANT_KEY`.

Set `REDSYS_MOCK_PUBLIC_URL` to the mock's full reachable origin when request origins are internal to a proxy.
Otherwise returned links use the request origin. `REDSYS_MOCK_DB_PATH` overrides lazy SQLite storage at
`data/redsys.sqlite3`; links, scenarios, last callback snapshots and delivery results survive reload/restart.
Do not remove this file to run tests: tests use independent temporary storage and intercepted HTTP.

## Protocol and state

- POST `/sis/rest/trataPeticionREST` accepts the signed Redsys envelope. It validates the signature before mutation.
  Operation `F` returns signed `Ds_Response=9998` and `Ds_UrlPago2Fases`. Creation is pending, never paid.
- `DS_MERCHANT_P2F_EXPIRYDATE` accepts positive relative minutes or `yyyy-mm-dd-HH.MM.ss.SSS`; omission means 24 hours.
  This mock interprets absolute dates as UTC. Provider timezone behavior must still be verified against Redsys.
- Operation `51` identifies the same merchant/terminal/native order and returns a signed `0000` on expiration change.
  A past deadline deactivates the unpaid link. A paid link cannot be deactivated; this is not a refund.
- POST `/sis/realizarPago` accepts the legacy signed form (`0`) and opens the same local checkout. The original
  manual form parser/webhook endpoints remain available. Repeated merchant/terminal/order creation is rejected;
  it does not silently create or replace another link.
- GET `/sis/p2f?t=...` opens checkout. Select paid, declined, cancelled or pending plus mock card brand/last four.
  Only simulated successful authorization (`0000`–`0099`) records `paid`. Declines/pending can be followed by a retry;
  paid, customer-cancelled, deactivated and expired links reject further checkout results. Refreshing checkout sends
  no callback. Closing it sends no cancellation event.
- A result persists before callback delivery. The callback is URL-encoded POST to the **exact saved MerchantURL**,
  preserving tunnel origin, path and query. PayGold uses native order and operation `F` without adding `con`; legacy
  callbacks preserve the original form callback including its `con` hint. Amount, currency, account and terminal
  match the original request. Card data is synthetic/masked; never enter real card information here.
- The checkout exposes a **Return to merchant** link using the saved URLOK/URLKO unchanged. The tester follows it
  explicitly after simulating the outcome. Browser navigation never establishes trusted payment in CrinnoPayments.
  This intentionally simulates the accepted return-URL assumption; it does not prove real PayGold retains those URLs.

## Controls and diagnostics

GET `/mock/redsys/paygold/links` returns link snapshots and future-link defaults. POST `/mock/redsys/paygold/controls`
sets persistent defaults for **new links**, or accepts `token` to change an existing link's future behavior. Controls
remain set until reset; they are not consumed once. Changing controls does not notify the consumer.

```json
{
  "creation_error": "SIS0487",
  "creation_response": "normal",
  "expiration_error": "",
  "callback_mode": "normal"
}
```

- `creation_error`: empty for normal or an explicit `SISdddd` processing error (for example missing PayGold `SIS0487`).
- `creation_response`: `normal`, `bad_signature`, `http503_after_create`, `malformed_after_create`. The last three
  persist the provider-side link so the consumer can observe unknown creation outcomes without blindly recreating it.
- `expiration_error`: empty or `SISdddd`, returned when operation 51 targets that link.
- `callback_mode`: `normal`, `omitted`, `invalid_signature`, `duplicate`. Duplicate sends the identical signed event.

POST `/mock/redsys/paygold/links/{token}/simulate` accepts `response_code` (four digits), `card_brand` (`1`, `2`, `8`)
and `last_four` (four digits). POST `/mock/redsys/paygold/links/{token}/replay` resends the **last stored event** using
the link's current callback mode without another payment. Set callback mode to `normal` before recovering an omitted
or invalid delivery. Delivery history records HTTP statuses or a generic transport error; no response bodies or
exception strings containing credentials are stored. HTTP acceptance does not prove financial processing by Django.
Reset new-link controls with `{}` and reset a link with `{"token":"LINK_TOKEN"}`.

The mock intentionally accepts valid-shaped merchant/terminal/currency combinations without real-provider provisioning.
It does not validate USD availability, PayGold terminal enablement, provider fraud rules or real card authentication.
It does not implement preauthorization/capture, refunds, provider reconciliation or HMAC_SHA512_V2.

Checks: `uv run pytest -q tests/test_redsys_paygold.py`.
