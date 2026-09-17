from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from redis.asyncio import Redis


class RedisCoordinator:
    def __init__(self, url: str) -> None:
        self.client = Redis.from_url(url, decode_responses=True)

    async def ready(self) -> bool:
        return bool(await self.client.ping())

    async def close(self) -> None:
        await self.client.aclose()

    @asynccontextmanager
    async def lock(self, key: str, ttl_seconds: int = 30) -> AsyncIterator[bool]:
        token = str(uuid4())
        acquired = bool(
            await self.client.set(f"supportpilot:lock:{key}", token, nx=True, ex=ttl_seconds)
        )
        try:
            yield acquired
        finally:
            if acquired:
                await self.client.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('del', KEYS[1]) else return 0 end",
                    1,
                    f"supportpilot:lock:{key}",
                    token,
                )

    async def allow(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        redis_key = f"supportpilot:rate:{key}"
        count = await self.client.eval(
            "local count = redis.call('incr', KEYS[1]); "
            "if count == 1 then redis.call('expire', KEYS[1], ARGV[1]); end; "
            "return count",
            1,
            redis_key,
            window_seconds,
        )
        return int(count) <= limit
