"""Single-owner login: a scrypt password hash and an HMAC-signed session cookie."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

COOKIE = "jobagent_session"
SESSION_TTL_S = 7 * 24 * 3600


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    expected = hash_password(password, bytes.fromhex(salt_hex)).split("$")[2]
    return hmac.compare_digest(expected, digest_hex)


def _sign(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def make_token(secret: str, now: float | None = None) -> str:
    expires = int((now or time.time()) + SESSION_TTL_S)
    body = f"{expires}.{secrets.token_hex(8)}"
    return f"{body}.{_sign(secret, body)}"


def token_valid(secret: str, token: str, now: float | None = None) -> bool:
    try:
        expires, nonce, sig = token.split(".")
    except ValueError:
        return False
    if not hmac.compare_digest(_sign(secret, f"{expires}.{nonce}"), sig):
        return False
    return expires.isdigit() and int(expires) > (now or time.time())
