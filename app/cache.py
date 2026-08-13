from __future__ import annotations

import json
from typing import Any

from app.redis import get_redis_client


def cache_get_json(key: str) -> Any | None:
    raw = get_redis_client().get(key)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def cache_set_json(key: str, value: Any, ttl_seconds: int) -> None:
    get_redis_client().setex(key, ttl_seconds, json.dumps(value, default=str))


def cache_delete(*keys: str) -> None:
    if keys:
        get_redis_client().delete(*keys)
