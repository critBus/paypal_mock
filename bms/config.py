from pydantic import BaseModel
from typing import Optional

class BMSConfig(BaseModel):
    """Configuración válida para el mock"""
    cid: str = "260"
    mid: str = "76074"
    app_key: str = "12345"
    username: str = "nicolas"
    password: str = "password1"
    is_test: bool = True
    
    # URLs
    base_url: str = "http://localhost:9000"#"https://services.bmspay.com"
    
    # Respuestas por defecto
    default_authorization_number: str = "631448"
    default_service_reference: str = "1P5JG9W4PNV8U1DDVT"

# Configuración global
BMS_CONFIG = BMSConfig()