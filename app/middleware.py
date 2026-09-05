from __future__ import annotations

import time
import uuid

import structlog
from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.config import settings
from logging_config import get_logger

log = get_logger("http")

WEBHOOK_PATH_PREFIXES = ("/webhook", "/api/payments/cashfree-webhook")
ALLOWED_CONTENT_TYPES = ("application/json", "multipart/form-data", "application/x-www-form-urlencoded")
EXEMPT_PATHS = ("/health", "/health/ready", "/metrics", "/docs", "/redoc", "/openapi.json")


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind a request-scoped correlation ID so every log line can be joined."""

    async def dispatch(self, request: Request, call_next):
        correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())
        request.state.correlation_id = correlation_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id,
            method=request.method,
            path=request.url.path,
        )
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("method", "path")
        response.headers["X-Correlation-ID"] = correlation_id
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            log.exception(
                "request_failed",
                duration_ms=duration_ms,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        log.info(
            "request_completed",
            status_code=response.status_code,
            duration_ms=duration_ms,
            user_id=getattr(request.state, "user_id", None),
        )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies before they are buffered into memory.

    Content-Length is checked first (cheap); requests without it are streamed
    and aborted once the running total exceeds the limit.
    """

    async def dispatch(self, request: Request, call_next):
        limit = (
            settings.max_webhook_body_bytes
            if request.url.path.startswith(WEBHOOK_PATH_PREFIXES)
            else settings.max_request_body_bytes
        )

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > limit:
                    return _too_large(limit)
            except ValueError:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Invalid Content-Length header"},
                )
            return await call_next(request)

        if request.method in ("POST", "PUT", "PATCH"):
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > limit:
                    return _too_large(limit)

            # Re-serve the consumed stream to downstream handlers.
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = receive  # noqa: SLF001

        return await call_next(request)


def _too_large(limit: int) -> JSONResponse:
    log.warning("request_body_too_large", limit_bytes=limit)
    return JSONResponse(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        content={"detail": f"Request body exceeds {limit} bytes"},
    )


class ContentTypeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            has_body = request.headers.get("content-length") not in (None, "0")
            content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
            if has_body and content_type and content_type not in ALLOWED_CONTENT_TYPES:
                return JSONResponse(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    content={"detail": f"Unsupported Content-Type: {content_type}"},
                )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self.hsts = settings.enable_hsts and settings.is_production

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-XSS-Protection", "0")
        if self.hsts:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
