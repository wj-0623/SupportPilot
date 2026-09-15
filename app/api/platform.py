from __future__ import annotations

import hashlib
import json
import time
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.service import ActionService
from app.connectors.credentials import EnvironmentCredentialResolver
from app.core.config import Settings
from app.core.security import (
    Principal,
    principal_dependency,
    verify_webhook_signature,
)
from app.db.models import ActionExecution, ConnectorDefinition, DomainPackRevision, KnowledgeSource
from app.db.platform_repository import PlatformRepository
from app.db.repository import ConflictError, NotFoundError
from app.domain.config import DomainPackConfig
from app.domain.guardrails import inspect_message
from app.domain.registry import DomainPackRegistry
from app.evaluation.simulation import simulate_domain_pack
from app.knowledge_ingestion import sync_knowledge
from app.schemas import (
    ActionConfirmRequest,
    ActionPlanRequest,
    ActionResponse,
    ConnectorCreateRequest,
    ConnectorResponse,
    DomainPackRevisionResponse,
    KnowledgeSourceCreateRequest,
    KnowledgeSourceResponse,
    KnowledgeSyncRequest,
    ReleaseCreateRequest,
    ResolutionOutcomeRequest,
    SimulationRequest,
)


def _domain_response(revision: DomainPackRevision) -> DomainPackRevisionResponse:
    return DomainPackRevisionResponse(
        id=revision.id,
        tenant_id=revision.tenant_id,
        slug=revision.slug,
        version=revision.version,
        status=revision.status,
        checksum=revision.checksum,
        config=json.loads(revision.config_json),
        created_by=revision.created_by,
        created_at=revision.created_at,
        published_at=revision.published_at,
    )


def _connector_response(connector: ConnectorDefinition) -> ConnectorResponse:
    return ConnectorResponse(
        id=connector.id,
        tenant_id=connector.tenant_id,
        name=connector.name,
        provider=connector.provider,
        version=connector.version,
        status=connector.status,
        base_url=connector.base_url,
        credential_ref=connector.credential_ref,
        capabilities=json.loads(connector.capabilities_json),
        config=json.loads(connector.config_json),
        created_at=connector.created_at,
    )


def _action_response(execution: ActionExecution) -> ActionResponse:
    return ActionResponse(
        id=execution.id,
        tenant_id=execution.tenant_id,
        customer_id=execution.customer_id,
        conversation_id=execution.conversation_id,
        connector_id=execution.connector_id,
        action=execution.action,
        risk_tier=execution.risk_tier,
        status=execution.status,
        idempotency_key=execution.idempotency_key,
        parameters=json.loads(execution.request_json),
        result=json.loads(execution.result_json) if execution.result_json else None,
        error_code=execution.error_code,
        confirmation_digest=execution.confirmation_digest,
        confirmed_by=execution.confirmed_by,
        created_at=execution.created_at,
        updated_at=execution.updated_at,
    )


def _knowledge_response(source: KnowledgeSource) -> KnowledgeSourceResponse:
    return KnowledgeSourceResponse(
        id=source.id,
        tenant_id=source.tenant_id,
        source_key=source.source_key,
        source_type=source.source_type,
        version=source.version,
        status=source.status,
        location=source.location,
        checksum=source.checksum,
        injection_flags=json.loads(source.injection_flags_json),
        synced_at=source.synced_at,
        published_at=source.published_at,
    )


def create_platform_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    customer_auth = principal_dependency(settings, "customer", "agent", "admin")
    staff_auth = principal_dependency(settings, "agent", "admin")
    admin_auth = principal_dependency(settings, "admin")

    async def get_session(request: Request):  # type: ignore[no-untyped-def]
        async with request.app.state.database.sessions() as session:
            yield session

    async def live_pack(session: AsyncSession, tenant_id: str) -> DomainPackConfig:
        revision = await PlatformRepository(session).get_live_domain_pack(tenant_id)
        return DomainPackConfig.model_validate_json(revision.config_json)

    @router.get("/admin/domain-packs/templates", tags=["domain-packs"])
    async def list_templates(
        request: Request, principal: Principal = Depends(staff_auth)
    ) -> list[dict[str, Any]]:
        del principal
        registry: DomainPackRegistry = request.app.state.domain_registry
        return [
            {
                "slug": pack.slug,
                "name": pack.name,
                "vertical": pack.vertical,
                "checksum": pack.checksum(),
                "config": pack.model_dump(mode="json"),
            }
            for pack in registry.list()
        ]

    @router.get(
        "/admin/domain-packs",
        response_model=list[DomainPackRevisionResponse],
        tags=["domain-packs"],
    )
    async def list_domain_packs(
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(staff_auth),
    ) -> list[DomainPackRevisionResponse]:
        revisions = await PlatformRepository(session).list_domain_packs(principal.tenant_id)
        return [_domain_response(item) for item in revisions]

    @router.post(
        "/admin/domain-packs",
        response_model=DomainPackRevisionResponse,
        status_code=201,
        tags=["domain-packs"],
    )
    async def create_domain_pack(
        pack: DomainPackConfig,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(staff_auth),
    ) -> DomainPackRevisionResponse:
        revision = await PlatformRepository(session).create_domain_pack_draft(
            principal.tenant_id, pack, principal.subject
        )
        await session.commit()
        return _domain_response(revision)

    @router.post(
        "/admin/domain-packs/{revision_id}/publish",
        response_model=DomainPackRevisionResponse,
        tags=["domain-packs"],
    )
    async def publish_domain_pack(
        revision_id: str,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> DomainPackRevisionResponse:
        try:
            revision = await PlatformRepository(session).publish_domain_pack(
                principal.tenant_id, revision_id
            )
            await session.commit()
        except (NotFoundError, ConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _domain_response(revision)

    @router.get("/admin/connectors", response_model=list[ConnectorResponse], tags=["connectors"])
    async def list_connectors(
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(staff_auth),
    ) -> list[ConnectorResponse]:
        connectors = await PlatformRepository(session).list_connectors(principal.tenant_id)
        return [_connector_response(item) for item in connectors]

    @router.post(
        "/admin/connectors",
        response_model=ConnectorResponse,
        status_code=201,
        tags=["connectors"],
    )
    async def create_connector(
        payload: ConnectorCreateRequest,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> ConnectorResponse:
        if payload.provider != "mock" and not payload.credential_ref:
            raise HTTPException(
                status_code=422, detail="External connectors require credential_ref"
            )
        connector = await PlatformRepository(session).create_connector(
            tenant_id=principal.tenant_id,
            name=payload.name,
            provider=payload.provider,
            base_url=payload.base_url,
            credential_ref=payload.credential_ref,
            capabilities=payload.capabilities,
            config=payload.config,
        )
        await session.commit()
        return _connector_response(connector)

    @router.post(
        "/admin/connectors/{connector_id}/activate",
        response_model=ConnectorResponse,
        tags=["connectors"],
    )
    async def activate_connector(
        request: Request,
        connector_id: str,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> ConnectorResponse:
        repo = PlatformRepository(session)
        connector = await repo.get_connector(principal.tenant_id, connector_id)
        pack = await live_pack(session, principal.tenant_id)
        if connector.provider != "mock" and not pack.safety.allowed_connector_hosts:
            raise HTTPException(
                status_code=409,
                detail="Live Domain Pack must allowlist every external connector host",
            )
        try:
            provider = request.app.state.provider_registry.build(
                connector,
                allowed_hosts=pack.safety.allowed_connector_hosts,
                timeout_seconds=settings.connector_timeout_seconds,
                max_retries=settings.connector_max_retries,
            )
            if not await provider.healthcheck():
                raise ConflictError("Connector health check failed")
            connector = await repo.activate_connector(principal.tenant_id, connector_id)
            await session.commit()
        except (ValueError, RuntimeError, ConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _connector_response(connector)

    @router.post("/actions", response_model=ActionResponse, status_code=201, tags=["actions"])
    async def plan_action(
        request: Request,
        payload: ActionPlanRequest,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(customer_auth),
        idempotency_key: Annotated[str, Header(min_length=8, max_length=128)] = "",
    ) -> ActionResponse:
        if not idempotency_key:
            raise HTTPException(status_code=422, detail="Idempotency-Key is required")
        pack = await live_pack(session, principal.tenant_id)
        service: ActionService = request.app.state.action_service
        try:
            execution = await service.plan(
                session,
                principal=principal,
                pack=pack,
                connector_id=payload.connector_id,
                action=payload.action,
                request=payload.parameters,
                idempotency_key=idempotency_key,
                conversation_id=payload.conversation_id,
            )
            await session.commit()
        except (ConflictError, NotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _action_response(execution)

    @router.post("/actions/{action_id}/confirm", response_model=ActionResponse, tags=["actions"])
    async def confirm_action(
        request: Request,
        action_id: str,
        payload: ActionConfirmRequest,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(customer_auth),
    ) -> ActionResponse:
        try:
            execution = await request.app.state.action_service.confirm(
                session,
                principal=principal,
                action_id=action_id,
                digest=payload.confirmation_digest,
            )
            await session.commit()
        except (ConflictError, NotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _action_response(execution)

    @router.post(
        "/admin/actions/{action_id}/approve", response_model=ActionResponse, tags=["actions"]
    )
    async def approve_action(
        request: Request,
        action_id: str,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(staff_auth),
    ) -> ActionResponse:
        try:
            execution = await request.app.state.action_service.approve(
                session, principal=principal, action_id=action_id
            )
            await session.commit()
        except (ConflictError, NotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _action_response(execution)

    @router.post(
        "/admin/actions/{action_id}/execute", response_model=ActionResponse, tags=["actions"]
    )
    async def execute_action(
        request: Request,
        action_id: str,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(staff_auth),
    ) -> ActionResponse:
        pack = await live_pack(session, principal.tenant_id)
        try:
            execution = await request.app.state.action_service.execute_queued(
                session, tenant_id=principal.tenant_id, action_id=action_id, pack=pack
            )
            await session.commit()
        except (ConflictError, NotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _action_response(execution)

    @router.post("/admin/knowledge", response_model=KnowledgeSourceResponse, tags=["knowledge"])
    async def create_knowledge(
        payload: KnowledgeSourceCreateRequest,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(staff_auth),
    ) -> KnowledgeSourceResponse:
        inspection = inspect_message(payload.content)
        flags = [reason for reason in inspection.reasons if reason == "prompt_injection"]
        source = await PlatformRepository(session).create_knowledge_source(
            tenant_id=principal.tenant_id,
            source_key=payload.source_key,
            source_type=payload.source_type,
            location=payload.location,
            content=inspection.sanitized_text,
            metadata=payload.metadata,
            injection_flags=flags,
        )
        await session.commit()
        return _knowledge_response(source)

    @router.post(
        "/admin/knowledge/{source_id}/publish",
        response_model=KnowledgeSourceResponse,
        tags=["knowledge"],
    )
    async def publish_knowledge(
        source_id: str,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> KnowledgeSourceResponse:
        try:
            source = await PlatformRepository(session).publish_knowledge(
                principal.tenant_id, source_id
            )
            await session.commit()
        except (ConflictError, NotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _knowledge_response(source)

    @router.post(
        "/admin/knowledge/sync", response_model=KnowledgeSourceResponse, tags=["knowledge"]
    )
    async def sync_knowledge_source(
        payload: KnowledgeSyncRequest,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(staff_auth),
    ) -> KnowledgeSourceResponse:
        pack = await live_pack(session, principal.tenant_id)
        try:
            content = await sync_knowledge(
                payload.location,
                payload.source_type,
                pack.safety.allowed_connector_hosts,
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        inspection = inspect_message(content)
        flags = [reason for reason in inspection.reasons if reason == "prompt_injection"]
        source = await PlatformRepository(session).create_knowledge_source(
            tenant_id=principal.tenant_id,
            source_key=payload.source_key,
            source_type=payload.source_type,
            location=payload.location,
            content=inspection.sanitized_text,
            metadata=payload.metadata,
            injection_flags=flags,
        )
        await session.commit()
        return _knowledge_response(source)

    @router.post("/admin/releases", status_code=201, tags=["releases"])
    async def create_release(
        payload: ReleaseCreateRequest,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> dict[str, Any]:
        revision = await session.get(DomainPackRevision, payload.domain_pack_revision_id)
        if revision is None or revision.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="Domain Pack revision not found")
        artifact = {
            "domain_pack_checksum": revision.checksum,
            "prompt_version": "support-v3",
            "router_version": "hybrid-router-v3",
            "policy_version": "domain-pack-v3",
        }
        try:
            release = await PlatformRepository(session).create_release(
                tenant_id=principal.tenant_id,
                domain_pack_revision_id=payload.domain_pack_revision_id,
                artifact=artifact,
                evaluation=payload.evaluation,
                canary_percent=payload.canary_percent,
            )
            await session.commit()
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": release.id, "version": release.version, "status": release.status, **artifact}

    @router.post("/admin/simulations/chat", tags=["simulations"])
    async def simulate_chat(
        payload: SimulationRequest,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(staff_auth),
    ) -> dict[str, Any]:
        revision = await session.get(DomainPackRevision, payload.domain_pack_revision_id)
        if revision is None or revision.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="Domain Pack revision not found")
        results = await simulate_domain_pack(settings, revision.config_json, payload.cases)
        return {
            "sandbox": True,
            "side_effects": "isolated",
            "domain_pack_revision_id": revision.id,
            "results": results,
        }

    @router.post("/admin/releases/{release_id}/activate", tags=["releases"])
    async def activate_release(
        release_id: str,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> dict[str, Any]:
        try:
            release = await PlatformRepository(session).activate_release(
                principal.tenant_id, release_id, principal.subject
            )
            await session.commit()
        except (ConflictError, NotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": release.id, "version": release.version, "status": release.status}

    @router.post("/admin/releases/{release_id}/promote", tags=["releases"])
    async def promote_release(
        release_id: str,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> dict[str, Any]:
        try:
            release = await PlatformRepository(session).promote_release(
                principal.tenant_id, release_id, principal.subject
            )
            await session.commit()
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": release.id, "version": release.version, "status": release.status}

    @router.post("/admin/releases/{release_id}/rollback", tags=["releases"])
    async def rollback_release(
        release_id: str,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> dict[str, Any]:
        try:
            release = await PlatformRepository(session).rollback_release(
                principal.tenant_id, release_id, principal.subject
            )
            await session.commit()
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": release.id, "version": release.version, "status": release.status}

    @router.post("/outcomes", status_code=201, tags=["outcomes"])
    async def record_outcome(
        payload: ResolutionOutcomeRequest,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(customer_auth),
    ) -> dict[str, Any]:
        customer_id = principal.customer_id
        if customer_id:
            await PlatformRepository(session).get_scoped_conversation(
                principal.tenant_id, customer_id, payload.conversation_id
            )
        outcome = await PlatformRepository(session).record_outcome(
            tenant_id=principal.tenant_id,
            conversation_id=payload.conversation_id,
            run_id=payload.run_id,
            resolved=payload.resolved,
            source="customer" if principal.role == "customer" else "staff",
            first_contact=payload.first_contact,
            reopened=payload.reopened,
        )
        await session.commit()
        return {"id": outcome.id, "resolved": outcome.resolved, "source": outcome.source}

    @router.post("/webhooks/{connector_id}", status_code=202, tags=["webhooks"])
    async def receive_webhook(
        request: Request,
        connector_id: str,
        session: AsyncSession = Depends(get_session),
        x_webhook_signature: Annotated[str, Header(min_length=32, max_length=256)] = "",
        x_webhook_timestamp: Annotated[str, Header(min_length=1, max_length=20)] = "",
        x_event_id: Annotated[str, Header(min_length=1, max_length=200)] = "",
    ) -> dict[str, str]:
        if not x_webhook_signature or not x_webhook_timestamp or not x_event_id:
            raise HTTPException(status_code=401, detail="Signed webhook headers are required")
        try:
            timestamp = int(x_webhook_timestamp)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="Invalid webhook timestamp") from exc
        if abs(int(time.time()) - timestamp) > settings.webhook_tolerance_seconds:
            raise HTTPException(status_code=401, detail="Stale webhook")
        body = await request.body()
        connector = await session.get(ConnectorDefinition, connector_id)
        if connector is None or connector.status != "live" or not connector.credential_ref:
            raise HTTPException(status_code=404, detail="Webhook connector not found")
        connector_config = json.loads(connector.config_json)
        webhook_secret_ref = connector_config.get("webhook_secret_ref")
        if not isinstance(webhook_secret_ref, str):
            raise HTTPException(status_code=404, detail="Webhook secret is not configured")
        secret = EnvironmentCredentialResolver().resolve(webhook_secret_ref)
        valid = verify_webhook_signature(body, x_webhook_signature, secret, x_webhook_timestamp)
        if not valid:
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        event = await PlatformRepository(session).record_webhook(
            tenant_id=connector.tenant_id,
            provider=connector.provider,
            external_id=x_event_id,
            payload_hash=hashlib.sha256(body).hexdigest(),
            signature_valid=True,
        )
        await session.commit()
        return {"id": event.id, "status": event.status}

    return router
