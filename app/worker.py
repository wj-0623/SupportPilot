from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy import or_, select

from app.actions.service import ActionService
from app.connectors.credentials import EnvironmentCredentialResolver
from app.connectors.registry import ProviderRegistry
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.database import Database
from app.db.models import ActionExecution
from app.db.platform_repository import PlatformRepository
from app.domain.config import DomainPackConfig

logger = logging.getLogger(__name__)


async def process_once(database: Database, service: ActionService) -> int:
    processed = 0
    async with database.sessions() as session:
        actions = list(
            await session.scalars(
                select(ActionExecution)
                .where(
                    ActionExecution.status.in_(["queued", "retryable"]),
                    or_(
                        ActionExecution.next_attempt_at.is_(None),
                        ActionExecution.next_attempt_at <= datetime.now(UTC),
                    ),
                )
                .order_by(ActionExecution.created_at)
                .limit(25)
                .with_for_update(skip_locked=True)
            )
        )
        for action in actions:
            revision = await PlatformRepository(session).get_live_domain_pack(action.tenant_id)
            pack = DomainPackConfig.model_validate_json(revision.config_json)
            await service.execute_queued(
                session, tenant_id=action.tenant_id, action_id=action.id, pack=pack
            )
            await PlatformRepository(session).mark_action_outbox_delivered(
                action.tenant_id, action.id
            )
            processed += 1
        await session.commit()
    return processed


async def run_worker() -> None:
    settings = get_settings()
    settings.validate_runtime()
    configure_logging(settings.log_level)
    database = Database(settings.database_url)
    service = ActionService(settings, ProviderRegistry(EnvironmentCredentialResolver()))
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, stopping.set)
    logger.info("Action worker started")
    try:
        while not stopping.is_set():
            count = await process_once(database, service)
            if count == 0:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stopping.wait(), timeout=1.0)
    finally:
        await database.dispose()
        logger.info("Action worker stopped")


if __name__ == "__main__":
    asyncio.run(run_worker())
