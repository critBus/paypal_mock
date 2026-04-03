from fastapi import FastAPI, Request, HTTPException, Header
from pydantic import BaseModel, Field
from typing import Optional
from typing import List, Dict, Any
import os
from dotenv import load_dotenv
import uuid

import json
import re
from datetime import datetime
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path
from fastapi.templating import Jinja2Templates
import httpx

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from config import settings
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Mock Paypal Payment Links API",
    description="Mini API de prueba para simular la creación de payment links de Paypal"
)

# CORS para permitir acceso desde el frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Montar archivos estáticos (CSS local)
app.mount("/static", StaticFiles(directory=str(settings.STATIC_DIR)), name="static")

# Configurar templates
templates = Jinja2Templates(directory=str(settings.TEMPLATES_DIR))