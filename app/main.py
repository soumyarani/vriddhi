"""Application entrypoint.

Run with:  uvicorn app.main:app

Middleware order matters and is the reverse of the order added: Starlette wraps
each new layer *around* the previous one, so the last one added is the first to
see a request. The stack below is added so that, at runtime, a request passes
correlation-id → logging → body-size → content-type → security-headers → CORS
→ routes. The correlation id is established first because everything after it
wants to log under that id, and the body-size guard runs before anything reads
the body.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.database import dispose_engine
from app.errors import AppError
from app.middleware import (
    BodySizeLimitMiddleware,
    ContentTypeMiddleware,
    CorrelationIdMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
)
from app.rate_limit import limiter, rate_limit_handler
from app.redis import close_arq, close_redis
from app.routers import admin, auth, health, store, webhook
from app.routers.health import setup_metrics
from logging_config import configure_logging, get_logger

log = get_logger(__name__)

# Cross-cutting behaviour that the per-route schema cannot express: auth, the
# error envelope, pagination and idempotency are uniform across every endpoint,
# so they are documented once here rather than repeated 94 times.
API_DESCRIPTION = """
Backend for a WhatsApp-first shopping experience with a companion web storefront.
Both channels share one catalogue, cart, order and payment model.

### Authentication

All authenticated routes take `Authorization: Bearer <access_token>`.

Obtain tokens by POSTing a Google authorization code to `/api/auth/google`
(customers) or `/api/auth/agent/google` (staff). Access tokens live 15 minutes;
refresh tokens live 7 days and are **rotated on every use** — the old one stops
working the moment you exchange it.

Presenting an already-rotated refresh token is treated as theft: the entire token
family is revoked and every session for that user ends. Store exactly one refresh
token per client and never retry a refresh with a stale value.

WhatsApp customers never authenticate here. They are identified by the phone
number on the inbound webhook and created on first contact.

### Errors

Every error shares one envelope, so a client can branch on `code` without
parsing prose:

```json
{ "code": "not_found", "detail": "Product not found" }
```

Common codes: `validation_error` (422), `unauthorized` (401), `forbidden` (403),
`not_found` (404), `conflict` (409), `rate_limited` (429).

`409 conflict` is the interesting one — it means the request was well-formed but
the world disagreed: insufficient stock, an expired coupon, or an illegal order
transition. It is worth surfacing to the user, not retrying blindly.

Requesting an order that belongs to somebody else returns **404, not 403**, so
that order IDs cannot be probed for existence.

### Pagination

List endpoints are cursor-based, not offset-based, so results stay stable while
new rows arrive:

```
GET /api/products?limit=20
GET /api/products?limit=20&cursor=<next_cursor from the previous response>
```

Responses carry `{ "items": [...], "next_cursor": "...", "has_more": true }`.
Treat `cursor` as opaque. `has_more` is exact — the query over-fetches by one row
rather than guessing.

### Rate limits

Per user where authenticated, per IP otherwise: auth 10/min, coupon validation
5/min, storefront 60/min, admin 120/min, webhooks 1000/min. Exceeding a limit
returns 429 with a `Retry-After` header.

### Money

Prices are GST-inclusive rupees, in line with Indian retail practice. Tax is
extracted out of the displayed price rather than added on top, so the amount a
customer sees is the amount they pay.
"""

TAGS_METADATA = [
    {
        "name": "auth",
        "description": (
            "Google OAuth sign-in for customers and staff, token refresh, logout. "
            "Staff sign-in additionally requires the email domain to be listed in "
            "`AGENT_ALLOWED_DOMAINS`."
        ),
    },
    {
        "name": "store",
        "description": (
            "The customer-facing surface: catalogue, cart, checkout, orders, "
            "addresses, wishlist and reviews. Catalogue reads are public; "
            "everything touching a cart or an order needs a customer token."
        ),
    },
    {
        "name": "admin",
        "description": (
            "Staff console: order confirmation and fulfilment, product and coupon "
            "management, the agent conversation queue, and catalogue sync to Meta. "
            "Requires an agent token; a subset requires an admin role."
        ),
    },
    {
        "name": "webhooks",
        "description": (
            "Inbound callbacks from Meta and Cashfree. Not for client use. Every "
            "request is signature-verified, deduplicated by event id, and answered "
            "immediately — the real work happens on the background queue, because "
            "both providers retry anything slow."
        ),
    },
    {
        "name": "health",
        "description": (
            "`/health` is liveness and never touches a dependency. `/health/ready` "
            "is readiness and probes PostgreSQL and Redis, returning 503 with "
            "per-component detail when one is down. Point your load balancer at "
            "the latter."
        ),
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level, json_output=settings.is_production)
    log.info(
        "app_started",
        environment=settings.environment,
        docs_enabled=not settings.is_production,
    )
    yield
    # Connections are closed on the way out so a reload or a rolling deploy does
    # not leak pooled sockets.
    await dispose_engine()
    await close_arq()
    await close_redis()
    log.info("app_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=API_DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        servers=[
            {"url": settings.api_base_url, "description": settings.environment},
        ],
        contact={"name": settings.seller_legal_name},
        # Schema endpoints are public, so they stay off in production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.state.limiter = limiter

    # See the module docstring: added first == outermost == runs last.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
        expose_headers=["X-Correlation-ID"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(ContentTypeMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    _register_error_handlers(app)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(store.router)
    app.include_router(admin.router)
    app.include_router(webhook.router)

    setup_metrics(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        # Domain errors are expected control flow, not incidents: log them at
        # warning without a stack trace so real 500s stay visible.
        log.warning(
            "domain_error",
            code=exc.code,
            status_code=exc.status_code,
            path=request.url.path,
            message=exc.message,
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return await rate_limit_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Request validation failed",
                "code": "validation_error",
                "errors": _clean_validation_errors(exc),
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": _HTTP_CODES.get(exc.status_code, "error")},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", path=request.url.path, error=str(exc))
        # The message is deliberately generic — an exception string can carry a
        # query fragment or a connection URI, and this response is public.
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "code": "internal_error"},
        )


def _clean_validation_errors(exc: RequestValidationError) -> list[dict]:
    """Strip pydantic's `ctx`/`input`, which can echo back submitted secrets."""
    cleaned = []
    for err in exc.errors():
        cleaned.append(
            {
                "field": ".".join(str(p) for p in err.get("loc", ())),
                "message": err.get("msg", "invalid"),
                "type": err.get("type", "value_error"),
            }
        )
    return cleaned


_HTTP_CODES = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
}


app = create_app()
