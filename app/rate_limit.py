"""Rate limiting via slowapi, backed by Redis so limits are shared across replicas."""

from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

from app.config import settings
from app.security import client_ip


def _identity(request: Request) -> str:
    """Prefer the authenticated principal so one noisy IP can't throttle a NAT.

    `request.state.rate_limit_key` is set by the auth dependency once the
    caller is resolved; unauthenticated traffic falls back to client IP.
    """
    key = getattr(request.state, "rate_limit_key", None)
    if key:
        return key
    return client_ip(request) or get_remote_address(request)


limiter = Limiter(
    key_func=_identity,
    storage_uri=settings.redis_url if settings.rate_limit_enabled else "memory://",
    enabled=settings.rate_limit_enabled,
    headers_enabled=True,
    # Fail open. If Redis is unreachable the limiter cannot count, and the
    # choice is between serving unthrottled traffic and 500ing every limited
    # endpoint — including checkout. Losing rate limiting is a degradation;
    # losing the storefront is an outage. The Redis probe in /health/ready is
    # what surfaces the underlying problem.
    swallow_errors=True,
)


async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Please slow down and retry."},
        headers={"Retry-After": str(getattr(exc, "retry_after", 60) or 60)},
    )
