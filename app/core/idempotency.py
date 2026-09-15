from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.coordination import RedisCoordinator


class IdempotencyCoordinator:
    def __init__(self, distributed: RedisCoordinator | None) -> None:
        self.distributed = distributed
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._guard = asyncio.Lock()

    @asynccontextmanager
    async def lock(self, tenant_id: str, key: str) -> AsyncIterator[bool]:
        scoped = f"request:{tenant_id}:{key}"
        if self.distributed:
            async with self.distributed.lock(scoped, ttl_seconds=180) as acquired:
                yield acquired
            return
        async with self._guard:
            lock = self._locks.setdefault(scoped, asyncio.Lock())
        async with lock:
            yield True
