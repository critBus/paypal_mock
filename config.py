from pydantic_settings import BaseSettings
from typing import Optional
from pathlib import Path

class Settings(BaseSettings):
    # Django API Base URL
    DJANGO_API_URL: str = "http://localhost:8001"
    
    # Redsys Testing Config (valores por defecto de testing Redsys)
    REDSYS_TEST_MERCHANT_KEY: str = "asdasasd"
    REDSYS_TEST_MERCHANT_CODE: str = "123213213"
    REDSYS_TEST_TERMINAL: str = "002"
    
    # App Key para autenticación con Django API
    REDSYS_APP_KEY: Optional[str] = None
    
    # Paths
    BASE_DIR: Path = Path(__file__).parent
    STATIC_DIR: Path = BASE_DIR / "static"
    TEMPLATES_DIR: Path = BASE_DIR / "templates"
    
    class Config:
        env_file = ".env"

settings = Settings()