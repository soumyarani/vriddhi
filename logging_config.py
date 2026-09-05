"""Structured JSON logging with PII redaction.

Redaction runs as a structlog processor so it applies to every log record
regardless of which module emitted it, including values nested inside dicts.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

_PHONE_RE = re.compile(r"(\+?\d{1,3})?[\s-]?(\d{9,12})")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])([A-Za-z0-9._%+-]*)(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "authorization",
    "access_token",
    "refresh_token",
    "jwt_secret",
    "api_key",
    "openai_api_key",
    "cashfree_secret_key",
    "whatsapp_token",
    "whatsapp_app_secret",
    "client_secret",
    "signature",
    "x-hub-signature-256",
}

PII_KEYS = {"phone", "email", "cashfree_payment_id", "cashfree_order_id", "payment_id", "gstin"}


def mask_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) < 4:
        return "****"
    prefix = "+" if value.strip().startswith("+") else ""
    country = digits[:-10] if len(digits) > 10 else ""
    return f"{prefix}{country}****{digits[-4:]}"


def mask_email(value: str) -> str:
    match = _EMAIL_RE.fullmatch(value.strip())
    if not match:
        return "***"
    return f"{match.group(1)}***{match.group(3)}"


def mask_id(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return f"{value[:2]}***{value[-4:]}"


def redact_text(text: str) -> str:
    text = _EMAIL_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)
    text = _CARD_RE.sub("[redacted-card]", text)
    text = _PHONE_RE.sub(lambda m: f"{m.group(1) or ''}****{m.group(2)[-4:]}", text)
    return text


def redact_value(key: str, value: Any, depth: int = 0) -> Any:
    lowered = key.lower()
    if lowered in SENSITIVE_KEYS:
        return "[redacted]"
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {k: redact_value(k, v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(key, v, depth + 1) for v in value]
    if isinstance(value, str):
        if lowered == "phone":
            return mask_phone(value)
        if lowered == "email":
            return mask_email(value)
        if lowered in PII_KEYS:
            return mask_id(value)
        return redact_text(value)
    return value


def pii_processor(_logger: Any, _name: str, event_dict: dict) -> dict:
    return {k: redact_value(k, v) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            pii_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level.upper())
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
