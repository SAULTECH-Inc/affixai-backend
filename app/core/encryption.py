"""AES-256-GCM field-level encryption.

Ciphertext format: `<iv_hex>:<auth_tag_hex>:<ciphertext_hex>`.
Compatible with the NestJS EncryptionService it replaces — existing rows can
still be decrypted as long as ENCRYPTION_KEY is unchanged.
"""
from __future__ import annotations

import hashlib
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings


@lru_cache(maxsize=1)
def _key() -> bytes:
    # Match NestJS: SHA-256 of the configured secret yields a 32-byte AES-256 key.
    return hashlib.sha256(settings.ENCRYPTION_KEY.encode()).digest()


def encrypt(plaintext: str) -> str:
    iv = os.urandom(16)
    aesgcm = AESGCM(_key())
    # AESGCM returns ciphertext || 16-byte auth tag concatenated.
    combined = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
    ciphertext, auth_tag = combined[:-16], combined[-16:]
    return f"{iv.hex()}:{auth_tag.hex()}:{ciphertext.hex()}"


def decrypt(payload: str) -> str:
    iv_hex, tag_hex, ct_hex = payload.split(":", 2)
    iv = bytes.fromhex(iv_hex)
    tag = bytes.fromhex(tag_hex)
    ciphertext = bytes.fromhex(ct_hex)
    aesgcm = AESGCM(_key())
    plaintext = aesgcm.decrypt(iv, ciphertext + tag, None)
    return plaintext.decode("utf-8")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
