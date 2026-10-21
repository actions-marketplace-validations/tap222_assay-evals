"""Server-key encryption for secrets at rest (Slack, Jira, Linear tokens).

Secrets in Assay's database (integrations config) are encrypted using a server
master key (ASSAY_SECRET_KEY). Tokens are encrypted on write and transparently
decrypted when used for outbound requests. Existing plaintext secrets are read
transparently and encrypted on next write or via migration.
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken

ENC_PREFIX = "enc:v1:"


def derive_key(secret: str) -> bytes:
    """Derive a URL-safe base64 32-byte Fernet key from any passphrase or secret key."""
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def get_cipher(key: Optional[str] = None) -> Optional[Fernet]:
    k = key or os.environ.get("ASSAY_SECRET_KEY")
    if not k:
        return None
    return Fernet(derive_key(k))


def encrypt_secret(val: str, key: Optional[str] = None) -> str:
    """Encrypt a secret string using the server key, returning enc:v1:<token>.

    If val is empty or already starts with enc:v1:, it is returned as-is.
    If no key is configured, val is returned as-is.
    """
    if not val or val.startswith(ENC_PREFIX):
        return val
    cipher = get_cipher(key)
    if cipher is None:
        return val
    token = cipher.encrypt(val.encode("utf-8")).decode("utf-8")
    return f"{ENC_PREFIX}{token}"


def decrypt_secret(val: str, key: Optional[str] = None) -> str:
    """Decrypt an enc:v1:<token> secret string.

    If val does not start with enc:v1:, it is treated as plaintext and returned
    unchanged (providing backward compatibility for pre-existing databases).
    """
    if not val or not val.startswith(ENC_PREFIX):
        return val
    cipher = get_cipher(key)
    if cipher is None:
        raise ValueError("Secret is encrypted at rest, but no server key (ASSAY_SECRET_KEY) is configured.")
    token = val[len(ENC_PREFIX):]
    try:
        return cipher.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise ValueError("Failed to decrypt secret: invalid server key or corrupted token.")


def is_encrypted(val: str) -> bool:
    return isinstance(val, str) and val.startswith(ENC_PREFIX)
