from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ConnectorError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ConnectorResult:
    data: dict[str, Any]
    external_id: str | None = None


class CommerceProvider(Protocol):
    async def execute(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ConnectorResult: ...

    async def healthcheck(self) -> bool: ...

    async def close(self) -> None: ...
