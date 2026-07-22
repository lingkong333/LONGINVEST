from __future__ import annotations

import base64
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretCipher:
    def __init__(self, encoded_key: str) -> None:
        try:
            key = base64.b64decode(encoded_key.encode(), altchars=b"-_", validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("LONGINVEST_MASTER_KEY must be URL-safe base64") from exc
        if len(key) != 32:
            raise ValueError("LONGINVEST_MASTER_KEY must decode to 32 bytes")
        self._cipher = AESGCM(key)
        self._fingerprint_key = key

    def encrypt(self, key_name: str, value: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._cipher.encrypt(nonce, value.encode(), key_name.encode())

    def decrypt(self, key_name: str, ciphertext: bytes) -> str:
        nonce, payload = ciphertext[:12], ciphertext[12:]
        return self._cipher.decrypt(nonce, payload, key_name.encode()).decode()

    def fingerprint(self, key_name: str, value: str) -> str:
        return hmac.digest(
            self._fingerprint_key,
            f"{key_name}\0{value}".encode(),
            "sha256",
        ).hex()[:16]
