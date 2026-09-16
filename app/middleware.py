from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from uuid import uuid4

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.db.platform_repository import PlatformRepository
from app.metrics import AUDIT_EVENTS, HTTP_LATENCY, HTTP_REQUESTS

logger = logging.getLogger(__name__)


class RequestSizeLimitMiddleware:
    """Reject oversized HTTP bodies before routing or JSON parsing."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            content_length = self.max_bytes + 1
        if content_length > self.max_bytes:
            await self._reject(scope, receive, send)
            return
        messages: list[Message] = []
        received = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break
        iterator = iter(messages)

        async def replay() -> Message:
            try:
                return next(iterator)
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = Response(
            content='{"detail":"Request body is too large"}',
            status_code=413,
            media_type="application/json",
        )
        await response(scope, receive, send)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid4()))[:128]
        request.state.request_id = request_id
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        route = request.scope.get("route")
        path_template = getattr(route, "path", request.url.path)
        HTTP_REQUESTS.labels(request.method, path_template, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, path_template).observe(elapsed)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response


class AuditMiddleware(BaseHTTPMiddleware):
    """Record privileged mutations without storing request bodies or credentials."""

    AUDITED_PREFIXES = (
        "/api/v1/admin/",
        "/api/v1/actions",
        "/api/v1/ops/",
        "/api/v1/tickets/",
    )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if request.method not in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        } or not request.url.path.startswith(self.AUDITED_PREFIXES):
            return response
        principal = getattr(request.state, "principal", None)
        database = getattr(request.app.state, "database", None)
        if principal is None or database is None:
            return response
        try:
            async with database.sessions() as session:
                await PlatformRepository(session).record_audit(
                    tenant_id=principal.tenant_id,
                    actor=principal.subject,
                    role=principal.role,
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                    request_id=getattr(request.state, "request_id", None),
                    resource_id=_resource_hint(request.url.path),
                )
                await session.commit()
            AUDIT_EVENTS.labels(status="persisted").inc()
        except Exception:
            AUDIT_EVENTS.labels(status="failed").inc()
            logger.exception("Failed to persist API audit event")
        return response


def _resource_hint(path: str) -> str | None:
    ignored = {
        "activate",
        "approve",
        "confirm",
        "execute",
        "promote",
        "publish",
        "resolve",
        "review",
        "rollback",
        "sync",
    }
    for part in reversed(path.strip("/").split("/")):
        if part not in ignored and part not in {"api", "v1", "admin", "ops", "actions", "tickets"}:
            return part[:200]
    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, requests_per_minute: int) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self.limit = requests_per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = asyncio.Lock()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        coordinator = getattr(request.app.state, "coordinator", None)
        if coordinator is not None:
            if not await coordinator.allow(client, self.limit):
                return Response(
                    content='{"detail":"Rate limit exceeded"}',
                    status_code=429,
                    media_type="application/json",
                    headers={"Retry-After": "60"},
                )
            return await call_next(request)
        now = time.monotonic()
        async with self.lock:
            hits = self.hits[client]
            while hits and hits[0] <= now - 60:
                hits.popleft()
            if len(hits) >= self.limit:
                return Response(
                    content='{"detail":"Rate limit exceeded"}',
                    status_code=429,
                    media_type="application/json",
                    headers={"Retry-After": "60"},
                )
            hits.append(now)
        return await call_next(request)
