from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


class MessageResponse(BaseModel):
    detail: str
    success: bool = True


class ErrorResponse(BaseModel):
    detail: str
    code: str = "error"


# ---------------------------------------------------------------------------
# Reusable OpenAPI response documentation
#
# Every handler can return these, because they come from the middleware and the
# error handlers rather than from the route body. Declaring them per-operation
# would mean repeating the same four entries ~94 times, so routers attach the
# relevant set once at APIRouter construction.
# ---------------------------------------------------------------------------
def _err(description: str, code: str, detail: str) -> dict:
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {
            "application/json": {"example": {"code": code, "detail": detail}}
        },
    }


RATE_LIMITED = {
    429: _err("Rate limit exceeded; see the Retry-After header.",
              "rate_limited", "Too many requests"),
}

AUTH_RESPONSES = {
    401: _err("Missing, malformed or expired access token.",
              "unauthorized", "Not authenticated"),
    **RATE_LIMITED,
}

STAFF_RESPONSES = {
    **AUTH_RESPONSES,
    403: _err("Authenticated, but this role may not perform the action.",
              "forbidden", "Insufficient permissions"),
    404: _err("No such record.", "not_found", "Order not found"),
    409: _err("Well-formed, but rejected by current state — for example an "
              "illegal order transition.",
              "conflict", "Cannot cancel a delivered order"),
}

CUSTOMER_RESPONSES = {
    **AUTH_RESPONSES,
    404: _err(
        "No such record. Also returned when the record belongs to another "
        "customer, so that IDs cannot be probed for existence.",
        "not_found", "Order not found",
    ),
    409: _err(
        "Well-formed, but rejected by current state — insufficient stock, an "
        "expired coupon, or an order that can no longer change.",
        "conflict", "Only 3 left in stock",
    ),
}


class HealthComponent(BaseModel):
    name: str
    healthy: bool
    latency_ms: float | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: str
    version: str = "1.0.0"
    environment: str
    checked_at: datetime
    components: list[HealthComponent] = Field(default_factory=list)
