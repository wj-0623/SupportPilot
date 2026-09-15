from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import Principal, principal_dependency
from app.db.platform_repository import PlatformRepository
from app.db.repository import ConflictError, NotFoundError
from app.schemas import (
    ChatRequest,
    ChatResponse,
    ConversationResponse,
    MessageResponse,
    ResolveTicketRequest,
    TicketResponse,
)
from app.service import SupportService


def _ticket_response(ticket) -> TicketResponse:  # type: ignore[no-untyped-def]
    return TicketResponse(
        id=ticket.id,
        conversation_id=ticket.conversation_id,
        customer_id=ticket.customer_id,
        order_id=ticket.order_id,
        status=ticket.status,
        priority=ticket.priority,
        reason=ticket.reason,
        resolution=ticket.resolution,
        created_at=ticket.created_at,
        resolved_at=ticket.resolved_at,
    )


def create_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    customer_auth = principal_dependency(settings, "customer", "agent", "admin")
    admin_auth = principal_dependency(settings, "agent", "admin")

    async def get_session(request: Request):  # type: ignore[no-untyped-def]
        async with request.app.state.database.sessions() as session:
            yield session

    @router.post(
        "/chat",
        response_model=ChatResponse,
        summary="Send one customer message",
    )
    async def chat(
        request: Request,
        payload: ChatRequest,
        session: AsyncSession = Depends(get_session),
        idempotency_key: str | None = Header(default=None, max_length=128),
        principal: Principal = Depends(customer_auth),
    ) -> ChatResponse:
        if len(payload.message) > settings.max_message_chars:
            raise HTTPException(status_code=422, detail="Message is too long")
        service: SupportService = request.app.state.support_service
        customer_id = principal.customer_id or payload.customer_id
        if not customer_id:
            raise HTTPException(status_code=422, detail="Customer identity is required")
        if principal.customer_id and payload.customer_id not in {None, principal.customer_id}:
            raise HTTPException(status_code=403, detail="Customer identity mismatch")
        try:
            if idempotency_key:
                async with request.app.state.idempotency.lock(
                    principal.tenant_id, idempotency_key
                ) as acquired:
                    if not acquired:
                        raise ConflictError("An identical request is already in progress")
                    return await service.chat(
                        session,
                        payload,
                        idempotency_key,
                        tenant_id=principal.tenant_id,
                        customer_id=customer_id,
                    )
            return await service.chat(
                session,
                payload,
                None,
                tenant_id=principal.tenant_id,
                customer_id=customer_id,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get(
        "/conversations/{conversation_id}",
        response_model=ConversationResponse,
    )
    async def get_conversation(
        conversation_id: str,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(customer_auth),
        customer_id: str | None = Query(default=None, max_length=64),
    ) -> ConversationResponse:
        resolved_customer_id = principal.customer_id or customer_id
        if not resolved_customer_id and principal.role == "customer":
            raise HTTPException(status_code=422, detail="Customer identity is required")
        if principal.customer_id and customer_id not in {None, principal.customer_id}:
            raise HTTPException(status_code=403, detail="Customer identity mismatch")
        try:
            platform_repo = PlatformRepository(session)
            if resolved_customer_id:
                conversation = await platform_repo.get_scoped_conversation(
                    principal.tenant_id, resolved_customer_id, conversation_id
                )
            else:
                conversation = await platform_repo.get_scoped_conversation_by_id(
                    principal.tenant_id, conversation_id
                )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ConversationResponse(
            id=conversation.id,
            customer_id=conversation.customer_id,
            status=conversation.status,
            messages=[
                MessageResponse(
                    id=message.id,
                    role=message.role,
                    content=message.content,
                    intent=message.intent,
                    metadata=json.loads(message.metadata_json),
                    created_at=message.created_at,
                )
                for message in conversation.messages
            ],
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )

    @router.post("/chat/stream", tags=["chat"], summary="Stream turn progress and final response")
    async def stream_chat(
        request: Request,
        payload: ChatRequest,
        session: AsyncSession = Depends(get_session),
        idempotency_key: str | None = Header(default=None, max_length=128),
        principal: Principal = Depends(customer_auth),
    ) -> StreamingResponse:
        customer_id = principal.customer_id or payload.customer_id
        if not customer_id:
            raise HTTPException(status_code=422, detail="Customer identity is required")
        if principal.customer_id and payload.customer_id not in {None, principal.customer_id}:
            raise HTTPException(status_code=403, detail="Customer identity mismatch")

        async def events():  # type: ignore[no-untyped-def]
            yield 'event: status\ndata: {"stage":"accepted"}\n\n'
            service: SupportService = request.app.state.support_service
            try:
                if idempotency_key:
                    async with request.app.state.idempotency.lock(
                        principal.tenant_id, idempotency_key
                    ) as acquired:
                        if not acquired:
                            raise ConflictError("An identical request is already in progress")
                        response = await service.chat(
                            session,
                            payload,
                            idempotency_key,
                            tenant_id=principal.tenant_id,
                            customer_id=customer_id,
                        )
                else:
                    response = await service.chat(
                        session,
                        payload,
                        None,
                        tenant_id=principal.tenant_id,
                        customer_id=customer_id,
                    )
                data = response.model_dump_json()
                yield f"event: final\ndata: {data}\n\n"
            except (NotFoundError, ConflictError) as exc:
                data = json.dumps({"detail": str(exc)}, ensure_ascii=False)
                yield f"event: error\ndata: {data}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @router.get(
        "/tickets",
        response_model=list[TicketResponse],
        summary="List human-review tickets",
    )
    async def list_tickets(
        session: AsyncSession = Depends(get_session),
        ticket_status: Annotated[str | None, Query(alias="status")] = "open",
        principal: Principal = Depends(admin_auth),
    ) -> list[TicketResponse]:
        tickets = await PlatformRepository(session).list_scoped_tickets(
            principal.tenant_id, status=ticket_status
        )
        return [_ticket_response(ticket) for ticket in tickets]

    @router.post(
        "/tickets/{ticket_id}/resolve",
        response_model=TicketResponse,
        summary="Resolve a human-review ticket",
    )
    async def resolve_ticket(
        ticket_id: str,
        payload: ResolveTicketRequest,
        session: AsyncSession = Depends(get_session),
        principal: Principal = Depends(admin_auth),
    ) -> TicketResponse:
        try:
            ticket = await PlatformRepository(session).resolve_scoped_ticket(
                principal.tenant_id, ticket_id, payload.resolution
            )
            await session.commit()
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return _ticket_response(ticket)

    return router
