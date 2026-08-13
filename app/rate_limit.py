from __future__ import annotations

from collections import defaultdict

from app.redis import InMemoryRedis, get_redis_client


class RequestRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, int] = defaultdict(int)

    def allow(self, key: str) -> bool:
        client = get_redis_client()
        if isinstance(client, InMemoryRedis):
            self._hits = client._counts
            count = client.incr_with_ttl(key, self.window_seconds)
            return count <= self.max_requests

        current = client.incr(key)
        if current == 1:
            client.expire(key, self.window_seconds)
        return current <= self.max_requests
