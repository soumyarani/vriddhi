from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.config import settings
from logging_config import get_logger

log = get_logger(__name__)

_pool: aioredis.ConnectionPool | None = None
_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Process-wide Redis client backed by a shared connection pool."""
    global _pool, _client
    if _client is None:
        _pool = aioredis.ConnectionPool.from_url(
            settings.redis_url,
            max_connections=settings.redis_max_connections,
            decode_responses=True,
        )
        _client = aioredis.Redis(connection_pool=_pool)
    return _client


async def close_redis() -> None:
    global _pool, _client
    if _client is not None:
        await _client.aclose()
        _client = None
    if _pool is not None:
        await _pool.disconnect()
        _pool = None


async def check_redis() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False


def arq_redis_settings() -> RedisSettings:
    parsed = urlparse(settings.redis_url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int(parsed.path.lstrip("/") or 0),
        password=parsed.password,
    )


_arq: ArqRedis | None = None


async def get_arq() -> ArqRedis:
    """Queue connection shared by the API process."""
    global _arq
    if _arq is None:
        _arq = await create_pool(arq_redis_settings())
    return _arq


async def close_arq() -> None:
    global _arq
    if _arq is not None:
        await _arq.aclose()
        _arq = None


async def enqueue(job: str, *args: Any, **kwargs: Any) -> bool:
    """Best-effort enqueue.

    Returns False instead of raising: webhook handlers must still ACK when the
    queue is unreachable. Work is not lost — the event row stays `pending` and
    the sweeper cron re-enqueues it.
    """
    try:
        pool = await get_arq()
        await pool.enqueue_job(job, *args, **kwargs)
        return True
    except Exception as exc:
        log.error("enqueue_failed", job=job, error=str(exc))
        return False
