from __future__ import annotations

import json
import re
from datetime import datetime

from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app import app

from .encript import encrypt


class CardDataTokenRequest(BaseModel):
    encryption_key: str = Field(
        ...,
        description="Clave Fernet activa del proyecto que recibirá card_data.",
    )
    cvn: str = Field(default="123", min_length=3, max_length=4)
    card_number: str = Field(default="4111111111111111")
    exp_date: str = Field(default="08/32")
    name_on_card: str = Field(default="Crinno Payment Test", min_length=1)
    zip_code: str = Field(default="12345")


class CardDataTokenResponse(BaseModel):
    card_data: str


def _normalized_card_payload(request: CardDataTokenRequest) -> dict[str, str]:
    card_number = re.sub(r"[\s-]", "", request.card_number)
    cvn = request.cvn.strip()
    exp_date = request.exp_date.strip()
    exp_digits = re.sub(r"[\s/-]", "", exp_date)

    if not card_number.isdigit() or not 13 <= len(card_number) <= 19:
        raise HTTPException(
            status_code=422,
            detail="card_number debe contener entre 13 y 19 dígitos.",
        )
    if not cvn.isdigit() or len(cvn) not in {3, 4}:
        raise HTTPException(
            status_code=422,
            detail="cvn debe contener 3 o 4 dígitos.",
        )
    if len(exp_digits) != 4 or not exp_digits.isdigit():
        raise HTTPException(
            status_code=422,
            detail="exp_date debe tener formato MM/YY, por ejemplo 08/32.",
        )

    month = int(exp_digits[:2])
    year = int(exp_digits[2:])
    now = datetime.now()
    if month not in range(1, 13):
        raise HTTPException(status_code=422, detail="El mes de exp_date no es válido.")
    if (year, month) < (now.year % 100, now.month):
        raise HTTPException(status_code=422, detail="La tarjeta está vencida.")

    name_on_card = request.name_on_card.strip()
    if not name_on_card:
        raise HTTPException(status_code=422, detail="name_on_card es obligatorio.")

    return {
        "cvn": cvn,
        "card_number": card_number,
        # Se conserva MM/YY: el servicio BMS ya elimina '/' antes de enviarlo.
        "exp_date": f"{month:02d}/{year:02d}",
        "name_on_card": name_on_card,
        "zip_code": request.zip_code.strip(),
    }


def _generate_card_data(request: CardDataTokenRequest) -> str:
    payload = _normalized_card_payload(request)
    encryption_key = request.encryption_key.strip()

    try:
        # Valida explícitamente la clave para devolver un error comprensible.
        Fernet(encryption_key.encode("utf-8"))
        return encrypt(json.dumps(payload), encryption_key=encryption_key)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "encryption_key no es una clave Fernet válida. Usa el valor de "
                "EncryptionKey activo en el proyecto que recibirá la petición."
            ),
        ) from exc


@app.post(
    "/bms/card-data/generate",
    response_model=CardDataTokenResponse,
    summary="Generar card_data cifrado para pruebas BMSPay",
    tags=["BMS testing"],
)
async def generate_bms_card_data(request: CardDataTokenRequest) -> CardDataTokenResponse:
    return CardDataTokenResponse(card_data=_generate_card_data(request))


@app.get(
    "/bms/card-data",
    response_class=HTMLResponse,
    summary="Formulario para generar card_data de BMSPay",
    tags=["BMS testing"],
)
async def bms_card_data_form() -> HTMLResponse:
    return HTMLResponse(
        """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Generar card_data de BMSPay</title>
  <style>
    body { font-family: Arial, sans-serif; background: #f5f7fa; margin: 0; padding: 32px; color: #1f2937; }
    main { max-width: 760px; margin: auto; background: white; padding: 28px; border-radius: 12px; box-shadow: 0 4px 18px #0001; }
    h1 { margin-top: 0; }
    .hint { color: #52606d; line-height: 1.5; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .wide { grid-column: 1 / -1; }
    label { display: block; font-weight: 700; margin-bottom: 6px; }
    input, textarea { width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; }
    textarea { min-height: 150px; resize: vertical; }
    button { margin-top: 18px; padding: 11px 18px; border: 0; border-radius: 6px; background: #1456d9; color: white; cursor: pointer; }
    button.secondary { background: #475569; margin-left: 8px; }
    #error { color: #b91c1c; margin-top: 16px; white-space: pre-wrap; }
    #resultBox { display: none; margin-top: 24px; }
    @media (max-width: 620px) { .grid { grid-template-columns: 1fr; } .wide { grid-column: auto; } }
  </style>
</head>
<body>
<main>
  <h1>Generar card_data de BMSPay</h1>
  <p class="hint">
    Esta utilidad es solo para pruebas locales. La clave debe ser exactamente la
    <code>EncryptionKey</code> activa del servicio que va a recibir la petición.
  </p>
  <form id="cardForm">
    <div class="grid">
      <div class="wide">
        <label for="encryption_key">Clave Fernet activa</label>
        <input id="encryption_key" name="encryption_key" type="text" autocomplete="off" required>
      </div>
      <div>
        <label for="card_number">Número de tarjeta</label>
        <input id="card_number" name="card_number" value="4111111111111111" required>
      </div>
      <div>
        <label for="name_on_card">Nombre en la tarjeta</label>
        <input id="name_on_card" name="name_on_card" value="Crinno Payment Test" required>
      </div>
      <div>
        <label for="exp_date">Vencimiento (MM/YY)</label>
        <input id="exp_date" name="exp_date" value="08/32" required>
      </div>
      <div>
        <label for="cvn">CVN</label>
        <input id="cvn" name="cvn" value="123" required>
      </div>
      <div>
        <label for="zip_code">Código postal</label>
        <input id="zip_code" name="zip_code" value="12345">
      </div>
    </div>
    <button type="submit">Generar card_data</button>
  </form>
  <div id="error"></div>
  <section id="resultBox">
    <label for="result">Valor listo para payment_extra_data.card_data</label>
    <textarea id="result" readonly></textarea>
    <button class="secondary" type="button" id="copyButton">Copiar</button>
  </section>
</main>
<script>
  const form = document.getElementById("cardForm");
  const errorBox = document.getElementById("error");
  const resultBox = document.getElementById("resultBox");
  const result = document.getElementById("result");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.textContent = "";
    resultBox.style.display = "none";

    const payload = Object.fromEntries(new FormData(form).entries());
    const response = await fetch("/bms/card-data/generate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      errorBox.textContent = typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail, null, 2);
      return;
    }
    result.value = data.card_data;
    resultBox.style.display = "block";
  });

  document.getElementById("copyButton").addEventListener("click", async () => {
    await navigator.clipboard.writeText(result.value);
  });
</script>
</body>
</html>
        """
    )
