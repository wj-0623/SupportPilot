from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AgentRun,
    Conversation,
    Customer,
    Feedback,
    IdempotencyRecord,
    Message,
    Order,
    RunReview,
    Ticket,
)


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class SupportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_customer(self, customer_id: str) -> Customer:
        customer = await self.session.get(Customer, customer_id)
        if customer is None:
            raise NotFoundError("Customer not found")
        return customer

    async def get_or_create_conversation(
        self, customer_id: str, conversation_id: str | None
    ) -> Conversation:
        await self.get_customer(customer_id)
        if conversation_id:
            conversation = await self.session.get(Conversation, conversation_id)
            if conversation is None:
                raise NotFoundError("Conversation not found")
            if conversation.customer_id != customer_id:
                raise NotFoundError("Conversation not found")
            return conversation
        conversation = Conversation(customer_id=customer_id)
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        intent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            intent=intent,
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def recent_messages(self, conversation_id: str, limit: int = 10) -> list[Message]:
        result = await self.session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        return list(reversed(list(result)))

    async def get_order(self, customer_id: str, order_id: str) -> Order:
        order = await self.session.scalar(
            select(Order).where(Order.id == order_id, Order.customer_id == customer_id)
        )
        if order is None:
            raise NotFoundError("Order not found for this customer")
        return order

    async def list_orders(self, customer_id: str, limit: int = 5) -> list[Order]:
        result = await self.session.scalars(
            select(Order)
            .where(Order.customer_id == customer_id)
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
        return list(result)

    async def create_ticket(
        self,
        conversation_id: str,
        customer_id: str,
        reason: str,
        order_id: str | None = None,
        priority: str = "normal",
    ) -> Ticket:
        await self.session.execute(
            select(Conversation.id).where(Conversation.id == conversation_id).with_for_update()
        )
        existing = await self.session.scalar(
            select(Ticket).where(
                Ticket.conversation_id == conversation_id,
                Ticket.order_id == order_id,
                Ticket.status == "open",
            )
        )
        if existing:
            return existing
        ticket = Ticket(
            conversation_id=conversation_id,
            customer_id=customer_id,
            order_id=order_id,
            reason=reason,
            priority=priority,
        )
        self.session.add(ticket)
        await self.session.flush()
        return ticket

    async def get_conversation(self, conversation_id: str) -> Conversation:
        conversation = await self.session.get(Conversation, conversation_id)
        if conversation is None:
            raise NotFoundError("Conversation not found")
        await self.session.refresh(conversation, ["messages"])
        return conversation

    async def list_tickets(self, status: str | None = None, limit: int = 100) -> list[Ticket]:
        statement = select(Ticket).order_by(Ticket.created_at.desc()).limit(limit)
        if status:
            statement = statement.where(Ticket.status == status)
        result = await self.session.scalars(statement)
        return list(result)

    async def resolve_ticket(self, ticket_id: str, resolution: str) -> Ticket:
        ticket = await self.session.get(Ticket, ticket_id)
        if ticket is None:
            raise NotFoundError("Ticket not found")
        if ticket.status == "resolved":
            raise ConflictError("Ticket is already resolved")
        ticket.status = "resolved"
        ticket.resolution = resolution
        ticket.resolved_at = datetime.now(UTC)
        return ticket

    async def get_idempotent_response(self, key: str, request_hash: str) -> dict[str, Any] | None:
        record = await self.session.get(IdempotencyRecord, key)
        if record is None:
            return None
        if record.request_hash != request_hash:
            raise ConflictError("Idempotency key was already used for a different request")
        return cast(dict[str, Any], json.loads(record.response_json))

    async def save_idempotent_response(
        self, key: str, request_hash: str, response: dict[str, Any]
    ) -> None:
        self.session.add(
            IdempotencyRecord(
                key=key,
                request_hash=request_hash,
                response_json=json.dumps(response, ensure_ascii=False),
            )
        )

    async def create_agent_run(
        self,
        *,
        conversation_id: str | None,
        customer_id: str,
        message_id: str | None,
        status: str,
        duration_ms: float,
        input_hash: str,
        prompt_version: str,
        router_version: str,
        policy_version: str,
        intent: str | None = None,
        mode: str | None = None,
        route_confidence: float | None = None,
        tool_calls: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        handoff: bool = False,
        citation_count: int = 0,
        model_name: str | None = None,
        safety_flags: list[str] | None = None,
        trace: list[str] | None = None,
        observations: list[dict[str, Any]] | None = None,
        error_type: str | None = None,
    ) -> AgentRun:
        run = AgentRun(
            conversation_id=conversation_id,
            customer_id=customer_id,
            message_id=message_id,
            status=status,
            intent=intent,
            mode=mode,
            route_confidence=route_confidence,
            duration_ms=duration_ms,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            handoff=handoff,
            citation_count=citation_count,
            input_hash=input_hash,
            prompt_version=prompt_version,
            router_version=router_version,
            policy_version=policy_version,
            model_name=model_name,
            safety_flags_json=json.dumps(safety_flags or [], ensure_ascii=False),
            trace_json=json.dumps(trace or [], ensure_ascii=False),
            observations_json=json.dumps(observations or [], ensure_ascii=False),
            error_type=error_type,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def get_agent_run(self, run_id: str) -> AgentRun:
        run = await self.session.get(AgentRun, run_id)
        if run is None:
            raise NotFoundError("Agent run not found")
        return run

    async def list_agent_runs(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        prompt_version: str | None = None,
        router_version: str | None = None,
        policy_version: str | None = None,
    ) -> list[AgentRun]:
        statement = select(AgentRun).order_by(AgentRun.created_at.desc()).limit(limit)
        if status:
            statement = statement.where(AgentRun.status == status)
        if prompt_version:
            statement = statement.where(AgentRun.prompt_version == prompt_version)
        if router_version:
            statement = statement.where(AgentRun.router_version == router_version)
        if policy_version:
            statement = statement.where(AgentRun.policy_version == policy_version)
        return list(await self.session.scalars(statement))

    async def create_feedback(
        self,
        *,
        message_id: str,
        customer_id: str,
        rating: int,
        resolved: bool | None,
        reason: str | None,
        comment: str | None,
    ) -> Feedback:
        message = await self.session.get(Message, message_id)
        if message is None or message.role != "assistant":
            raise NotFoundError("Assistant message not found")
        conversation = await self.session.get(Conversation, message.conversation_id)
        if conversation is None or conversation.customer_id != customer_id:
            raise NotFoundError("Assistant message not found")
        existing = await self.session.scalar(
            select(Feedback).where(Feedback.message_id == message_id)
        )
        if existing:
            raise ConflictError("Feedback already exists for this message")
        run = await self.session.scalar(select(AgentRun).where(AgentRun.message_id == message_id))
        if run is None:
            raise NotFoundError("Agent run not found for this message")
        feedback = Feedback(
            run_id=run.id,
            message_id=message_id,
            customer_id=customer_id,
            rating=rating,
            resolved=resolved,
            reason=reason,
            comment=comment,
        )
        self.session.add(feedback)
        await self.session.flush()
        return feedback

    async def list_feedback(self, *, limit: int = 1_000) -> list[Feedback]:
        return list(
            await self.session.scalars(
                select(Feedback).order_by(Feedback.created_at.desc()).limit(limit)
            )
        )

    async def create_run_review(
        self,
        *,
        run_id: str,
        expected_intent: str | None,
        quality_score: int,
        notes: str | None,
        reviewer: str,
    ) -> RunReview:
        await self.get_agent_run(run_id)
        existing = await self.session.scalar(select(RunReview).where(RunReview.run_id == run_id))
        if existing:
            raise ConflictError("Run already has a review")
        review = RunReview(
            run_id=run_id,
            expected_intent=expected_intent,
            quality_score=quality_score,
            notes=notes,
            reviewer=reviewer,
        )
        self.session.add(review)
        await self.session.flush()
        return review

    async def list_run_reviews(self, *, limit: int = 1_000) -> list[RunReview]:
        return list(
            await self.session.scalars(
                select(RunReview).order_by(RunReview.created_at.desc()).limit(limit)
            )
        )
