from __future__ import annotations

from collections import defaultdict
from time import time

import redis

from app.config import settings


class InMemoryRedis:
    def __init__(self) -> None:
        self._kv: dict[str, tuple[str, float | None]] = {}
        self._counts: dict[str, tuple[int, float]] = {}

    def get(self, key: str):
        value = self._kv.get(key)
        if not value:
            return None
        data, expires = value
        if expires is not None and expires < time():
            self._kv.pop(key, None)
            return None
        return data.encode("utf-8")

    def setex(self, key: str, ttl_seconds: int, value: str) -> bool:
        self._kv[key] = (value, time() + ttl_seconds)
        return True

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self._kv:
                del self._kv[key]
                deleted += 1
        return deleted

    def incr_with_ttl(self, key: str, ttl_seconds: int) -> int:
        count, expires = self._counts.get(key, (0, 0.0))
        now = time()
        if expires <= now:
            count = 0
            expires = now + ttl_seconds
        count += 1
        self._counts[key] = (count, expires)
        return count


_client = None


def get_redis_client():
    global _client
    if _client is not None:
        return _client
    if settings.redis_url:
        _client = redis.Redis.from_url(settings.redis_url, decode_responses=False)
    else:
        _client = InMemoryRedis()
    return _client
