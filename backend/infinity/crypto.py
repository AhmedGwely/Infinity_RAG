"""Password hashing, opaque session tokens and encryption of stored credentials."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except Exception:
        return False
    return hmac.compare_digest(actual, expected)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def password_problems(password: str) -> list[str]:
    problems = []
    if len(password) < 10:
        problems.append("at least 10 characters")
    if password.lower() == password or password.upper() == password:
        problems.append("both upper- and lower-case letters")
    if not any(c.isdigit() for c in password):
        problems.append("a digit")
    return problems


UNREADABLE_MESSAGE = ("A stored credential could not be decrypted with the current APP_SECRET_KEY (it was saved with a different key). "
                      "Re-enter the credential in the web interface (Providers / Sources), or restore the original APP_SECRET_KEY.")


class SecretUnreadableError(ValueError):
    """A stored encrypted value cannot be decrypted with the current key. (ValueError subclass for backwards compatibility.)"""


def key_fingerprint(secret: str) -> str:
    """Short, non-reversible identifier of a key, safe to print/log for comparing candidates."""
    return hashlib.sha256(secret.encode()).hexdigest()[:12].upper()


class SecretBox:
    """Encrypt small secrets (API keys) with a key derived from APP_SECRET_KEY."""

    def __init__(self, secret: str) -> None:
        if len(secret) < 16:
            raise ValueError("APP_SECRET_KEY must be at least 16 characters")
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as error:
            raise SecretUnreadableError(UNREADABLE_MESSAGE) from error

    def encrypt_json(self, value: dict) -> str:
        return self.encrypt(json.dumps(value))

    def decrypt_json(self, token: str) -> dict:
        return json.loads(self.decrypt(token))

    def can_decrypt(self, token: str) -> bool:
        try:
            self._fernet.decrypt(token.encode())
            return True
        except InvalidToken:
            return False


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    return "\u2022" * 8 + value[-4:] if len(value) > 8 else "\u2022" * 8
