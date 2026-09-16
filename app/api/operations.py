from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.versions import POLICY_VERSION, PROMPT_VERSION, ROUTER_VERSION
from app.core.config import Settings
from app.core.security import Principal, principal_dependency
from app.db.models import AgentRun, Feedback, RunReview, Ticket
from app.db.platform_repository import PlatformRepository
from app.db.repository import ConflictError, NotFoundError, SupportRepository
from app.domain.config import DomainPackConfig
from app.domain.guardrails import inspect_message
from app.knowledge import partition_fresh_knowledge
from app.metrics import FEEDBACK
from app.ops.quality import build_quality_snapshot
from app.schemas import (
    AgentRunResponse,
    AuditEventResponse,
    EvaluationCandidateReviewRequest,
    FeedbackRequest,
    FeedbackResponse,
    HandoffSnapshot,
    KnowledgeHealthResponse,
    QualitySnapshot,
    RunReviewRequest,
    RunReviewResponse,
)


def _run_response(run: AgentRun) -> AgentRunResponse:
    return AgentRunResponse(
        id=run.id,
        conversation_id=run.conversation_id,
        customer_id=run.customer_id,
        message_id=run.message_id,
        status=run.status,
        intent=run.intent,
        mode=run.mode,
        route_confidence=run.route_confidence,
        duration_ms=run.duration_ms,
        tool_calls=run.tool_calls,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        handoff=run.handoff,
        citation_count=run.citation_count,
        prompt_version=run.prompt_version,
        router_version=run.router_version,
        policy_version=run.policy_version,
        model_name=run.model_name,
        safety_flags=json.loads(run.safety_flags_json),
        trace=json.loads(run.trace_json),
        observations=json.loads(run.observations_json),
        error_type=run.error_type,
        created_at=run.created_at,
    )


def _feedback_response(feedback: Feedback) -> FeedbackResponse:
    return FeedbackResponse(
        id=feedback.id,
        run_id=feedback.run_id,
        message_id=feedback.message_id,
        rating=feedback.rating,
        resolved=feedback.resolved,
        reason=feedback.reason,
        created_at=feedback.created_at,
    )


def _review_response(review: RunReview) -> RunReviewResponse:
    return RunReviewResponse(
        id=review.id,
        run_id=review.run_id,
        expected_intent=review.expected_intent,
        quality_score=review.quality_score,
        notes=review.notes,
        reviewer=review.reviewer,
        created_at=review.created_at,
    )


def create_operations_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    customer_auth = principal_dependency(settings, "customer", "agent", "admin")
    admin_auth = principal_dependency(settings, "agent", "admin")

    async def get_session(request: Request):  # type: ignore[no-untyped-def]
        async with request.app.state.database.sessions() as session:
            yield session

    @router.post(
        "/messages/{message_id}/feedback",
        response_model=FeedbackResponse,
        tags=["feedback"],
    )
    async def submit_feedback(
        message_id: str,
        payload: FeedbackRequest,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(customer_auth),
    ) -> FeedbackResponse:
        sanitized_comment = (
            inspect_message(payload.comment).sanitized_text if payload.comment else None
        )
        repo = SupportRepository(session)
        customer_id = principal.customer_id or payload.customer_id
        if not customer_id:
            raise HTTPException(status_code=422, detail="Customer identity is required")
        if principal.customer_id and payload.customer_id not in {None, principal.customer_id}:
            raise HTTPException(status_code=403, detail="Customer identity mismatch")
        try:
            await PlatformRepository(session).require_scoped_message(
                principal.tenant_id, customer_id, message_id
            )
            feedback = await repo.create_feedback(
                message_id=message_id,
                customer_id=customer_id,
                rating=payload.rating,
                resolved=payload.resolved,
                reason=payload.reason,
                comment=sanitized_comment,
            )
            if payload.rating == -1 or payload.resolved is False:
                platform_repo = PlatformRepository(session)
                run = await platform_repo.get_scoped_run(principal.tenant_id, feedback.run_id)
                await platform_repo.create_evaluation_candidate(
                    tenant_id=principal.tenant_id,
                    run=run,
                    source="customer_feedback",
                    expected={"resolved": payload.resolved, "reason": payload.reason},
                    evidence={"rating": payload.rating},
                )
            await session.commit()
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        FEEDBACK.labels(rating=str(payload.rating), reason=payload.reason or "unspecified").inc()
        return _feedback_response(feedback)

    @router.get(
        "/ops/runs",
        response_model=list[AgentRunResponse],
        tags=["operations"],
    )
    async def list_runs(
        session: AsyncSession = Depends(get_session),
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
        run_status: Annotated[str | None, Query(alias="status")] = None,
        principal: Principal = Depends(admin_auth),
    ) -> list[AgentRunResponse]:
        runs = await PlatformRepository(session).list_scoped_runs(
            principal.tenant_id, limit=limit, status=run_status
        )
        return [_run_response(run) for run in runs]

    @router.get(
        "/ops/runs/{run_id}",
        response_model=AgentRunResponse,
        tags=["operations"],
    )
    async def get_run(
        run_id: str,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> AgentRunResponse:
        try:
            run = await PlatformRepository(session).get_scoped_run(principal.tenant_id, run_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _run_response(run)

    @router.post(
        "/ops/runs/{run_id}/review",
        response_model=RunReviewResponse,
        tags=["operations"],
    )
    async def review_run(
        run_id: str,
        payload: RunReviewRequest,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> RunReviewResponse:
        notes = inspect_message(payload.notes).sanitized_text if payload.notes else None
        repo = SupportRepository(session)
        try:
            await PlatformRepository(session).get_scoped_run(principal.tenant_id, run_id)
            review = await repo.create_run_review(
                run_id=run_id,
                expected_intent=payload.expected_intent,
                quality_score=payload.quality_score,
                notes=notes,
                reviewer=payload.reviewer,
            )
            run = await PlatformRepository(session).get_scoped_run(principal.tenant_id, run_id)
            if payload.quality_score <= 3 or (
                payload.expected_intent and payload.expected_intent != run.intent
            ):
                await PlatformRepository(session).create_evaluation_candidate(
                    tenant_id=principal.tenant_id,
                    run=run,
                    source="human_review",
                    expected={
                        "intent": payload.expected_intent,
                        "minimum_quality": payload.quality_score,
                    },
                    evidence={"review_id": review.id},
                )
            await session.commit()
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _review_response(review)

    @router.get(
        "/ops/quality",
        response_model=QualitySnapshot,
        tags=["operations"],
    )
    async def quality_snapshot(
        session: AsyncSession = Depends(get_session),
        limit: Annotated[int, Query(ge=1, le=10_000)] = 1_000,
        principal: Principal = Depends(admin_auth),
    ) -> QualitySnapshot:
        platform_repo = PlatformRepository(session)
        runs = await platform_repo.list_scoped_runs(principal.tenant_id, limit=limit)
        runs = [
            run
            for run in runs
            if run.prompt_version == PROMPT_VERSION
            and run.router_version == ROUTER_VERSION
            and run.policy_version == POLICY_VERSION
        ]
        feedback = await platform_repo.list_scoped_feedback(principal.tenant_id, limit=limit)
        reviews = await platform_repo.list_scoped_reviews(principal.tenant_id, limit=limit)
        outcomes = await platform_repo.list_outcomes(principal.tenant_id, limit=limit)
        actions = await platform_repo.list_actions(principal.tenant_id, limit=limit)
        return build_quality_snapshot(runs, feedback, reviews, outcomes, actions)

    @router.get(
        "/ops/audit-events",
        response_model=list[AuditEventResponse],
        tags=["operations"],
    )
    async def list_audit_events(
        session: AsyncSession = Depends(get_session),
        limit: Annotated[int, Query(ge=1, le=1_000)] = 200,
        principal: Principal = Depends(admin_auth),
    ) -> list[AuditEventResponse]:
        events = await PlatformRepository(session).list_audit_events(
            principal.tenant_id, limit=limit
        )
        return [
            AuditEventResponse(
                id=event.id,
                actor=event.actor,
                role=event.role,
                method=event.method,
                path=event.path,
                status_code=event.status_code,
                request_id=event.request_id,
                resource_id=event.resource_id,
                created_at=event.created_at,
            )
            for event in events
        ]

    @router.get(
        "/ops/knowledge-health",
        response_model=KnowledgeHealthResponse,
        tags=["operations"],
    )
    async def knowledge_health(
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> KnowledgeHealthResponse:
        repo = PlatformRepository(session)
        pack_revision = await repo.get_live_domain_pack(principal.tenant_id)
        pack = DomainPackConfig.model_validate_json(pack_revision.config_json)
        sources = await repo.list_live_knowledge(principal.tenant_id)
        fresh, stale = partition_fresh_knowledge(sources, pack.knowledge.freshness_hours)
        return KnowledgeHealthResponse(
            live_sources=len(sources),
            fresh_sources=len(fresh),
            stale_sources=len(stale),
            stale_source_keys=sorted({source.source_key for source in stale}),
            freshness_hours=pack.knowledge.freshness_hours,
        )

    @router.get(
        "/ops/handoffs",
        response_model=HandoffSnapshot,
        tags=["operations"],
    )
    async def handoff_snapshot(
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> HandoffSnapshot:
        tickets = await PlatformRepository(session).list_scoped_tickets(
            principal.tenant_id, status=None, limit=10_000
        )
        now = datetime.now(UTC)

        def aware(value: datetime) -> datetime:
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value

        measurable = [
            ticket
            for ticket in tickets
            if ticket.sla_due_at
            and (ticket.first_response_at is not None or aware(ticket.sla_due_at) <= now)
        ]

        def missed_sla(ticket: Ticket) -> bool:
            if ticket.sla_due_at is None:
                return False
            return ticket.first_response_at is None or aware(ticket.first_response_at) > aware(
                ticket.sla_due_at
            )

        breached = [ticket for ticket in measurable if missed_sla(ticket)]
        active_statuses = {"open", "in_progress", "waiting_customer", "reopened"}
        return HandoffSnapshot(
            total_tickets=len(tickets),
            active_tickets=sum(ticket.status in active_statuses for ticket in tickets),
            overdue_tickets=len(breached),
            sla_compliance_rate=(
                round((len(measurable) - len(breached)) / len(measurable), 4)
                if measurable
                else None
            ),
            by_priority=dict(Counter(ticket.priority for ticket in tickets)),
        )

    @router.get("/ops/evaluation-candidates", tags=["operations"])
    async def list_evaluation_candidates(
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
        candidate_status: Annotated[str, Query(alias="status")] = "candidate",
    ) -> list[dict[str, object]]:
        candidates = await PlatformRepository(session).list_evaluation_candidates(
            principal.tenant_id, status=candidate_status
        )
        return [
            {
                "id": item.id,
                "run_id": item.run_id,
                "source": item.source,
                "status": item.status,
                "input_hash": item.input_hash,
                "expected": json.loads(item.expected_json),
                "evidence": json.loads(item.evidence_json),
                "created_at": item.created_at.isoformat(),
            }
            for item in candidates
        ]

    @router.post("/ops/evaluation-candidates/{candidate_id}/review", tags=["operations"])
    async def review_evaluation_candidate(
        candidate_id: str,
        payload: EvaluationCandidateReviewRequest,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> dict[str, object]:
        try:
            candidate = await PlatformRepository(session).review_evaluation_candidate(
                principal.tenant_id, candidate_id, payload.accepted, principal.subject
            )
            await session.commit()
        except (NotFoundError, ConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": candidate.id, "status": candidate.status}

    @router.post("/ops/evaluation-datasets", status_code=201, tags=["operations"])
    async def build_evaluation_dataset(
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> dict[str, object]:
        try:
            revision = await PlatformRepository(session).build_dataset_revision(
                principal.tenant_id, principal.subject
            )
            await session.commit()
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "id": revision.id,
            "version": revision.version,
            "status": revision.status,
            "checksum": revision.checksum,
        }

    return router
