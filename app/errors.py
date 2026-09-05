"""Domain exceptions.

Services raise these instead of HTTPException so they stay usable from arq
workers and the AI action executor, where there is no HTTP request. A single
handler in main.py maps them to responses.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, detail: Any = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        if code:
            self.code = code

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"detail": self.message, "code": self.code}
        if self.detail is not None:
            body["errors"] = self.detail
        return body


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class AuthError(AppError):
    status_code = 401
    code = "unauthorized"


class PermissionError_(AppError):
    status_code = 403
    code = "forbidden"


class OutOfStockError(ConflictError):
    code = "out_of_stock"


class PaymentError(AppError):
    status_code = 502
    code = "payment_failed"


class UpstreamError(AppError):
    status_code = 503
    code = "upstream_unavailable"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"
