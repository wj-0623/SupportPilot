from __future__ import annotations

from typing import Any

import httpx

from app.connectors.base import ConnectorError, ConnectorResult
from app.connectors.generic_rest import validate_connector_url


class ChatwootHelpdeskProvider:
    """Reference human-handoff adapter for a Chatwoot account inbox."""

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        account_id: int,
        inbox_id: int,
        allowed_hosts: list[str],
        timeout_seconds: float = 10,
    ) -> None:
        self.base_url = validate_connector_url(base_url, allowed_hosts)
        self.account_id = account_id
        self.inbox_id = inbox_id
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"api_access_token": api_token, "Accept": "application/json"},
            follow_redirects=False,
        )

    async def create_handoff(
        self,
        *,
        customer_external_id: str,
        name: str,
        summary: str,
        priority: str,
        custom_attributes: dict[str, Any],
    ) -> ConnectorResult:
        try:
            contact = await self.client.post(
                f"{self.base_url}api/v1/accounts/{self.account_id}/contacts",
                json={
                    "inbox_id": self.inbox_id,
                    "identifier": customer_external_id,
                    "name": name,
                },
            )
            contact.raise_for_status()
            contact_id = contact.json()["payload"]["contact"]["id"]
            source_id = f"shopsage-{customer_external_id}"
            conversation = await self.client.post(
                f"{self.base_url}api/v1/accounts/{self.account_id}/conversations",
                json={
                    "source_id": source_id,
                    "inbox_id": self.inbox_id,
                    "contact_id": contact_id,
                    "status": "open",
                    "custom_attributes": {**custom_attributes, "priority": priority},
                    "message": {"content": summary},
                },
            )
            conversation.raise_for_status()
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise ConnectorError(
                "chatwoot_handoff_failed", "Chatwoot handoff failed", retryable=True
            ) from exc
        data = conversation.json()
        return ConnectorResult(data, external_id=str(data.get("id", "")) or None)

    async def healthcheck(self) -> bool:
        try:
            response = await self.client.get(
                f"{self.base_url}api/v1/accounts/{self.account_id}/inboxes"
            )
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
        if action != "handoff":
            raise ConnectorError("unsupported_action", f"Chatwoot does not support {action}")
        attributes = dict(payload.get("custom_attributes", {}))
        attributes["shopsage_idempotency_key"] = idempotency_key
        return await self.create_handoff(
            customer_external_id=str(payload["customer_id"]),
            name=str(payload.get("customer_name", payload["customer_id"])),
            summary=str(payload["summary"]),
            priority=str(payload.get("priority", "normal")),
            custom_attributes=attributes,
        )

    async def close(self) -> None:
        await self.client.aclose()
