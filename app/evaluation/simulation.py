from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.agent.graph import SupportGraph
from app.core.config import Settings
from app.db.database import Database
from app.db.models import DomainPackRevision
from app.db.seed import seed_demo_data
from app.knowledge import KnowledgeBase
from app.schemas import ChatRequest, SimulationCaseRequest
from app.service import SupportService


async def simulate_domain_pack(
    settings: Settings,
    pack_json: str,
    cases: list[SimulationCaseRequest],
) -> list[dict[str, Any]]:
    sandbox_settings = settings.model_copy(
        update={
            "app_env": "test",
            "auth_mode": "development",
            "database_url": "sqlite+aiosqlite:///:memory:",
            "llm_enabled": False,
            "openai_api_key": None,
            "expose_debug_trace": True,
            "redis_url": None,
        }
    )
    database = Database(sandbox_settings.database_url)
    await database.create_schema()
    await seed_demo_data(database, sandbox_settings)
    async with database.sessions() as session:
        revision = await session.scalar(
            select(DomainPackRevision).where(
                DomainPackRevision.tenant_id == sandbox_settings.default_tenant_id,
                DomainPackRevision.status == "live",
            )
        )
        if revision is None:
            raise RuntimeError("Sandbox seed did not create a live Domain Pack")
        revision.config_json = pack_json
        await session.commit()
    service = SupportService(
        SupportGraph(
            sandbox_settings,
            KnowledgeBase(sandbox_settings.knowledge_base_path, sandbox_settings.catalog_path),
        ),
        sandbox_settings,
    )
    results: list[dict[str, Any]] = []
    try:
        for case in cases:
            conversation_id: str | None = None
            turns: list[dict[str, Any]] = []
            async with database.sessions() as session:
                for message in case.turns:
                    response = await service.chat(
                        session,
                        ChatRequest(
                            customer_id=case.customer_id,
                            conversation_id=conversation_id,
                            message=message,
                        ),
                        None,
                        tenant_id=sandbox_settings.default_tenant_id,
                        customer_id=case.customer_id,
                    )
                    conversation_id = response.conversation_id
                    turns.append(response.model_dump(mode="json"))
            results.append({"id": case.id, "turns": turns})
    finally:
        await database.dispose()
    return results
