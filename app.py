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

app = FastAPI(
    title="Mock Paypal Payment Links API",
    description="Mini API de prueba para simular la creación de payment links de Paypal"
)