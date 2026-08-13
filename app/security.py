from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt

from app.config import settings


class TokenError(Exception):
    pass


def create_access_token(subject: str, actor: str = "user", role: str | None = None) -> str:
    exp = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": subject,
        "exp": exp,
        "type": "access",
        "actor": actor,
        "iss": settings.jwt_issuer,
    }
    if role:
        payload["role"] = role
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise TokenError("Invalid token") from exc
    if payload.get("type") != "access":
        raise TokenError("Invalid token type")
    if not payload.get("sub"):
        raise TokenError("Missing subject")
    return payload


def verify_webhook_hmac_sha256(body: bytes, header_value: str | None, secret: str) -> bool:
    if not header_value or not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value)


def verify_ip_allowed(ip: str | None) -> bool:
    whitelist = settings.webhook_ip_set
    if not whitelist:
        return True
    if not ip:
        return False
    return ip in whitelist


def payload_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
