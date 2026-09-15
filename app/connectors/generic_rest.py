from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.connectors.base import ConnectorError, ConnectorResult


@dataclass(frozen=True)
class RestAction:
    method: str
    path: str


def validate_connector_url(base_url: str, allowed_hosts: list[str]) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("connector base_url must be an HTTPS origin without embedded credentials")
    host = parsed.hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local):
        raise ValueError("private connector addresses are forbidden")
    normalized_allowlist = {item.lower().rstrip(".") for item in allowed_hosts}
    if normalized_allowlist and host not in normalized_allowlist:
        raise ValueError("connector host is not on the tenant allowlist")
    return base_url.rstrip("/") + "/"


class GenericRestCommerceProvider:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        actions: dict[str, RestAction],
        allowed_hosts: list[str],
        timeout_seconds: float = 10,
        max_retries: int = 2,
    ) -> None:
        self.base_url = validate_connector_url(base_url, allowed_hosts)
        self.actions = actions
        self.max_retries = max_retries
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            follow_redirects=False,
        )

    async def healthcheck(self) -> bool:
        try:
            response = await self.client.get(urljoin(self.base_url, "health"))
            return response.status_code < 500
        except httpx.HTTPError:
            return False

    async def execute(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ConnectorResult:
        spec = self.actions.get(action)
        if spec is None:
            raise ConnectorError("unsupported_action", f"Connector does not support {action}")
        url = urljoin(self.base_url, spec.path.lstrip("/"))
        headers = {"Idempotency-Key": idempotency_key}
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.client.request(
                    spec.method.upper(), url, json=payload, headers=headers
                )
                if response.status_code in {429, 502, 503, 504}:
                    raise ConnectorError(
                        f"http_{response.status_code}",
                        "Transient connector failure",
                        retryable=True,
                    )
                if response.status_code >= 400:
                    raise ConnectorError(
                        f"http_{response.status_code}", "Connector rejected the action"
                    )
                body = response.json()
                if not isinstance(body, dict):
                    raise ConnectorError("invalid_response", "Connector returned a non-object")
                return ConnectorResult(body, external_id=response.headers.get("X-Resource-Id"))
            except (httpx.TimeoutException, httpx.NetworkError, ConnectorError) as exc:
                last_error = exc
                retryable = not isinstance(exc, ConnectorError) or exc.retryable
                if not retryable or attempt >= self.max_retries:
                    break
                await asyncio.sleep(0.1 * (2**attempt))
        if isinstance(last_error, ConnectorError):
            raise last_error
        raise ConnectorError("network_error", "Connector network request failed", retryable=True)

    async def close(self) -> None:
        await self.client.aclose()
