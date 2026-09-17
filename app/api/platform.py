from __future__ import annotations

import hashlib
import json
import time
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.service import ActionService
from app.agent.versions import POLICY_VERSION, PROMPT_VERSION, ROUTER_VERSION
from app.connectors.credentials import EnvironmentCredentialResolver
from app.core.config import Settings
from app.core.security import (
    Principal,
    principal_dependency,
    verify_webhook_signature,
)
from app.db.models import (
    ActionExecution,
    ConnectorDefinition,
    DomainPackRevision,
    KnowledgeSource,
    Message,
)
from app.db.platform_repository import PlatformRepository
from app.db.repository import ConflictError, NotFoundError, SupportRepository
from app.domain.config import DomainPackConfig
from app.domain.guardrails import inspect_message
from app.domain.registry import DomainPackRegistry
from app.evaluation.simulation import simulate_domain_pack
from app.knowledge_ingestion import sync_knowledge
from app.schemas import (
    ActionConfirmRequest,
    ActionPlanRequest,
    ActionResponse,
    ChannelMessageRequest,
    ChannelMessageResponse,
    ChatRequest,
    ConnectorCreateRequest,
    ConnectorResponse,
    CustomerMappingRequest,
    CustomerMappingResponse,
    DomainPackRevisionResponse,
    KnowledgeSourceCreateRequest,
    KnowledgeSourceResponse,
    KnowledgeSyncRequest,
    PendingAction,
    ReleaseCreateRequest,
    ResolutionOutcomeRequest,
    SimulationRequest,
    TenantMemberRequest,
    TenantMemberResponse,
)


async def _verify_connector_webhook(
    *,
    request: Request,
    session: AsyncSession,
    connector_id: str,
    signature: str,
    timestamp_text: str,
    settings: Settings,
    required_capability: str | None = None,
) -> tuple[ConnectorDefinition, bytes]:
    if not signature or not timestamp_text:
        raise HTTPException(status_code=401, detail="Signed webhook headers are required")
    try:
        timestamp = int(timestamp_text)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid webhook timestamp") from exc
    if abs(int(time.time()) - timestamp) > settings.webhook_tolerance_seconds:
        raise HTTPException(status_code=401, detail="Stale webhook")
    connector = await session.get(ConnectorDefinition, connector_id)
    if connector is None or connector.status != "live":
        raise HTTPException(status_code=404, detail="Webhook connector not found")
    capabilities = json.loads(connector.capabilities_json)
    if required_capability and required_capability not in capabilities:
        raise HTTPException(status_code=404, detail="Connector capability not found")
    connector_config = json.loads(connector.config_json)
    webhook_secret_ref = connector_config.get("webhook_secret_ref")
    if not isinstance(webhook_secret_ref, str):
        raise HTTPException(status_code=404, detail="Webhook secret is not configured")
    secret = EnvironmentCredentialResolver().resolve(webhook_secret_ref)
    body = await request.body()
    if not verify_webhook_signature(body, signature, secret, timestamp_text):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    return connector, body


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

    @router.get(
        "/admin/customer-mappings",
        response_model=list[CustomerMappingResponse],
        tags=["identity"],
    )
    async def list_customer_mappings(
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(staff_auth),
    ) -> list[CustomerMappingResponse]:
        mappings = await PlatformRepository(session).list_customer_mappings(principal.tenant_id)
        return [
            CustomerMappingResponse.model_validate(item, from_attributes=True) for item in mappings
        ]

    @router.post(
        "/admin/customer-mappings",
        response_model=CustomerMappingResponse,
        status_code=201,
        tags=["identity"],
    )
    async def upsert_customer_mapping(
        payload: CustomerMappingRequest,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> CustomerMappingResponse:
        try:
            mapping = await PlatformRepository(session).upsert_customer_mapping(
                tenant_id=principal.tenant_id,
                connector_id=payload.connector_id,
                external_id=payload.external_id,
                customer_id=payload.customer_id,
                name=payload.name,
                email=payload.email,
            )
            await session.commit()
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return CustomerMappingResponse.model_validate(mapping, from_attributes=True)

    @router.get("/admin/members", response_model=list[TenantMemberResponse], tags=["identity"])
    async def list_members(
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> list[TenantMemberResponse]:
        members = await PlatformRepository(session).list_members(principal.tenant_id)
        return [TenantMemberResponse.model_validate(item, from_attributes=True) for item in members]

    @router.post(
        "/admin/members",
        response_model=TenantMemberResponse,
        status_code=201,
        tags=["identity"],
    )
    async def upsert_member(
        payload: TenantMemberRequest,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> TenantMemberResponse:
        if payload.role == "customer" and not payload.customer_id:
            raise HTTPException(status_code=422, detail="Customer members require customer_id")
        if payload.role != "customer" and payload.customer_id:
            raise HTTPException(
                status_code=422, detail="Only customer members may include customer_id"
            )
        if payload.customer_id:
            try:
                await PlatformRepository(session).require_customer(
                    principal.tenant_id, payload.customer_id
                )
            except NotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        member = await PlatformRepository(session).upsert_member(
            tenant_id=principal.tenant_id,
            subject=payload.subject,
            role=payload.role,
            customer_id=payload.customer_id,
            active=payload.active,
        )
        await session.commit()
        return TenantMemberResponse.model_validate(member, from_attributes=True)

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
        if settings.is_production and payload.provider == "mock":
            raise HTTPException(
                status_code=422, detail="Mock connectors are disabled in production"
            )
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
        declared = set(json.loads(connector.capabilities_json))
        supported = request.app.state.provider_registry.supported_capabilities(connector)
        unsupported = sorted(declared - supported)
        if unsupported:
            raise HTTPException(
                status_code=409,
                detail=f"Connector adapter does not implement: {', '.join(unsupported)}",
            )
        if connector.provider != "mock" and not pack.safety.allowed_connector_hosts:
            raise HTTPException(
                status_code=409,
                detail="Live Domain Pack must allowlist every external connector host",
            )
        provider = None
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
        finally:
            if provider is not None:
                await provider.close()
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
            "prompt_version": PROMPT_VERSION,
            "router_version": ROUTER_VERSION,
            "policy_version": POLICY_VERSION,
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
        if not x_event_id:
            raise HTTPException(status_code=401, detail="Signed webhook headers are required")
        connector, body = await _verify_connector_webhook(
            request=request,
            session=session,
            connector_id=connector_id,
            signature=x_webhook_signature,
            timestamp_text=x_webhook_timestamp,
            settings=settings,
        )
        event = await PlatformRepository(session).record_webhook(
            tenant_id=connector.tenant_id,
            connector_id=connector.id,
            provider=connector.provider,
            external_id=x_event_id,
            payload_hash=hashlib.sha256(body).hexdigest(),
            signature_valid=True,
        )
        await session.commit()
        return {"id": event.id, "status": event.status}

    @router.post(
        "/channels/{connector_id}/messages",
        response_model=ChannelMessageResponse,
        tags=["channels"],
    )
    async def receive_channel_message(
        request: Request,
        connector_id: str,
        payload: ChannelMessageRequest,
        session: AsyncSession = Depends(get_session),
        x_webhook_signature: Annotated[str, Header(min_length=32, max_length=256)] = "",
        x_webhook_timestamp: Annotated[str, Header(min_length=1, max_length=20)] = "",
        x_event_id: Annotated[str, Header(min_length=1, max_length=200)] = "",
    ) -> ChannelMessageResponse:
        if not x_event_id:
            raise HTTPException(status_code=401, detail="Signed webhook headers are required")
        connector, body = await _verify_connector_webhook(
            request=request,
            session=session,
            connector_id=connector_id,
            signature=x_webhook_signature,
            timestamp_text=x_webhook_timestamp,
            settings=settings,
            required_capability="inbound_chat",
        )
        repo = PlatformRepository(session)
        payload_hash = hashlib.sha256(body).hexdigest()
        existing_event = await repo.get_webhook(connector.tenant_id, connector.id, x_event_id)
        if existing_event:
            if existing_event.payload_hash != payload_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Webhook event id was reused with a different payload",
                )
            if existing_event.status != "processed" or not existing_event.message_id:
                raise HTTPException(status_code=409, detail="Webhook event is still processing")
            message = await session.get(Message, existing_event.message_id)
            if message is None or existing_event.conversation_id is None:
                raise HTTPException(status_code=409, detail="Webhook result is unavailable")
            metadata = json.loads(message.metadata_json)
            automation_state = str(metadata.get("automation_state", "auto"))
            pending_action = None
            pending_action_id = metadata.get("pending_action_id")
            if isinstance(pending_action_id, str):
                action = await repo.get_action(connector.tenant_id, pending_action_id)
                if action.confirmation_digest:
                    pending_action = PendingAction(
                        id=action.id,
                        action=action.action,
                        status=action.status,
                        confirmation_digest=action.confirmation_digest,
                    )
            return ChannelMessageResponse(
                event_id=existing_event.id,
                conversation_id=existing_event.conversation_id,
                message_id=message.id,
                reply="" if message.role == "user" else message.content,
                ticket_id=metadata.get("ticket_id"),
                pending_action=pending_action,
                automation_state=automation_state,
            )

        lock_key = f"channel:{connector.id}:{payload.external_conversation_id}"
        async with request.app.state.idempotency.lock(connector.tenant_id, lock_key) as acquired:
            if not acquired:
                raise HTTPException(status_code=409, detail="Channel conversation is busy")
            customer_link = await repo.require_external_customer(
                connector.tenant_id, payload.external_customer_id, connector.id
            )
            mapping = await repo.get_channel_conversation(
                connector.tenant_id, connector.id, payload.external_conversation_id
            )
            if mapping:
                conversation = await repo.get_scoped_conversation_by_id(
                    connector.tenant_id, mapping.conversation_id
                )
                if conversation.automation_state != "auto":
                    event = await repo.record_webhook(
                        tenant_id=connector.tenant_id,
                        connector_id=connector.id,
                        provider=connector.provider,
                        external_id=x_event_id,
                        payload_hash=payload_hash,
                        signature_valid=True,
                    )
                    inspection = inspect_message(payload.message)
                    incoming = await SupportRepository(session).add_message(
                        conversation.id,
                        "user",
                        inspection.sanitized_text,
                        metadata={
                            "channel": connector.provider,
                            "automation_state": conversation.automation_state,
                            "locale": payload.locale,
                            "safety_reasons": list(inspection.reasons),
                        },
                    )
                    await repo.complete_webhook(
                        event, conversation_id=conversation.id, message_id=incoming.id
                    )
                    await session.commit()
                    return ChannelMessageResponse(
                        event_id=event.id,
                        conversation_id=conversation.id,
                        message_id=incoming.id,
                        reply="",
                        automation_state=conversation.automation_state,
                    )
            response = await request.app.state.support_service.chat(
                session,
                ChatRequest(
                    message=payload.message,
                    conversation_id=mapping.conversation_id if mapping else None,
                    locale=payload.locale,
                    external_product_id=payload.external_product_id,
                ),
                f"channel:{connector.id}:{x_event_id}",
                tenant_id=connector.tenant_id,
                customer_id=customer_link.customer_id,
                channel=connector.provider,
            )
            event = await repo.record_webhook(
                tenant_id=connector.tenant_id,
                connector_id=connector.id,
                provider=connector.provider,
                external_id=x_event_id,
                payload_hash=payload_hash,
                signature_valid=True,
            )
            await repo.bind_channel_conversation(
                connector.tenant_id,
                connector.id,
                payload.external_conversation_id,
                response.conversation_id,
            )
            await request.app.state.channel_delivery.enqueue_for_conversation(
                session,
                tenant_id=connector.tenant_id,
                conversation_id=response.conversation_id,
                content=response.reply,
                idempotency_key=f"agent-reply:{response.message_id}",
                source_message_id=response.message_id,
            )
            await repo.complete_webhook(
                event, conversation_id=response.conversation_id, message_id=response.message_id
            )
            await session.commit()
        return ChannelMessageResponse(
            event_id=event.id,
            conversation_id=response.conversation_id,
            message_id=response.message_id,
            reply=response.reply,
            ticket_id=response.ticket_id,
            pending_action=response.pending_action,
            automation_state="human" if response.ticket_id else "auto",
        )

    return router
