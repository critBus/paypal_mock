from __future__ import annotations

import base64
from binascii import Error as BinasciiError

from cryptography.fernet import Fernet, InvalidToken




# https://morioh.com/p/4f5288b77c14
def encrypt(txt: str, encryption_key: str | None = None) -> str:
    fernet_key = encryption_key.encode("utf-8")
    try:
        # convert integer etc to string first
        txt = str(txt)
        # get the key from settings
        cipher_suite = Fernet(fernet_key)
        # #input should be byte, so convert the text to byte
        encrypted_text = cipher_suite.encrypt(txt.encode("utf-8"))
        # encode to urlsafe base64 format
        return base64.b64encode(encrypted_text).decode("utf-8")
    except Exception:
        print("Failed to encrypt value")
        raise


def decrypt(encrypted_string: str, encryption_key: str | None = None) -> str:
    fernet_key = encryption_key.encode("utf-8")
    try:
        # base64 decode
        txt = base64.b64decode(encrypted_string)
        cipher_suite = Fernet(fernet_key)
        return cipher_suite.decrypt(txt).decode("utf-8")
    except InvalidToken:
        print("Failed to decrypt value with invalid token")
        return encrypted_string
    except (BinasciiError, ValueError):
        print("Failed to decode encrypted value")
        raise
    except Exception:
        print("Failed to decrypt value")
        raise
