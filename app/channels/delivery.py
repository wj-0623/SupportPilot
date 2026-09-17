from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError
from app.connectors.registry import ProviderRegistry
from app.core.config import Settings
from app.db.models import OutboundMessage
from app.db.platform_repository import PlatformRepository
from app.db.repository import ConflictError
from app.domain.config import DomainPackConfig
from app.metrics import OUTBOUND_DELIVERIES

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueueResult:
    message: OutboundMessage | None
    reason: str


class ChannelDeliveryService:
    def __init__(self, settings: Settings, providers: ProviderRegistry) -> None:
        self.settings = settings
        self.providers = providers

    async def enqueue_for_conversation(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        conversation_id: str,
        content: str,
        idempotency_key: str,
        source_action_id: str | None = None,
        source_message_id: str | None = None,
    ) -> QueueResult:
        repo = PlatformRepository(session)
        mapping = await repo.get_channel_mapping_for_conversation(tenant_id, conversation_id)
        if mapping is None:
            return QueueResult(None, "no_channel_mapping")
        connector = await repo.get_connector(tenant_id, mapping.connector_id)
        capabilities = set(json.loads(connector.capabilities_json))
        if connector.status != "live" or "outbound_chat" not in capabilities:
            return QueueResult(None, "outbound_chat_unavailable")
        existing = await repo.get_outbound_by_key(tenant_id, connector.id, idempotency_key)
        if existing:
            if existing.conversation_id != conversation_id or existing.content != content:
                raise ConflictError(
                    "Outbound idempotency key was reused for different message content"
                )
            return QueueResult(existing, "already_queued")
        message = await repo.enqueue_outbound_message(
            tenant_id=tenant_id,
            connector_id=connector.id,
            conversation_id=conversation_id,
            external_conversation_id=mapping.external_conversation_id,
            content=content,
            idempotency_key=idempotency_key,
            source_action_id=source_action_id,
            source_message_id=source_message_id,
        )
        return QueueResult(message, "queued")

    async def deliver(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        pack: DomainPackConfig,
    ) -> None:
        repo = PlatformRepository(session)
        connector = await repo.get_connector(message.tenant_id, message.connector_id)
        provider = None
        try:
            if connector.status != "live":
                raise ConnectorError(
                    "connector_not_live", "Channel connector is not live", retryable=False
                )
            provider = self.providers.build(
                connector,
                allowed_hosts=pack.safety.allowed_connector_hosts,
                timeout_seconds=self.settings.connector_timeout_seconds,
                max_retries=self.settings.connector_max_retries,
            )
            result = await provider.execute(
                "send_message",
                {
                    "external_conversation_id": message.external_conversation_id,
                    "content": message.content,
                },
                idempotency_key=f"{message.tenant_id}:{message.idempotency_key}",
            )
            await repo.mark_outbound_delivered(message, result.external_id)
            OUTBOUND_DELIVERIES.labels(status="delivered", provider=connector.provider).inc()
        except ConnectorError as exc:
            await repo.mark_outbound_failed(
                message,
                exc.code,
                max_attempts=self.settings.outbound_max_attempts,
                retryable=exc.retryable,
            )
            OUTBOUND_DELIVERIES.labels(status=message.status, provider=connector.provider).inc()
        except Exception as exc:
            logger.exception("Unexpected channel delivery failure")
            await repo.mark_outbound_failed(
                message,
                type(exc).__name__,
                max_attempts=self.settings.outbound_max_attempts,
                retryable=False,
            )
            OUTBOUND_DELIVERIES.labels(status=message.status, provider=connector.provider).inc()
        finally:
            if provider is not None:
                try:
                    await provider.close()
                except Exception:
                    logger.exception("Failed to close channel provider")
