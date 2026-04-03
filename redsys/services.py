import base64
import json
import hmac
import hashlib
import re
import time
from typing import Tuple, Dict, Any, Optional
from Crypto.Cipher import DES3
import httpx
from config import settings

class RedsysSignatureGenerator:
    """Genera signatures válidas para testing de Redsys"""
    
    @staticmethod
    def encrypt_3des(order_number: str, key: str) -> bytes:
        """Encripta el order number con 3DES"""
        cipher = DES3.new(
            base64.b64decode(key),
            DES3.MODE_CBC,
            IV=b"\0\0\0\0\0\0\0\0"
        )
        return cipher.encrypt(order_number.encode().ljust(16, b"\0"))
    
    @staticmethod
    def generate_signature(order_number: str, merchant_parameters: str, key: str) -> str:
        """Genera la signature HMAC SHA256 para Redsys"""
        encrypted_order = RedsysSignatureGenerator.encrypt_3des(order_number, key)
        signature = hmac.new(
            encrypted_order,
            merchant_parameters.encode(),
            hashlib.sha256
        ).digest()
        return base64.b64encode(signature).decode()
    
    @staticmethod
    def build_merchant_parameters(data: Dict[str, Any]) -> str:
        """Construye y encodea los merchant parameters"""
        json_data = json.dumps(data)
        return base64.b64encode(json_data.encode()).decode()
    
    @staticmethod
    def decode_merchant_parameters(encoded_params: str) -> Dict[str, Any]:
        """Decodifica los merchant parameters de base64"""
        try:
            decoded = base64.b64decode(encoded_params).decode()
            return json.loads(decoded)
        except Exception as e:
            return {"error": str(e)}




class RedsysFormParser:
    """Parser para extraer datos del formulario HTML de Redsys"""
    
    @staticmethod
    def normalize_html(form_html: str) -> str:
        """
        Normaliza el HTML eliminando escapes de JSON
        Soporta: \" y " comillas
        """
        normalized = form_html.replace('\\"', '"')
        normalized = normalized.replace('\\n', '\n')
        normalized = normalized.replace('\\t', '\t')
        normalized = normalized.replace('\\\\', '\\')
        return normalized
    
    @staticmethod
    def parse_form_html(form_html: str) -> Dict[str, Any]:
        """
        Extrae los datos del formulario HTML de Redsys
        Soporta tanto HTML normal como HTML con escapes de JSON
        """
        result = {
            "success": False,
            "data": {},
            "error": None
        }
        
        try:
            # Normalizar el HTML (quitar escapes de JSON)
            normalized_html = RedsysFormParser.normalize_html(form_html)
            
            # Extraer data-payment-id
            payment_id_match = re.search(r'data-payment-id="([^"]+)"', normalized_html)
            if payment_id_match:
                result["data"]["payment_id"] = payment_id_match.group(1)
            
            # Extraer data-payment-order-number
            order_number_match = re.search(r'data-payment-order-number="([^"]+)"', normalized_html)
            if order_number_match:
                result["data"]["order_number"] = order_number_match.group(1)
            
            # Extraer action URL
            action_match = re.search(r'action="([^"]+)"', normalized_html)
            if action_match:
                result["data"]["action_url"] = action_match.group(1)
            
            # Extraer Ds_SignatureVersion
            sig_version_match = re.search(r'name="Ds_SignatureVersion"[^>]*value="([^"]+)"', normalized_html)
            if sig_version_match:
                result["data"]["signature_version"] = sig_version_match.group(1)
            
            # Extraer Ds_MerchantParameters
            merchant_params_match = re.search(r'name="Ds_MerchantParameters"[^>]*value="([^"]+)"', normalized_html)
            if merchant_params_match:
                result["data"]["merchant_parameters"] = merchant_params_match.group(1)
                # Decodificar para mostrar
                result["data"]["decoded_params"] = RedsysSignatureGenerator.decode_merchant_parameters(
                    result["data"]["merchant_parameters"]
                )
            
            # Extraer Ds_Signature
            signature_match = re.search(r'name="Ds_Signature"[^>]*value="([^"]+)"', normalized_html)
            if signature_match:
                result["data"]["signature"] = signature_match.group(1)
            
            # Validar que tenemos los campos requeridos
            required_fields = ["order_number", "merchant_parameters", "signature"]
            missing_fields = [f for f in required_fields if f not in result["data"]]
            
            if missing_fields:
                result["error"] = f"Campos requeridos faltantes: {', '.join(missing_fields)}"
            else:
                result["success"] = True
                
        except Exception as e:
            result["error"] = str(e)
        
        return result


class RedsysWebhookSimulator:
    """Simula webhooks de Redsys para testing - COMPATIBLE CON DJANGO"""
    
    # Códigos de respuesta comunes de Redsys
    RESPONSE_CODES = {
        # Éxito
        "0000": {"status": "paid", "description": "Transacción autorizada"},
        "0001": {"status": "paid", "description": "Transacción autorizada para pagos"},
        "0002": {"status": "paid", "description": "Transacción autorizada"},
        # Cancelación
        "0400": {"status": "cancelled", "description": "Transacción cancelada"},
        "9915": {"status": "cancelled", "description": "Pago cancelado por el usuario"},
        # Errores
        "0101": {"status": "failed", "description": "Tarjeta caducada"},
        "0102": {"status": "failed", "description": "Tarjeta en excepción temporal"},
        "0129": {"status": "failed", "description": "CVV2 incorrecto"},
        "0172": {"status": "failed", "description": "Denegada, no reintentar"},
        "0190": {"status": "failed", "description": "Denegación del emisor"},
        "9008": {"status": "failed", "description": "Error de formato en Ds_Merchant_MerchantCode"},
        "9018": {"status": "failed", "description": "Falta Ds_Merchant_Amount"},
        "9020": {"status": "failed", "description": "Falta Ds_Merchant_MerchantSignature"},
        "9041": {"status": "failed", "description": "Error en cálculo de firma"},
    }
    
    def __init__(self):
        self.signature_generator = RedsysSignatureGenerator()
        self.form_parser = RedsysFormParser()
    
    def get_payment_status(self, response_code: str) -> str:
        """Obtiene el status de pago según el código de respuesta"""
        code_info = self.RESPONSE_CODES.get(response_code, {})
        return code_info.get("status", "failed")
    
    def parse_form(self, form_html: str) -> Dict[str, Any]:
        """Parsea el formulario HTML de Redsys"""
        return self.form_parser.parse_form_html(form_html)
    
    def build_webhook_payload(
        self,
        order_number: str,
        response_code: str = "0000",
        amount: str = "1800",
        currency: str = "840",
        transaction_type: str = "0",
        merchant_code: Optional[str] = None,
        terminal: Optional[str] = None,
        simulate_signature: bool = True
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        """
        Construye el payload del webhook de Redsys
        COMPATIBLE CON: redsys/services/response_parser.py
        
        Returns:
            Tuple con (form_data, decoded_parameters)
        """
        from datetime import datetime
        
        merchant_code = merchant_code or settings.REDSYS_TEST_MERCHANT_CODE
        terminal = terminal or settings.REDSYS_TEST_TERMINAL
        
        # Calcular amount en euros (aproximado para testing)
        amount_euro = str(int(float(amount) * 0.88))  # USD a EUR aproximado
        
        # Generar timestamp para Ds_Control_* (como hace Redsys real)
        control_timestamp = str(int(time.time() * 1000))
        
        # Obtener fecha y hora actual (formato Redsys)
        now = datetime.now()
        ds_date = now.strftime("%d/%m/%Y")
        ds_hour = now.strftime("%H:%M")
        
        # Parámetros que van en Ds_MerchantParameters
        # ESTOS DEBEN COINCIDIR CON LOS QUE DJANGO ESPERA
        merchant_params = {
            "Ds_MerchantCode": merchant_code,
            "Ds_Terminal": terminal,
            "Ds_Order": order_number,
            "Ds_Amount": amount,
            "Ds_Currency": currency,
            "Ds_Date": ds_date,
            "Ds_Hour": ds_hour,
            "Ds_SecurePayment": "1",  # CRÍTICO - Django lo verifica
            "Ds_Card_Country": "724",  # España (común en testing)
            "Ds_Response": response_code,
            "Ds_MerchantData": "",  # CRÍTICO - Django espera este campo
            "Ds_TransactionType": transaction_type,
            "Ds_ConsumerLanguage": "1",  # CRÍTICO - Español
            "Ds_AuthorisationCode": "123456" if response_code.startswith("00") else "",
            "Ds_Card_Brand": "1",  # VISA
            "Ds_Card_Typology": "CONSUMO",  # CRÍTICO
            "Ds_ProcessedPayMethod": "78",  # CRÍTICO
            "Ds_Amount_Euro": amount_euro,  # CRÍTICO
            f"Ds_Control_{control_timestamp}": control_timestamp,  # Dynamic field
        }
        
        # Encodear merchant parameters en base64
        merchant_parameters_encoded = self.signature_generator.build_merchant_parameters(merchant_params)
        
        # Generar signature compatible con Django
        if simulate_signature:
            signature = self.signature_generator.generate_signature(
                order_number=order_number,
                merchant_parameters=merchant_parameters_encoded,
                key=settings.REDSYS_TEST_MERCHANT_KEY
            )
        else:
            signature = "INVALID_SIGNATURE_FOR_TESTING"
        
        # Form data para enviar al webhook (exactamente como Redsys lo envía)
        form_data = {
            "Ds_SignatureVersion": "HMAC_SHA256_V1",
            "Ds_MerchantParameters": merchant_parameters_encoded,
            "Ds_Signature": signature,
        }
        
        return form_data, merchant_params
    
    async def send_webhook_to_django(
        self,
        order_number: str,
        response_code: str = "0000",
        amount: Optional[str] = None,
        currency: Optional[str] = None,
        simulate_signature: bool = True
    ) -> Dict[str, Any]:
        """
        Envía el webhook simulado a la API de Django
        """
        form_data, decoded_params = self.build_webhook_payload(
            order_number=order_number,
            response_code=response_code,
            amount=amount,
            currency=currency,
            simulate_signature=simulate_signature
        )
        
        # URL del webhook en Django
        webhook_url = f"{settings.DJANGO_API_URL}/api/redsys/webhooks/notifications/?con={order_number}"
        
        # Headers - EXACTAMENTE COMO REDSYS LOS ENVÍA
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Redsys-TPV/1.0",
        }
        
        # Agregar app key si está configurada (solo para testing local)
        if settings.REDSYS_APP_KEY:
            headers["X-Payment-Key"] = settings.REDSYS_APP_KEY
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    webhook_url,
                    data=form_data,  # form-data, NO JSON
                    headers=headers,
                    timeout=30.0
                )
                return {
                    "success": response.status_code in [200, 201],
                    "status_code": response.status_code,
                    "response_data": response.json() if response.content else {},
                    "webhook_payload": form_data,
                    "decoded_params": decoded_params,
                }
            except httpx.HTTPError as e:
                return {
                    "success": False,
                    "status_code": 0,
                    "error": str(e),
                    "webhook_payload": form_data,
                    "decoded_params": decoded_params,
                }
    
    async def get_order_status(self, order_number: str) -> Dict[str, Any]:
        """Obtiene el estado de una orden desde Django API"""
        if not settings.REDSYS_APP_KEY:
            return {"error": "REDSYS_APP_KEY no configurada"}
        
        url = f"{settings.DJANGO_API_URL}/api/redsys/order-request/{order_number}/"
        headers = {
            "X-Payment-Key": settings.REDSYS_APP_KEY,
            "Accept": "application/json",
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, headers=headers, timeout=10.0)
                return {
                    "success": response.status_code == 200,
                    "status_code": response.status_code,
                    "data": response.json() if response.content else {},
                }
            except httpx.HTTPError as e:
                return {
                    "success": False,
                    "status_code": 0,
                    "error": str(e),
                }