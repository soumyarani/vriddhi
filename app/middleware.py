from __future__ import annotations

import re
import time
import uuid

from fastapi import HTTPException, Request, status

from app.config import settings

PHONE_RE = re.compile(r"(\+?\d{2})\d{4,8}(\d{2})")
EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
PAYMENT_ID_RE = re.compile(r"(pay_[A-Za-z0-9]{3})[A-Za-z0-9]+")


def redact_pii(text: str) -> str:
    text = PHONE_RE.sub(r"\1****\2", text)
    text = EMAIL_RE.sub(r"\1***\2", text)
    text = PAYMENT_ID_RE.sub(r"\1****", text)
    return text


async def request_guards_middleware(request: Request, call_next):
    correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())
    request.state.correlation_id = correlation_id

    max_bytes = settings.max_webhook_body_bytes if request.url.path.startswith("/webhook") or request.url.path.startswith("/api/payments") else settings.max_request_body_bytes
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > max_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Request body too large")

    if request.method in {"POST", "PUT", "PATCH"} and request.url.path != "/health":
        content_type = request.headers.get("content-type", "")
        if content_type and "application/json" not in content_type and "multipart/form-data" not in content_type:
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Unsupported Content-Type")

    started = time.perf_counter()
    response = await call_next(request)
    response.headers["x-correlation-id"] = correlation_id
    response.headers["x-request-duration-ms"] = str(round((time.perf_counter() - started) * 1000, 2))
    return response
