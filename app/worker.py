from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select

from app.actions.service import ActionService
from app.channels.delivery import ChannelDeliveryService
from app.connectors.credentials import EnvironmentCredentialResolver
from app.connectors.registry import ProviderRegistry
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.database import Database
from app.db.models import ActionExecution
from app.db.platform_repository import PlatformRepository
from app.db.repository import SupportRepository
from app.domain.config import DomainPackConfig

logger = logging.getLogger(__name__)


ACTION_LABELS = {
    "cancel_order": "取消订单",
    "change_address": "修改地址",
    "create_return": "申请退货",
    "exchange_item": "申请换货",
    "handoff": "转接人工",
}


async def process_once(
    database: Database,
    service: ActionService,
    delivery: ChannelDeliveryService | None = None,
) -> int:
    delivery = delivery or ChannelDeliveryService(service.settings, service.providers)
    processed = 0
    async with database.sessions() as session:
        repo = PlatformRepository(session)
        stale_action_before = datetime.now(UTC) - timedelta(
            seconds=service.settings.action_lease_seconds
        )
        actions = list(
            await session.scalars(
                select(ActionExecution)
                .where(
                    or_(
                        and_(
                            ActionExecution.status.in_(["queued", "retryable"]),
                            or_(
                                ActionExecution.next_attempt_at.is_(None),
                                ActionExecution.next_attempt_at <= datetime.now(UTC),
                            ),
                        ),
                        and_(
                            ActionExecution.status == "executing",
                            ActionExecution.updated_at <= stale_action_before,
                        ),
                    ),
                )
                .order_by(ActionExecution.created_at)
                .limit(25)
                .with_for_update(skip_locked=True)
            )
        )
        for action in actions:
            if action.status == "executing":
                action.status = "retryable"
            revision = await repo.get_live_domain_pack(action.tenant_id)
            pack = DomainPackConfig.model_validate_json(revision.config_json)
            await service.execute_queued(
                session, tenant_id=action.tenant_id, action_id=action.id, pack=pack
            )
            await repo.mark_action_outbox_delivered(action.tenant_id, action.id)
            if action.conversation_id and action.status in {"succeeded", "failed"}:
                label = ACTION_LABELS.get(action.action, action.action)
                if action.status == "succeeded":
                    content = f"你的{label}请求已处理完成。"
                else:
                    content = f"你的{label}请求暂未完成，已转交人工客服继续处理。"
                    connector = await repo.get_connector(action.tenant_id, action.connector_id)
                    await SupportRepository(session).create_ticket(
                        conversation_id=action.conversation_id,
                        customer_id=action.customer_id,
                        reason=f"执行 {action.action} 失败：{action.error_code or 'unknown'}",
                        channel=connector.provider,
                    )
                await delivery.enqueue_for_conversation(
                    session,
                    tenant_id=action.tenant_id,
                    conversation_id=action.conversation_id,
                    content=content,
                    idempotency_key=f"action-result:{action.id}:{action.status}",
                    source_action_id=action.id,
                )
            processed += 1
        outbound = await repo.claim_outbound_messages(
            limit=25, lease_seconds=service.settings.outbound_lease_seconds
        )
        for message in outbound:
            revision = await repo.get_live_domain_pack(message.tenant_id)
            pack = DomainPackConfig.model_validate_json(revision.config_json)
            await delivery.deliver(session, message, pack)
            processed += 1
        await session.commit()
    return processed


async def run_worker() -> None:
    settings = get_settings()
    settings.validate_runtime()
    configure_logging(settings.log_level)
    database = Database(settings.database_url)
    service = ActionService(settings, ProviderRegistry(EnvironmentCredentialResolver()))
    delivery = ChannelDeliveryService(settings, service.providers)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, stopping.set)
    logger.info("Action worker started")
    try:
        while not stopping.is_set():
            count = await process_once(database, service, delivery)
            if count == 0:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stopping.wait(), timeout=1.0)
    finally:
        await database.dispose()
        logger.info("Action worker stopped")


if __name__ == "__main__":
    asyncio.run(run_worker())
