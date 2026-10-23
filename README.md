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
