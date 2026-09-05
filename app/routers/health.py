"""Liveness, readiness and Prometheus metrics.

These endpoints are deliberately outside `/api` and are listed in
`middleware.EXEMPT_PATHS`, so they are not request-logged and not rate limited:
a probe that can be throttled is a probe that will eventually lie.

Liveness answers "is this process running?" and must never touch a dependency —
if it did, a Redis blip would make Kubernetes restart healthy pods. Readiness
answers "should this replica receive traffic?" and therefore *does* check
Postgres and Redis, returning 503 with per-component detail when one is down.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

from app.config import settings
from app.database import check_database
from app.models.base import utcnow
from app.redis import check_redis
from app.schemas.common import HealthComponent, HealthResponse

router = APIRouter(tags=["health"])

APP_VERSION = "1.0.0"

# A dependency that hangs is as bad as one that is down; readiness must answer
# faster than the orchestrator's probe timeout.
PROBE_TIMEOUT_SECONDS = 3.0


async def _probe(name: str, check) -> HealthComponent:
    """Run one dependency check, timing it and never letting it raise."""
    started = time.perf_counter()
    try:
        healthy = bool(await asyncio.wait_for(check(), timeout=PROBE_TIMEOUT_SECONDS))
        error = None if healthy else "check returned false"
    except asyncio.TimeoutError:
        healthy = False
        error = f"timed out after {PROBE_TIMEOUT_SECONDS}s"
    except Exception as exc:  # pragma: no cover - defensive
        healthy = False
        error = f"{type(exc).__name__}: {exc}"

    return HealthComponent(
        name=name,
        healthy=healthy,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        error=error,
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
)
async def liveness() -> HealthResponse:
    """Cheap, dependency-free. Only proves the event loop is still serving."""
    return HealthResponse(
        status="ok",
        version=APP_VERSION,
        environment=settings.environment,
        checked_at=utcnow(),
        components=[],
    )


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    summary="Readiness probe",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def readiness(response: Response) -> HealthResponse:
    """Check every hard dependency, in parallel, and report each one."""
    components = list(
        await asyncio.gather(
            _probe("postgres", check_database),
            _probe("redis", check_redis),
        )
    )

    degraded = [c for c in components if not c.healthy]
    if degraded:
        # 503 so load balancers pull this replica out of rotation. The body
        # still names the failing component so on-call knows where to look.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if not degraded else "unavailable",
        version=APP_VERSION,
        environment=settings.environment,
        checked_at=utcnow(),
        components=components,
    )


@router.get("/metrics", include_in_schema=False, summary="Prometheus metrics")
async def metrics() -> Response:
    """Render the default Prometheus registry.

    The instrumentator has to wrap the ASGI app itself, which can only happen
    in `main.py` (see `setup_metrics` below). It registers its collectors into
    the global `prometheus_client` REGISTRY, so serving that registry here
    exposes them — plus any other collector the app registers — without this
    router needing a handle on the app object.
    """
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


def setup_metrics(app: Any) -> Any:
    """Attach request instrumentation. Call once from `main.py` at startup.

    Deliberately does *not* call `.expose(app)` — this router already owns
    `GET /metrics`, and exposing twice would register a duplicate route.
    """
    from prometheus_fastapi_instrumentator import Instrumentator

    instrumentator = Instrumentator(
        should_group_status_codes=False,
        should_ignore_untemplated=True,
        # Probes and the metrics scrape itself would otherwise dominate the
        # histograms and tell you nothing about real traffic.
        excluded_handlers=["/metrics", "/health", "/health/ready"],
    )
    instrumentator.instrument(app)
    return instrumentator
