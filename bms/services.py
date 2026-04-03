import jwt
import secrets
import string
from datetime import datetime, timedelta
from typing import Tuple, Optional
from .config import BMS_CONFIG
from .models import (
    TokenThreeDSRequest,
    TransactionSaleRequest,
    TokenThreeDSResponse,
    TransactionSaleResponse,
    PaymentPlanInfo,
    ErrorResponse
)

class BMSPayMockService:
    """Servicio mock que simula el comportamiento de BMSPay"""
    
    def __init__(self):
        self.transactions = {}
        self.tokens = {}
    
    def _generate_token(self, api_key: str) -> str:
        """Genera un JWT token mock"""
        payload = {
            "iss": "3dsintegrator_Authentication_Server",
            "aud": api_key,
            "exp": int((datetime.utcnow() + timedelta(hours=1)).timestamp())
        }
        token = jwt.encode(payload, "mock_secret_key", algorithm="HS256")
        return token
    
    def _generate_authorization_number(self) -> str:
        """Genera número de autorización aleatorio"""
        return ''.join(secrets.choice(string.digits) for _ in range(6))
    
    def _generate_service_reference(self) -> str:
        """Genera referencia de servicio única"""
        chars = string.ascii_uppercase + string.digits
        return ''.join(secrets.choice(chars) for _ in range(20))
    
    def _validate_credentials(self, request: TokenThreeDSRequest) -> Tuple[bool, Optional[str]]:
        """Valida credenciales del merchant"""
        if request.cid != BMS_CONFIG.cid:
            return False, "INVALID CREDENTIALS"
        if request.mid != BMS_CONFIG.mid:
            return False, "INVALID HOST CREDENTIALS"
        if request.AppKey != BMS_CONFIG.app_key:
            return False, "INVALID CREDENTIALS"
        if request.UserName != BMS_CONFIG.username:
            return False, "INVALID CREDENTIALS"
        if request.Password != BMS_CONFIG.password:
            return False, "INVALID CREDENTIALS"
        return True, None
    
    def _validate_card_data(self, request: TransactionSaleRequest) -> Tuple[bool, Optional[str]]:
        """Valida datos de tarjeta"""
        # Validar longitud de tarjeta (mínimo 13 dígitos)
        card_number = request.CardNumber.replace("X", "4")  # Para testing con datos enmascarados
        if len(card_number.replace("-", "").replace(" ", "")) < 13:
            return False, "TRANSACTION ERROR. INVALID PARAMETER"
        
        # Validar fecha de expiración
        exp_date = request.ExpDate.replace("X", "12")  # Para testing
        if len(exp_date) >= 4:
            try:
                month = int(exp_date[:2])
                year = int(exp_date[2:4])
                if month < 1 or month > 12:
                    return False, "TRANSACTION ERROR. INVALID EXP DATE (USE MMYY)"
                # Validar que no esté expirada
                current_year = int(datetime.now().strftime("%y"))
                current_month = int(datetime.now().strftime("%m"))
                if year < current_year or (year == current_year and month < current_month):
                    return False, "TRANSACTION ERROR. EXPIRATION DATE MUST BE IN FUTURE"
            except ValueError:
                return False, "TRANSACTION ERROR. INVALID EXP DATE (USE MMYY)"
        
        # Validar CVN
        if request.CVN == "XXX" or len(request.CVN) < 3:
            return False, "TRANSACTION ERROR. BAD CID"
        
        return True, None
    
    def authenticate_threeds(self, request: TokenThreeDSRequest) -> Tuple[TokenThreeDSResponse, int]:
        """Autentica y genera token 3DS"""
        is_valid, error_msg = self._validate_credentials(request)
        
        if not is_valid:
            response = TokenThreeDSResponse(
                Msg=[error_msg],
                Token="",
                ApiKey="",
                ResponseCode=401,
                verbiage="ERROR"
            )
            return response, 401
        
        # Generar API Key y Token
        api_key = ''.join(secrets.choice(string.hexdigits.lower()) for _ in range(32))
        token = self._generate_token(api_key)
        
        # Guardar token para validación posterior
        self.tokens[api_key] = {
            "token": token,
            "expires": datetime.utcnow() + timedelta(hours=1)
        }
        
        response = TokenThreeDSResponse(
            Msg=["Operation Successful"],
            Token=token,
            ApiKey=api_key,
            ResponseCode=200
        )
        return response, 200
    
    def process_sale(self, request: TransactionSaleRequest) -> Tuple[TransactionSaleResponse, int]:
        """Procesa una transacción de venta"""
        # Validar credenciales primero
        is_valid, error_msg = self._validate_credentials(request)
        if not is_valid:
            response = TransactionSaleResponse(
                Msg=[error_msg],
                verbiage="ERROR",
                ResponseCode=401,
                msoft_code="INT_ERROR",
                phard_code="ERROR",
                LastFour=request.CardNumber[-4:] if len(request.CardNumber) >= 4 else "0000",
                AuthorizationNumber="",
                ServiceReferenceNumber="",
                PaymentPlanInfo=PaymentPlanInfo()
            )
            return response, 401
        
        # Validar datos de tarjeta
        is_valid, error_msg = self._validate_card_data(request)
        if not is_valid:
            response = TransactionSaleResponse(
                Msg=[error_msg],
                verbiage="ERROR",
                ResponseCode=400,
                msoft_code="INT_ERROR",
                phard_code="ERROR",
                LastFour=request.CardNumber[-4:] if len(request.CardNumber) >= 4 else "0000",
                AuthorizationNumber="",
                ServiceReferenceNumber="",
                PaymentPlanInfo=PaymentPlanInfo()
            )
            return response, 400
        
        # Validar monto
        try:
            amount = float(request.Amount)
            if amount <= 0:
                raise ValueError("Amount must be positive")
        except (ValueError, TypeError):
            response = TransactionSaleResponse(
                Msg=["INCORRECT AMOUNT"],
                verbiage="ERROR",
                ResponseCode=400,
                msoft_code="INT_ERROR",
                phard_code="ERROR",
                LastFour=request.CardNumber[-4:] if len(request.CardNumber) >= 4 else "0000",
                AuthorizationNumber="",
                ServiceReferenceNumber="",
                PaymentPlanInfo=PaymentPlanInfo()
            )
            return response, 400
        
        # Generar respuesta exitosa
        auth_number = self._generate_authorization_number()
        service_ref = self._generate_service_reference()
        last_four = request.CardNumber[-4:] if len(request.CardNumber) >= 4 else "0000"
        
        # Determinar tipo de tarjeta basado en el número
        card_type = "VISA"
        if request.CardNumber.startswith("4"):
            card_type = "VISA"
        elif request.CardNumber.startswith("5"):
            card_type = "MASTERCARD"
        elif request.CardNumber.startswith("3"):
            card_type = "AMEX"
        
        response = TransactionSaleResponse(
            cv="GOOD",
            Msg=["OPERATION SUCCESSFUL"],
            avs="GOOD",
            Token=None,
            Balance=None,
            CardType=card_type,
            LastFour=last_four,
            verbiage="APPROVED",
            CustomerId=0,
            msoft_code="INT_SUCCESS",
            phard_code="SUCCESS",
            ResponseCode=200,
            displayMessage=None,
            PaymentPlanInfo=PaymentPlanInfo(),
            AuthorizationNumber=auth_number,
            ServiceReferenceNumber=service_ref
        )
        
        # Guardar transacción para referencia futura
        self.transactions[service_ref] = {
            "request": request,
            "response": response,
            "timestamp": datetime.utcnow()
        }
        
        return response, 200
    
    def get_transaction(self, service_reference: str) -> Tuple[Optional[TransactionSaleResponse], int]:
        """Recupera una transacción por referencia"""
        if service_reference in self.transactions:
            return self.transactions[service_reference]["response"], 200
        return None, 404
    
    def simulate_failure(self, failure_type: str = "decline") -> Tuple[TransactionSaleResponse, int]:
        """Simula diferentes tipos de fallo para testing"""
        failure_scenarios = {
            "decline": {
                "Msg": ["TRANSACTION ERROR. DECLINE"],
                "verbiage": "DECLINED",
                "ResponseCode": 400,
                "msoft_code": "INT_ERROR",
                "phard_code": "ERROR"
            },
            "invalid_card": {
                "Msg": ["TRANSACTION ERROR. THE MATHEMATICAL CHECK (MOD10/LUHN) ON THE ACCOUNT NUMBER HAS FAILED. POSSIBLE TYPO."],
                "verbiage": "ERROR",
                "ResponseCode": 400,
                "msoft_code": "INT_ERROR",
                "phard_code": "ERROR"
            },
            "expired_card": {
                "Msg": ["TRANSACTION ERROR. EXPIRATION DATE MUST BE IN FUTURE"],
                "verbiage": "ERROR",
                "ResponseCode": 400,
                "msoft_code": "INT_ERROR",
                "phard_code": "ERROR"
            },
            "invalid_cvv": {
                "Msg": ["TRANSACTION ERROR. BAD CID"],
                "verbiage": "ERROR",
                "ResponseCode": 400,
                "msoft_code": "INT_ERROR",
                "phard_code": "ERROR"
            },
            "unauthorized_merchant": {
                "Msg": ["THIS MERCHANT NUMBER IS NOT AUTHORIZED TO DO THIS OPERATION"],
                "verbiage": "ERROR",
                "ResponseCode": 401,
                "msoft_code": "INT_ERROR",
                "phard_code": "ERROR"
            },
            "duplicate_order": {
                "Msg": ["TRYING TO SEND AN INVALID OR UNAUTHORIZED REFERENCE NUMBER"],
                "verbiage": "ERROR",
                "ResponseCode": 400,
                "msoft_code": "INT_ERROR",
                "phard_code": "ERROR"
            }
        }
        
        scenario = failure_scenarios.get(failure_type, failure_scenarios["decline"])
        
        response = TransactionSaleResponse(
            cv="BAD",
            Msg=scenario["Msg"],
            avs="BAD",
            Token=None,
            Balance=None,
            CardType="VISA",
            LastFour="0000",
            verbiage=scenario["verbiage"],
            CustomerId=0,
            msoft_code=scenario["msoft_code"],
            phard_code=scenario["phard_code"],
            ResponseCode=scenario["ResponseCode"],
            displayMessage=None,
            PaymentPlanInfo=PaymentPlanInfo(),
            AuthorizationNumber="",
            ServiceReferenceNumber=""
        )
        
        return response, scenario["ResponseCode"]


# Instancia global del servicio
bms_mock_service = BMSPayMockService()