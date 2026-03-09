
from fastapi import FastAPI, Request, HTTPException, Header
from pydantic import BaseModel, Field
from typing import Optional
from typing import List, Dict, Any
import os
from dotenv import load_dotenv
import uuid

import json
import re
# from datetime import datetime
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path
from fastapi.templating import Jinja2Templates
import httpx
from typing import List, Literal, Optional
from app import app

@app.api_route("/webhooks/events/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def square_webhook_handler(request: Request):
    from datetime import datetime
    # Generar un nombre único para el archivo de log
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    unique_suffix = str(uuid.uuid4())[:8]
    # log_filename = LOGS_DIR / f"registro_llamada_interna_{timestamp}_{unique_suffix}.txt"

    # Capturar datos
    url = str(request.url)
    method = request.method
    headers = dict(request.headers)
    
    try:
        body = await request.body()
        if body:
            try:
                json_body = json.loads(body.decode("utf-8"))
                payload_str = json.dumps(json_body, indent=2, ensure_ascii=False)
                payload_type = "JSON"
            except json.JSONDecodeError:
                payload_str = body.decode("utf-8", errors="replace")
                payload_type = "raw"
        else:
            payload_str = "(vacío)"
            payload_type = "none"
    except Exception as e:
        payload_str = f"(error al leer cuerpo: {e})"
        payload_type = "error"

    # Formato del mensaje a imprimir y guardar
    log_lines = [
        f"Fecha y hora (UTC): {datetime.utcnow().isoformat()}",
        f"URL: {url}",
        f"Método: {method}",
        f"Headers:",
        json.dumps(headers, indent=2, ensure_ascii=False),
        f"Tipo de payload: {payload_type}",
        f"Payload:",
        payload_str,
        "\n" + "="*80 + "\n"
    ]

    full_log = "\n".join(log_lines)

    # Imprimir en consola
    print(full_log)

    # Guardar en archivo
    # try:
    #     with open(log_filename, "w", encoding="utf-8") as f:
    #         f.write(full_log)
    # except Exception as e:
    #     print(f"[ERROR AL GUARDAR LOG]: {e}")

    # Siempre responder con 200 OK
    return JSONResponse(status_code=200, content={"status": "ok"})
