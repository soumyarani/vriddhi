"""Redis cache helpers.

All reads fail open: if Redis is unavailable the caller falls through to the
database rather than erroring, so a cache outage degrades latency, not uptime.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, TypeVar

from app.redis import get_redis
from logging_config import get_logger

log = get_logger(__name__)

T = TypeVar("T")

# TTLs in seconds
TTL_CATEGORIES = 300
TTL_PRODUCT_DETAIL = 120
TTL_PRODUCT_LIST = 60
TTL_AI_CATALOG = 300
TTL_RATING = 600


class CacheKeys:
    CATEGORIES = "cache:categories"
    PRODUCT_DETAIL = "cache:product:{product_id}"
    PRODUCT_LIST = "cache:products:{fingerprint}"
    AI_CATALOG = "cache:ai:catalog"
    PRODUCT_RATING = "cache:rating:{product_id}"
    AI_CIRCUIT = "circuit:openai"
    AI_FAILURES = "circuit:openai:failures"
    REVOKED_REFRESH = "auth:revoked:{lookup_hash}"
    CATALOG_VERSION = "cache:catalog:version"


def _default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Not JSON serializable: {type(obj)}")


async def cache_get(key: str) -> Any | None:
    try:
        raw = await get_redis().get(key)
    except Exception as exc:
        log.warning("cache_get_failed", key=key, error=str(exc))
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def cache_set(key: str, value: Any, ttl: int) -> None:
    try:
        await get_redis().set(key, json.dumps(value, default=_default), ex=ttl)
    except Exception as exc:
        log.warning("cache_set_failed", key=key, error=str(exc))


async def cache_delete(*keys: str) -> None:
    if not keys:
        return
    try:
        await get_redis().delete(*keys)
    except Exception as exc:
        log.warning("cache_delete_failed", error=str(exc))


async def cache_delete_pattern(pattern: str) -> None:
    """SCAN-based deletion — never KEYS, which blocks the Redis event loop."""
    try:
        redis = get_redis()
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=500)
            if keys:
                await redis.delete(*keys)
            if cursor == 0:
                break
    except Exception as exc:
        log.warning("cache_delete_pattern_failed", pattern=pattern, error=str(exc))


async def cached(key: str, ttl: int, loader: Callable[[], Awaitable[T]]) -> T:
    hit = await cache_get(key)
    if hit is not None:
        return hit
    value = await loader()
    await cache_set(key, value, ttl)
    return value


async def invalidate_catalog(product_id: int | None = None) -> None:
    """Drop every cache entry that could embed stale product data."""
    keys = [CacheKeys.CATEGORIES, CacheKeys.AI_CATALOG]
    if product_id is not None:
        keys.append(CacheKeys.PRODUCT_DETAIL.format(product_id=product_id))
        keys.append(CacheKeys.PRODUCT_RATING.format(product_id=product_id))
    await cache_delete(*keys)
    await cache_delete_pattern("cache:products:*")


async def invalidate_product_rating(product_id: int) -> None:
    await cache_delete(CacheKeys.PRODUCT_RATING.format(product_id=product_id))
