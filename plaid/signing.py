"""Firma ES256 y JWK compatibles con Plaid-Verification."""

import base64
import hashlib
import json
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def signing_key(store):
    with store.transaction() as db:
        record = db.get("signing", "key")
        if not record:
            private = ec.generate_private_key(ec.SECP256R1())
            record = {
                "pem": private.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode(),
                "created_at": int(time.time()),
            }
            db.put("signing", "key", "system", record)
    return serialization.load_pem_private_key(
        record["pem"].encode(), password=None
    ), record["created_at"]


def public_key(store):
    private, created = signing_key(store)
    numbers = private.public_key().public_numbers()
    kid = hashlib.sha256(
        numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
    ).hexdigest()[:32]
    return {
        "alg": "ES256",
        "kid": kid,
        "kty": "EC",
        "crv": "P-256",
        "use": "sig",
        "x": b64(numbers.x.to_bytes(32, "big")),
        "y": b64(numbers.y.to_bytes(32, "big")),
        "created_at": created,
        "expired_at": None,
    }


def sign(store, raw):
    private, _ = signing_key(store)
    header = b64(
        json.dumps(
            {"alg": "ES256", "kid": public_key(store)["kid"]}, separators=(",", ":")
        ).encode()
    )
    claims = b64(
        json.dumps(
            {
                "iat": int(time.time()),
                "request_body_sha256": hashlib.sha256(raw).hexdigest(),
            },
            separators=(",", ":"),
        ).encode()
    )
    message = f"{header}.{claims}"
    r, s = decode_dss_signature(
        private.sign(message.encode(), ec.ECDSA(hashes.SHA256()))
    )
    return f"{message}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
