"""Webhook authenticity checks: HMAC signatures and optional source-IP allowlists."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from base64 import b64encode

from fastapi import Request

from app.config import settings
from logging_config import get_logger

log = get_logger(__name__)


def verify_meta_signature(payload: bytes, header_value: str | None) -> bool:
    """Validate Meta's `X-Hub-Signature-256` (HMAC-SHA256 of the raw body)."""
    if not settings.whatsapp_app_secret:
        # Unconfigured secret must never silently pass in production.
        if settings.is_production:
            log.error("meta_signature_secret_missing")
            return False
        log.warning("meta_signature_skipped_no_secret")
        return True

    if not header_value or not header_value.startswith("sha256="):
        return False

    expected = hmac.new(
        settings.whatsapp_app_secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header_value.removeprefix("sha256="))


def verify_cashfree_signature(
    raw_body: bytes, signature: str | None, timestamp: str | None
) -> bool:
    """Validate Cashfree's webhook signature: base64(HMAC-SHA256(timestamp + body))."""
    if not settings.cashfree_webhook_secret:
        if settings.is_production:
            log.error("cashfree_signature_secret_missing")
            return False
        log.warning("cashfree_signature_skipped_no_secret")
        return True

    if not signature or not timestamp:
        return False

    signed_payload = timestamp.encode() + raw_body
    digest = hmac.new(
        settings.cashfree_webhook_secret.encode(), signed_payload, hashlib.sha256
    ).digest()
    return hmac.compare_digest(b64encode(digest).decode(), signature)


def verify_meta_verify_token(token: str | None) -> bool:
    if not settings.whatsapp_verify_token:
        return False
    return hmac.compare_digest(settings.whatsapp_verify_token, token or "")


def client_ip(request: Request) -> str:
    """Left-most X-Forwarded-For entry, falling back to the socket peer.

    Only trust this behind a proxy that overwrites the header; a raw
    internet-facing deployment can be spoofed here.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _ip_allowed(ip: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowed:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            log.warning("invalid_ip_whitelist_entry", entry=entry)
    return False


def check_meta_ip(request: Request) -> bool:
    if not settings.webhook_ip_whitelist_enabled:
        return True
    return _ip_allowed(client_ip(request), settings.meta_ip_list)


def check_cashfree_ip(request: Request) -> bool:
    if not settings.webhook_ip_whitelist_enabled:
        return True
    return _ip_allowed(client_ip(request), settings.cashfree_ip_list)


def payload_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
