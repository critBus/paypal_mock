# Mock local de Axxipay

## Instalación

1. Copia la carpeta `axxipay` en la raíz del mock FastAPI, junto a `app.py` y `main.py`.
2. Agrega esta línea a `main.py`, junto a los imports de los demás mocks:

   ```python
   from axxipay.axxipay import *  # noqa: F403
   ```

3. Configura las variables de entorno:

   ```dotenv
   AXXIPAY_MERCHANT_PASSWORD=secret-password-strong
   AXXIPAY_WEBHOOK_TARGET_URL=http://localhost:8001/api/axxipay/webhooks/notifications/

   # Opcionales:
   AXXIPAY_MERCHANT_KEY=local-merchant-key
   AXXIPAY_VALIDATE_REQUEST_HASH=false
   ```

4. En `AxxipayConfiguration` de Django usa:

   - `checkout_api_url`: `http://localhost:8000`
   - `merchant_password`: el mismo valor de `AXXIPAY_MERCHANT_PASSWORD`
   - `merchant_key`: el mismo valor de `AXXIPAY_MERCHANT_KEY` si configuraste esta variable

El `merchant_password` debe coincidir para que Django marque el callback con
`signature_verified=True`. Si activas `AXXIPAY_VALIDATE_REQUEST_HASH`, el mock también
validará la firma de la solicitud inicial.

## Flujo

La integración Django llama a `POST /api/v1/session`. El mock devuelve un
`redirect_url` hacia un checkout local, donde puedes simular:

- pago exitoso: `status=success`, `order_status=settled`;
- pago rechazado: `status=fail`, `order_status=decline`;
- cancelación: `status=fail`, `order_status=void`.

El callback se envía a `AXXIPAY_WEBHOOK_TARGET_URL` con la firma MD5/SHA1 que espera
`CallbackHashGenerator`. Después, el navegador redirige al `success_url` o
`cancel_url` recibido en la creación de la sesión.

## Endpoints de control

- `GET /mock-axxipay/payments`: pagos guardados en memoria.
- `GET /mock-axxipay/payments/{payment_id}`: detalle de un pago.
- `POST /mock-axxipay/payments/{payment_id}/webhook`: reenvía un callback. Cuerpo:

  ```json
  {
    "outcome": "success",
    "target_url": "http://localhost:8001/api/axxipay/webhooks/notifications/"
  }
  ```

- `GET /mock-axxipay/webhook-deliveries`: historial de intentos de callback.
- `POST /mock-axxipay/reset`: limpia el estado del mock.

Los valores válidos de `outcome` son `success`, `failure`, `declined` y `cancelled`.
