"""Encryption of stored Telegram sessions and web-session tokens.

A Telethon session string is a full login to the user's Telegram account, so it
is never written to disk in plain text.
"""

import base64
import hashlib
import logging
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

log = logging.getLogger(__name__)


def key_from_secret(secret: str) -> bytes:
    """Turn SECRET_KEY into a Fernet key.

    A key from ``make key`` is used as is; any other string is stretched with
    scrypt, so SECRET_KEY can be any long random string.
    """
    try:
        Fernet(secret)
        return secret.encode()
    except ValueError:
        pass
    if len(secret) < 16:
        log.warning("SECRET_KEY is short; use a long random string (e.g. from `make key`).")
    derived = hashlib.scrypt(secret.encode(), salt=b"telegramq/secret-key/v1", n=2**14, r=8, p=1, dklen=32)
    return base64.urlsafe_b64encode(derived)


def load_or_create_key(configured: str | None, data_dir: Path) -> bytes:
    if configured:
        return key_from_secret(configured)
    key_file = data_dir / "secret.key"
    if key_file.exists():
        return key_file.read_bytes().strip()
    key = Fernet.generate_key()
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


class SessionCipher:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
