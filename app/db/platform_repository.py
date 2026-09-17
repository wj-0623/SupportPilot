from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ActionExecution,
    AgentRelease,
    AgentRun,
    AgentRunScope,
    AuditEvent,
    ChannelConversation,
    ConnectorDefinition,
    Conversation,
    ConversationScope,
    Customer,
    CustomerChannelIdentity,
    DomainPackRevision,
    EvaluationCandidate,
    EvaluationDatasetRevision,
    Feedback,
    KnowledgeSource,
    Message,
    OutboundMessage,
    OutboxEvent,
    ResolutionOutcome,
    RunReview,
    Tenant,
    TenantCustomer,
    TenantMember,
    Ticket,
    WebhookEvent,
    WorkflowCheckpoint,
)
from app.db.repository import ConflictError, NotFoundError
from app.domain.config import DomainPackConfig


class PlatformRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_tenant(self, tenant_id: str) -> Tenant:
        tenant = await self.session.get(Tenant, tenant_id)
        if tenant is None or tenant.status != "active":
            raise NotFoundError("Tenant not found")
        return tenant

    async def require_customer(self, tenant_id: str, customer_id: str) -> TenantCustomer:
        await self.get_tenant(tenant_id)
        link = await self.session.scalar(
            select(TenantCustomer).where(
                TenantCustomer.tenant_id == tenant_id,
                TenantCustomer.customer_id == customer_id,
            )
        )
        if link is None:
            raise NotFoundError("Customer not found")
        return link

    async def require_external_customer(
        self, tenant_id: str, external_customer_id: str, connector_id: str | None = None
    ) -> TenantCustomer:
        await self.get_tenant(tenant_id)
        if connector_id:
            identity = await self.session.scalar(
                select(CustomerChannelIdentity).where(
                    CustomerChannelIdentity.tenant_id == tenant_id,
                    CustomerChannelIdentity.connector_id == connector_id,
                    CustomerChannelIdentity.external_id == external_customer_id,
                )
            )
            if identity:
                return await self.require_customer(tenant_id, identity.customer_id)
        link = await self.session.scalar(
            select(TenantCustomer).where(
                TenantCustomer.tenant_id == tenant_id,
                TenantCustomer.external_id == external_customer_id,
            )
        )
        if link is None:
            raise NotFoundError("External customer mapping not found")
        return link

    async def list_customer_mappings(self, tenant_id: str) -> list[CustomerChannelIdentity]:
        await self.get_tenant(tenant_id)
        return list(
            await self.session.scalars(
                select(CustomerChannelIdentity)
                .where(CustomerChannelIdentity.tenant_id == tenant_id)
                .order_by(CustomerChannelIdentity.created_at.desc())
            )
        )

    async def upsert_customer_mapping(
        self,
        *,
        tenant_id: str,
        connector_id: str,
        external_id: str,
        customer_id: str,
        name: str,
        email: str,
    ) -> CustomerChannelIdentity:
        await self.get_tenant(tenant_id)
        await self.get_connector(tenant_id, connector_id)
        customer = await self.session.get(Customer, customer_id)
        if customer is None:
            email_owner = await self.session.scalar(select(Customer).where(Customer.email == email))
            if email_owner is not None:
                raise ConflictError("Customer email is already associated with another id")
            customer = Customer(id=customer_id, name=name, email=email)
            self.session.add(customer)
            await self.session.flush()
        elif customer.email != email:
            raise ConflictError("Customer id is already associated with another email")
        customer_link = await self.session.scalar(
            select(TenantCustomer).where(
                TenantCustomer.tenant_id == tenant_id,
                TenantCustomer.customer_id == customer_id,
            )
        )
        if customer_link is None:
            self.session.add(TenantCustomer(tenant_id=tenant_id, customer_id=customer_id))
            await self.session.flush()
        identity: CustomerChannelIdentity | None = await self.session.scalar(
            select(CustomerChannelIdentity).where(
                CustomerChannelIdentity.tenant_id == tenant_id,
                CustomerChannelIdentity.connector_id == connector_id,
                CustomerChannelIdentity.external_id == external_id,
            )
        )
        if identity:
            if identity.customer_id != customer_id:
                raise ConflictError("External customer id is already mapped")
            return identity
        identity = await self.session.scalar(
            select(CustomerChannelIdentity).where(
                CustomerChannelIdentity.tenant_id == tenant_id,
                CustomerChannelIdentity.connector_id == connector_id,
                CustomerChannelIdentity.customer_id == customer_id,
            )
        )
        if identity:
            identity.external_id = external_id
            await self.session.flush()
            return identity
        identity = CustomerChannelIdentity(
            tenant_id=tenant_id,
            connector_id=connector_id,
            customer_id=customer_id,
            external_id=external_id,
        )
        self.session.add(identity)
        await self.session.flush()
        return identity

    async def list_members(self, tenant_id: str) -> list[TenantMember]:
        await self.get_tenant(tenant_id)
        return list(
            await self.session.scalars(
                select(TenantMember)
                .where(TenantMember.tenant_id == tenant_id)
                .order_by(TenantMember.created_at)
            )
        )

    async def upsert_member(
        self,
        *,
        tenant_id: str,
        subject: str,
        role: str,
        customer_id: str | None,
        active: bool,
    ) -> TenantMember:
        await self.get_tenant(tenant_id)
        member = await self.session.scalar(
            select(TenantMember).where(
                TenantMember.tenant_id == tenant_id,
                TenantMember.subject == subject,
            )
        )
        if member is None:
            member = TenantMember(
                tenant_id=tenant_id,
                subject=subject,
                role=role,
                customer_id=customer_id,
                active=active,
            )
            self.session.add(member)
        else:
            member.role = role
            member.customer_id = customer_id
            member.active = active
        await self.session.flush()
        return member

    async def bind_conversation(self, tenant_id: str, conversation_id: str) -> None:
        existing = await self.session.get(ConversationScope, conversation_id)
        if existing:
            if existing.tenant_id != tenant_id:
                raise NotFoundError("Conversation not found")
            return
        self.session.add(ConversationScope(conversation_id=conversation_id, tenant_id=tenant_id))
        await self.session.flush()

    async def get_scoped_conversation(
        self, tenant_id: str, customer_id: str, conversation_id: str
    ) -> Conversation:
        conversation = await self.session.scalar(
            select(Conversation)
            .join(ConversationScope, ConversationScope.conversation_id == Conversation.id)
            .where(
                Conversation.id == conversation_id,
                Conversation.customer_id == customer_id,
                ConversationScope.tenant_id == tenant_id,
            )
        )
        if conversation is None:
            raise NotFoundError("Conversation not found")
        await self.session.refresh(conversation, ["messages"])
        return conversation

    async def get_scoped_conversation_by_id(
        self, tenant_id: str, conversation_id: str
    ) -> Conversation:
        conversation = await self.session.scalar(
            select(Conversation)
            .join(ConversationScope, ConversationScope.conversation_id == Conversation.id)
            .where(
                Conversation.id == conversation_id,
                ConversationScope.tenant_id == tenant_id,
            )
        )
        if conversation is None:
            raise NotFoundError("Conversation not found")
        await self.session.refresh(conversation, ["messages"])
        return conversation

    async def bind_run(self, tenant_id: str, run_id: str, release_id: str | None = None) -> None:
        self.session.add(AgentRunScope(run_id=run_id, tenant_id=tenant_id, release_id=release_id))
        await self.session.flush()

    async def list_scoped_runs(
        self, tenant_id: str, *, limit: int = 100, status: str | None = None
    ) -> list[AgentRun]:
        statement = (
            select(AgentRun)
            .join(AgentRunScope, AgentRunScope.run_id == AgentRun.id)
            .where(AgentRunScope.tenant_id == tenant_id)
            .order_by(AgentRun.created_at.desc())
            .limit(limit)
        )
        if status:
            statement = statement.where(AgentRun.status == status)
        return list(await self.session.scalars(statement))

    async def get_scoped_run(self, tenant_id: str, run_id: str) -> AgentRun:
        run = await self.session.scalar(
            select(AgentRun)
            .join(AgentRunScope, AgentRunScope.run_id == AgentRun.id)
            .where(AgentRun.id == run_id, AgentRunScope.tenant_id == tenant_id)
        )
        if run is None:
            raise NotFoundError("Agent run not found")
        return run

    async def require_scoped_message(
        self, tenant_id: str, customer_id: str, message_id: str
    ) -> Message:
        message = await self.session.scalar(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(ConversationScope, ConversationScope.conversation_id == Conversation.id)
            .where(
                Message.id == message_id,
                Conversation.customer_id == customer_id,
                ConversationScope.tenant_id == tenant_id,
            )
        )
        if message is None:
            raise NotFoundError("Assistant message not found")
        return message

    async def list_scoped_feedback(self, tenant_id: str, limit: int = 1_000) -> list[Feedback]:
        return list(
            await self.session.scalars(
                select(Feedback)
                .join(AgentRunScope, AgentRunScope.run_id == Feedback.run_id)
                .where(AgentRunScope.tenant_id == tenant_id)
                .order_by(Feedback.created_at.desc())
                .limit(limit)
            )
        )

    async def list_scoped_reviews(self, tenant_id: str, limit: int = 1_000) -> list[RunReview]:
        return list(
            await self.session.scalars(
                select(RunReview)
                .join(AgentRunScope, AgentRunScope.run_id == RunReview.run_id)
                .where(AgentRunScope.tenant_id == tenant_id)
                .order_by(RunReview.created_at.desc())
                .limit(limit)
            )
        )

    async def list_scoped_tickets(
        self, tenant_id: str, *, status: str | None = None, limit: int = 100
    ) -> list[Ticket]:
        statement = (
            select(Ticket)
            .join(ConversationScope, ConversationScope.conversation_id == Ticket.conversation_id)
            .where(ConversationScope.tenant_id == tenant_id)
            .order_by(Ticket.created_at.desc())
            .limit(limit)
        )
        if status:
            statement = statement.where(Ticket.status == status)
        return list(await self.session.scalars(statement))

    async def get_scoped_ticket(self, tenant_id: str, ticket_id: str) -> Ticket:
        ticket = await self.session.scalar(
            select(Ticket)
            .join(ConversationScope, ConversationScope.conversation_id == Ticket.conversation_id)
            .where(Ticket.id == ticket_id, ConversationScope.tenant_id == tenant_id)
        )
        if ticket is None:
            raise NotFoundError("Ticket not found")
        return ticket

    async def resolve_scoped_ticket(
        self, tenant_id: str, ticket_id: str, resolution: str
    ) -> Ticket:
        return await self.update_scoped_ticket(
            tenant_id, ticket_id, status="resolved", resolution=resolution
        )

    async def update_scoped_ticket(
        self,
        tenant_id: str,
        ticket_id: str,
        *,
        status: str | None = None,
        assigned_to: str | None = None,
        resolution: str | None = None,
    ) -> Ticket:
        ticket = await self.session.scalar(
            select(Ticket)
            .join(ConversationScope, ConversationScope.conversation_id == Ticket.conversation_id)
            .where(Ticket.id == ticket_id, ConversationScope.tenant_id == tenant_id)
            .with_for_update()
        )
        if ticket is None:
            raise NotFoundError("Ticket not found")
        transitions = {
            "open": {"in_progress", "waiting_customer", "resolved"},
            "in_progress": {"waiting_customer", "resolved"},
            "waiting_customer": {"in_progress", "resolved"},
            "resolved": {"reopened"},
            "reopened": {"in_progress", "waiting_customer", "resolved"},
        }
        now = datetime.now(UTC)
        if status == "resolved" and ticket.status == "resolved":
            raise ConflictError("Ticket is already resolved")
        if status and status != ticket.status:
            if status not in transitions.get(ticket.status, set()):
                raise ConflictError(f"Ticket cannot transition from {ticket.status} to {status}")
            if status == "resolved" and not resolution:
                raise ConflictError("Resolution is required when resolving a ticket")
            ticket.status = status
            if status in {"in_progress", "waiting_customer"} and ticket.first_response_at is None:
                ticket.first_response_at = now
            if status == "resolved":
                ticket.resolution = resolution
                ticket.resolved_at = now
            elif status == "reopened":
                ticket.resolution = None
                ticket.resolved_at = None
        if assigned_to is not None:
            ticket.assigned_to = assigned_to
        return ticket

    async def get_channel_conversation(
        self, tenant_id: str, connector_id: str, external_conversation_id: str
    ) -> ChannelConversation | None:
        mapping: ChannelConversation | None = await self.session.scalar(
            select(ChannelConversation).where(
                ChannelConversation.tenant_id == tenant_id,
                ChannelConversation.connector_id == connector_id,
                ChannelConversation.external_conversation_id == external_conversation_id,
            )
        )
        return mapping

    async def bind_channel_conversation(
        self,
        tenant_id: str,
        connector_id: str,
        external_conversation_id: str,
        conversation_id: str,
    ) -> ChannelConversation:
        existing = await self.get_channel_conversation(
            tenant_id, connector_id, external_conversation_id
        )
        if existing:
            if existing.conversation_id != conversation_id:
                raise ConflictError("External conversation is already bound")
            return existing
        mapping = ChannelConversation(
            tenant_id=tenant_id,
            connector_id=connector_id,
            external_conversation_id=external_conversation_id,
            conversation_id=conversation_id,
        )
        self.session.add(mapping)
        await self.session.flush()
        return mapping

    async def get_channel_mapping_for_conversation(
        self, tenant_id: str, conversation_id: str
    ) -> ChannelConversation | None:
        mapping: ChannelConversation | None = await self.session.scalar(
            select(ChannelConversation).where(
                ChannelConversation.tenant_id == tenant_id,
                ChannelConversation.conversation_id == conversation_id,
            )
        )
        return mapping

    async def set_conversation_automation(
        self, tenant_id: str, conversation_id: str, state: str
    ) -> Conversation:
        conversation = await self.get_scoped_conversation_by_id(tenant_id, conversation_id)
        conversation.automation_state = state
        conversation.automation_updated_at = datetime.now(UTC)
        if state == "closed":
            conversation.status = "closed"
        elif state == "auto" and conversation.status == "closed":
            conversation.status = "active"
        await self.session.flush()
        return conversation

    async def enqueue_outbound_message(
        self,
        *,
        tenant_id: str,
        connector_id: str,
        conversation_id: str,
        external_conversation_id: str,
        content: str,
        idempotency_key: str,
        source_action_id: str | None = None,
        source_message_id: str | None = None,
    ) -> OutboundMessage:
        existing = await self.session.scalar(
            select(OutboundMessage).where(
                OutboundMessage.tenant_id == tenant_id,
                OutboundMessage.connector_id == connector_id,
                OutboundMessage.idempotency_key == idempotency_key,
            )
        )
        if existing:
            return existing
        message = OutboundMessage(
            tenant_id=tenant_id,
            connector_id=connector_id,
            conversation_id=conversation_id,
            external_conversation_id=external_conversation_id,
            source_action_id=source_action_id,
            source_message_id=source_message_id,
            content=content,
            idempotency_key=idempotency_key,
        )
        self.session.add(message)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError("Outbound idempotency key already exists") from exc
        return message

    async def get_outbound_by_key(
        self, tenant_id: str, connector_id: str, idempotency_key: str
    ) -> OutboundMessage | None:
        message: OutboundMessage | None = await self.session.scalar(
            select(OutboundMessage).where(
                OutboundMessage.tenant_id == tenant_id,
                OutboundMessage.connector_id == connector_id,
                OutboundMessage.idempotency_key == idempotency_key,
            )
        )
        return message

    async def claim_outbound_messages(
        self, *, limit: int = 20, lease_seconds: int = 120
    ) -> list[OutboundMessage]:
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=lease_seconds)
        messages = list(
            await self.session.scalars(
                select(OutboundMessage)
                .where(
                    or_(
                        (
                            OutboundMessage.status.in_(["pending", "retryable"])
                            & (OutboundMessage.available_at <= now)
                        ),
                        (
                            (OutboundMessage.status == "sending")
                            & (OutboundMessage.updated_at <= stale_before)
                        ),
                    )
                )
                .order_by(OutboundMessage.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for message in messages:
            message.status = "sending"
            message.attempts += 1
        await self.session.flush()
        return messages

    async def mark_outbound_delivered(
        self, message: OutboundMessage, external_id: str | None
    ) -> None:
        message.status = "delivered"
        message.external_id = external_id
        message.error_code = None
        message.delivered_at = datetime.now(UTC)

    async def mark_outbound_failed(
        self,
        message: OutboundMessage,
        error_code: str,
        *,
        max_attempts: int,
        retryable: bool,
    ) -> None:
        message.error_code = error_code[:160]
        if not retryable or message.attempts >= max_attempts:
            message.status = "dead_letter"
            return
        message.status = "retryable"
        delay_seconds = min(900, 2 ** min(message.attempts, 9))
        message.available_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)

    async def list_outbound_messages(
        self, tenant_id: str, *, limit: int = 100
    ) -> list[OutboundMessage]:
        return list(
            await self.session.scalars(
                select(OutboundMessage)
                .where(OutboundMessage.tenant_id == tenant_id)
                .order_by(OutboundMessage.created_at.desc())
                .limit(limit)
            )
        )

    async def retry_outbound_message(self, tenant_id: str, message_id: str) -> OutboundMessage:
        message = await self.session.scalar(
            select(OutboundMessage)
            .where(
                OutboundMessage.id == message_id,
                OutboundMessage.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if message is None:
            raise NotFoundError("Outbound message not found")
        if message.status != "dead_letter":
            raise ConflictError("Only dead-letter messages can be retried")
        message.status = "retryable"
        message.attempts = 0
        message.available_at = datetime.now(UTC)
        message.error_code = None
        return message

    async def create_domain_pack_draft(
        self, tenant_id: str, pack: DomainPackConfig, created_by: str
    ) -> DomainPackRevision:
        await self.get_tenant(tenant_id)
        latest = await self.session.scalar(
            select(func.max(DomainPackRevision.version)).where(
                DomainPackRevision.tenant_id == tenant_id,
                DomainPackRevision.slug == pack.slug,
            )
        )
        revision = DomainPackRevision(
            tenant_id=tenant_id,
            slug=pack.slug,
            version=(latest or 0) + 1,
            status="draft",
            config_json=pack.model_dump_json(),
            checksum=pack.checksum(),
            created_by=created_by,
        )
        self.session.add(revision)
        await self.session.flush()
        return revision

    async def list_domain_packs(self, tenant_id: str) -> list[DomainPackRevision]:
        return list(
            await self.session.scalars(
                select(DomainPackRevision)
                .where(DomainPackRevision.tenant_id == tenant_id)
                .order_by(DomainPackRevision.slug, DomainPackRevision.version.desc())
            )
        )

    async def publish_domain_pack(self, tenant_id: str, revision_id: str) -> DomainPackRevision:
        revision = await self.session.scalar(
            select(DomainPackRevision).where(
                DomainPackRevision.id == revision_id,
                DomainPackRevision.tenant_id == tenant_id,
            )
        )
        if revision is None:
            raise NotFoundError("Domain Pack revision not found")
        if revision.status != "draft":
            raise ConflictError("Only a draft Domain Pack can be published")
        DomainPackConfig.model_validate_json(revision.config_json)
        await self.session.execute(
            update(DomainPackRevision)
            .where(
                DomainPackRevision.tenant_id == tenant_id,
                DomainPackRevision.slug == revision.slug,
                DomainPackRevision.status == "live",
            )
            .values(status="superseded")
        )
        revision.status = "live"
        revision.published_at = datetime.now(UTC)
        return revision

    async def get_live_domain_pack(
        self, tenant_id: str, slug: str | None = None
    ) -> DomainPackRevision:
        statement = select(DomainPackRevision).where(
            DomainPackRevision.tenant_id == tenant_id,
            DomainPackRevision.status == "live",
        )
        if slug:
            statement = statement.where(DomainPackRevision.slug == slug)
        revision = await self.session.scalar(
            statement.order_by(DomainPackRevision.published_at.desc()).limit(1)
        )
        if revision is None:
            raise NotFoundError("No live Domain Pack is configured")
        return revision

    async def create_connector(
        self,
        *,
        tenant_id: str,
        name: str,
        provider: str,
        base_url: str | None,
        credential_ref: str | None,
        capabilities: list[str],
        config: dict[str, Any],
    ) -> ConnectorDefinition:
        await self.get_tenant(tenant_id)
        connector = ConnectorDefinition(
            tenant_id=tenant_id,
            name=name,
            provider=provider,
            base_url=base_url,
            credential_ref=credential_ref,
            capabilities_json=json.dumps(capabilities),
            config_json=json.dumps(config),
        )
        self.session.add(connector)
        await self.session.flush()
        return connector

    async def get_connector(self, tenant_id: str, connector_id: str) -> ConnectorDefinition:
        connector = await self.session.scalar(
            select(ConnectorDefinition).where(
                ConnectorDefinition.id == connector_id,
                ConnectorDefinition.tenant_id == tenant_id,
            )
        )
        if connector is None:
            raise NotFoundError("Connector not found")
        return connector

    async def list_connectors(self, tenant_id: str) -> list[ConnectorDefinition]:
        return list(
            await self.session.scalars(
                select(ConnectorDefinition)
                .where(ConnectorDefinition.tenant_id == tenant_id)
                .order_by(ConnectorDefinition.created_at.desc())
            )
        )

    async def find_live_connector(
        self, tenant_id: str, action: str, provider: str | None = None
    ) -> ConnectorDefinition:
        connectors = await self.list_connectors(tenant_id)
        for connector in connectors:
            if (
                connector.status == "live"
                and (provider is None or connector.provider == provider)
                and action in json.loads(connector.capabilities_json)
            ):
                return connector
        raise NotFoundError(f"No live connector supports {action}")

    async def get_action_by_key(
        self, tenant_id: str, idempotency_key: str
    ) -> ActionExecution | None:
        action = await self.session.scalar(
            select(ActionExecution).where(
                ActionExecution.tenant_id == tenant_id,
                ActionExecution.idempotency_key == idempotency_key,
            )
        )
        return action

    async def create_action(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        conversation_id: str | None,
        connector_id: str,
        action: str,
        risk_tier: str,
        idempotency_key: str,
        request: dict[str, Any],
        status: str,
        confirmation_digest: str | None,
    ) -> ActionExecution:
        execution = ActionExecution(
            tenant_id=tenant_id,
            customer_id=customer_id,
            conversation_id=conversation_id,
            connector_id=connector_id,
            action=action,
            risk_tier=risk_tier,
            idempotency_key=idempotency_key,
            request_json=json.dumps(request, ensure_ascii=False, sort_keys=True),
            status=status,
            confirmation_digest=confirmation_digest,
        )
        self.session.add(execution)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError("Action idempotency key already exists") from exc
        return execution

    async def get_action(self, tenant_id: str, action_id: str) -> ActionExecution:
        action = await self.session.scalar(
            select(ActionExecution).where(
                ActionExecution.id == action_id,
                ActionExecution.tenant_id == tenant_id,
            )
        )
        if action is None:
            raise NotFoundError("Action execution not found")
        return action

    async def append_outbox(
        self, tenant_id: str, aggregate_id: str, event_type: str, payload: dict[str, Any]
    ) -> OutboxEvent:
        event = OutboxEvent(
            tenant_id=tenant_id,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def mark_action_outbox_delivered(self, tenant_id: str, action_id: str) -> None:
        await self.session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.tenant_id == tenant_id,
                OutboxEvent.aggregate_id == action_id,
                OutboxEvent.status == "pending",
            )
            .values(status="delivered", attempts=OutboxEvent.attempts + 1)
        )

    async def save_checkpoint(
        self,
        tenant_id: str,
        conversation_id: str,
        status: str,
        state: dict[str, Any],
    ) -> WorkflowCheckpoint:
        sequence = await self.session.scalar(
            select(func.max(WorkflowCheckpoint.sequence)).where(
                WorkflowCheckpoint.tenant_id == tenant_id,
                WorkflowCheckpoint.conversation_id == conversation_id,
            )
        )
        checkpoint = WorkflowCheckpoint(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            sequence=(sequence or 0) + 1,
            status=status,
            state_json=json.dumps(state, ensure_ascii=False),
        )
        self.session.add(checkpoint)
        await self.session.flush()
        return checkpoint

    async def create_knowledge_source(
        self,
        *,
        tenant_id: str,
        source_key: str,
        source_type: str,
        location: str | None,
        content: str,
        metadata: dict[str, Any],
        injection_flags: list[str],
    ) -> KnowledgeSource:
        latest = await self.session.scalar(
            select(func.max(KnowledgeSource.version)).where(
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.source_key == source_key,
            )
        )
        source = KnowledgeSource(
            tenant_id=tenant_id,
            source_key=source_key,
            source_type=source_type,
            version=(latest or 0) + 1,
            status="draft",
            location=location,
            checksum=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            content=content,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            injection_flags_json=json.dumps(injection_flags),
        )
        self.session.add(source)
        await self.session.flush()
        return source

    async def list_live_knowledge(self, tenant_id: str) -> list[KnowledgeSource]:
        return list(
            await self.session.scalars(
                select(KnowledgeSource).where(
                    KnowledgeSource.tenant_id == tenant_id,
                    KnowledgeSource.status == "live",
                )
            )
        )

    async def get_active_release(self, tenant_id: str) -> AgentRelease | None:
        release = await self.session.scalar(
            select(AgentRelease)
            .where(AgentRelease.tenant_id == tenant_id, AgentRelease.status == "active")
            .order_by(AgentRelease.activated_at.desc())
            .limit(1)
        )
        return release

    async def select_release(self, tenant_id: str, customer_id: str) -> AgentRelease | None:
        canary = await self.session.scalar(
            select(AgentRelease)
            .where(AgentRelease.tenant_id == tenant_id, AgentRelease.status == "canary")
            .order_by(AgentRelease.activated_at.desc())
            .limit(1)
        )
        champion = await self.get_active_release(tenant_id)
        if canary is None:
            return champion
        bucket = (
            int(hashlib.sha256(f"{tenant_id}:{customer_id}".encode()).hexdigest()[:8], 16) % 100
        )
        return canary if bucket < canary.canary_percent or champion is None else champion

    async def get_domain_pack_revision(
        self, tenant_id: str, revision_id: str
    ) -> DomainPackRevision:
        revision = await self.session.scalar(
            select(DomainPackRevision).where(
                DomainPackRevision.id == revision_id,
                DomainPackRevision.tenant_id == tenant_id,
            )
        )
        if revision is None:
            raise NotFoundError("Domain Pack revision not found")
        return revision

    async def activate_connector(self, tenant_id: str, connector_id: str) -> ConnectorDefinition:
        connector = await self.get_connector(tenant_id, connector_id)
        if connector.status not in {"draft", "disabled"}:
            raise ConflictError("Connector cannot be activated from its current state")
        connector.status = "live"
        return connector

    async def publish_knowledge(self, tenant_id: str, source_id: str) -> KnowledgeSource:
        source = await self.session.scalar(
            select(KnowledgeSource).where(
                KnowledgeSource.id == source_id,
                KnowledgeSource.tenant_id == tenant_id,
            )
        )
        if source is None:
            raise NotFoundError("Knowledge source not found")
        if source.status != "draft":
            raise ConflictError("Only draft knowledge can be published")
        if json.loads(source.injection_flags_json):
            raise ConflictError("Knowledge source failed the prompt-injection scan")
        await self.session.execute(
            update(KnowledgeSource)
            .where(
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.source_key == source.source_key,
                KnowledgeSource.status == "live",
            )
            .values(status="superseded")
        )
        source.status = "live"
        source.published_at = datetime.now(UTC)
        return source

    async def create_release(
        self,
        *,
        tenant_id: str,
        domain_pack_revision_id: str,
        artifact: dict[str, Any],
        evaluation: dict[str, Any],
        canary_percent: int,
    ) -> AgentRelease:
        revision = await self.session.scalar(
            select(DomainPackRevision).where(
                DomainPackRevision.id == domain_pack_revision_id,
                DomainPackRevision.tenant_id == tenant_id,
                DomainPackRevision.status == "live",
            )
        )
        if revision is None:
            raise ConflictError("Release requires a live Domain Pack revision")
        latest = await self.session.scalar(
            select(func.max(AgentRelease.version)).where(AgentRelease.tenant_id == tenant_id)
        )
        release = AgentRelease(
            tenant_id=tenant_id,
            version=(latest or 0) + 1,
            status="draft",
            domain_pack_revision_id=domain_pack_revision_id,
            artifact_json=json.dumps(artifact, ensure_ascii=False, sort_keys=True),
            evaluation_json=json.dumps(evaluation, ensure_ascii=False),
            canary_percent=canary_percent,
        )
        self.session.add(release)
        await self.session.flush()
        return release

    async def activate_release(
        self, tenant_id: str, release_id: str, approved_by: str
    ) -> AgentRelease:
        release = await self.session.scalar(
            select(AgentRelease).where(
                AgentRelease.id == release_id,
                AgentRelease.tenant_id == tenant_id,
            )
        )
        if release is None:
            raise NotFoundError("Agent release not found")
        if release.status != "draft":
            raise ConflictError("Only a draft release can be activated")
        evaluation = json.loads(release.evaluation_json)
        gate = evaluation.get("gate", {})
        if gate.get("passed") is not True:
            raise ConflictError("Release quality gate has not passed")
        target_status = "active" if release.canary_percent >= 100 else "canary"
        await self.session.execute(
            update(AgentRelease)
            .where(AgentRelease.tenant_id == tenant_id, AgentRelease.status == target_status)
            .values(status="superseded")
        )
        if target_status == "active":
            await self.session.execute(
                update(AgentRelease)
                .where(AgentRelease.tenant_id == tenant_id, AgentRelease.status == "canary")
                .values(status="superseded")
            )
        release.status = target_status
        release.approved_by = approved_by
        release.activated_at = datetime.now(UTC)
        return release

    async def promote_release(
        self, tenant_id: str, release_id: str, approved_by: str
    ) -> AgentRelease:
        release = await self.session.scalar(
            select(AgentRelease).where(
                AgentRelease.id == release_id,
                AgentRelease.tenant_id == tenant_id,
                AgentRelease.status == "canary",
            )
        )
        if release is None:
            raise ConflictError("Only a canary release can be promoted")
        await self.session.execute(
            update(AgentRelease)
            .where(AgentRelease.tenant_id == tenant_id, AgentRelease.status == "active")
            .values(status="superseded")
        )
        release.status = "active"
        release.canary_percent = 100
        release.approved_by = approved_by
        release.activated_at = datetime.now(UTC)
        return release

    async def rollback_release(
        self, tenant_id: str, release_id: str, approved_by: str
    ) -> AgentRelease:
        target = await self.session.scalar(
            select(AgentRelease).where(
                AgentRelease.id == release_id,
                AgentRelease.tenant_id == tenant_id,
                AgentRelease.status == "superseded",
            )
        )
        if target is None:
            raise ConflictError("Rollback target must be a superseded release")
        await self.session.execute(
            update(AgentRelease)
            .where(
                AgentRelease.tenant_id == tenant_id,
                AgentRelease.status.in_(["active", "canary"]),
            )
            .values(status="superseded")
        )
        target.status = "active"
        target.canary_percent = 100
        target.approved_by = approved_by
        target.activated_at = datetime.now(UTC)
        return target

    async def record_outcome(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        run_id: str | None,
        resolved: bool,
        source: str,
        first_contact: bool | None,
        reopened: bool,
    ) -> ResolutionOutcome:
        outcome = ResolutionOutcome(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            run_id=run_id,
            resolved=resolved,
            source=source,
            first_contact=first_contact,
            reopened=reopened,
        )
        self.session.add(outcome)
        await self.session.flush()
        return outcome

    async def list_outcomes(self, tenant_id: str, limit: int = 10_000) -> list[ResolutionOutcome]:
        return list(
            await self.session.scalars(
                select(ResolutionOutcome)
                .where(ResolutionOutcome.tenant_id == tenant_id)
                .order_by(ResolutionOutcome.created_at.desc())
                .limit(limit)
            )
        )

    async def list_actions(self, tenant_id: str, limit: int = 10_000) -> list[ActionExecution]:
        return list(
            await self.session.scalars(
                select(ActionExecution)
                .where(ActionExecution.tenant_id == tenant_id)
                .order_by(ActionExecution.created_at.desc())
                .limit(limit)
            )
        )

    async def record_webhook(
        self,
        *,
        tenant_id: str,
        connector_id: str,
        provider: str,
        external_id: str,
        payload_hash: str,
        signature_valid: bool,
    ) -> WebhookEvent:
        event = WebhookEvent(
            tenant_id=tenant_id,
            connector_id=connector_id,
            provider=provider,
            external_id=external_id,
            payload_hash=payload_hash,
            signature_valid=signature_valid,
            status="received" if signature_valid else "rejected",
        )
        self.session.add(event)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError("Webhook event was already processed") from exc
        return event

    async def get_webhook(
        self, tenant_id: str, connector_id: str, external_id: str
    ) -> WebhookEvent | None:
        event: WebhookEvent | None = await self.session.scalar(
            select(WebhookEvent).where(
                WebhookEvent.tenant_id == tenant_id,
                WebhookEvent.connector_id == connector_id,
                WebhookEvent.external_id == external_id,
            )
        )
        return event

    async def complete_webhook(
        self, event: WebhookEvent, *, conversation_id: str, message_id: str
    ) -> None:
        event.status = "processed"
        event.conversation_id = conversation_id
        event.message_id = message_id
        event.processed_at = datetime.now(UTC)

    async def record_audit(
        self,
        *,
        tenant_id: str,
        actor: str,
        role: str,
        method: str,
        path: str,
        status_code: int,
        request_id: str | None,
        resource_id: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            tenant_id=tenant_id,
            actor=actor,
            role=role,
            method=method,
            path=path,
            status_code=status_code,
            request_id=request_id,
            resource_id=resource_id,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def list_audit_events(self, tenant_id: str, limit: int = 200) -> list[AuditEvent]:
        return list(
            await self.session.scalars(
                select(AuditEvent)
                .where(AuditEvent.tenant_id == tenant_id)
                .order_by(AuditEvent.created_at.desc())
                .limit(limit)
            )
        )

    async def create_evaluation_candidate(
        self,
        *,
        tenant_id: str,
        run: AgentRun,
        source: str,
        expected: dict[str, Any],
        evidence: dict[str, Any],
    ) -> EvaluationCandidate:
        existing = await self.session.scalar(
            select(EvaluationCandidate).where(
                EvaluationCandidate.tenant_id == tenant_id,
                EvaluationCandidate.run_id == run.id,
                EvaluationCandidate.source == source,
            )
        )
        if existing:
            return existing
        candidate = EvaluationCandidate(
            tenant_id=tenant_id,
            run_id=run.id,
            source=source,
            input_hash=run.input_hash,
            expected_json=json.dumps(expected, ensure_ascii=False),
            evidence_json=json.dumps(evidence, ensure_ascii=False),
        )
        self.session.add(candidate)
        await self.session.flush()
        return candidate

    async def list_evaluation_candidates(
        self, tenant_id: str, status: str = "candidate", limit: int = 1_000
    ) -> list[EvaluationCandidate]:
        return list(
            await self.session.scalars(
                select(EvaluationCandidate)
                .where(
                    EvaluationCandidate.tenant_id == tenant_id,
                    EvaluationCandidate.status == status,
                )
                .order_by(EvaluationCandidate.created_at.desc())
                .limit(limit)
            )
        )

    async def review_evaluation_candidate(
        self, tenant_id: str, candidate_id: str, accepted: bool, reviewer: str
    ) -> EvaluationCandidate:
        candidate = await self.session.scalar(
            select(EvaluationCandidate).where(
                EvaluationCandidate.id == candidate_id,
                EvaluationCandidate.tenant_id == tenant_id,
            )
        )
        if candidate is None:
            raise NotFoundError("Evaluation candidate not found")
        if candidate.status != "candidate":
            raise ConflictError("Evaluation candidate was already reviewed")
        candidate.status = "accepted" if accepted else "rejected"
        candidate.reviewed_by = reviewer
        candidate.reviewed_at = datetime.now(UTC)
        return candidate

    async def build_dataset_revision(
        self, tenant_id: str, created_by: str
    ) -> EvaluationDatasetRevision:
        candidates = list(
            await self.session.scalars(
                select(EvaluationCandidate).where(
                    EvaluationCandidate.tenant_id == tenant_id,
                    EvaluationCandidate.status == "accepted",
                )
            )
        )
        if not candidates:
            raise ConflictError("No accepted evaluation candidates are available")
        cases = [
            {
                "candidate_id": item.id,
                "run_id": item.run_id,
                "input_hash": item.input_hash,
                "expected": json.loads(item.expected_json),
                "evidence": json.loads(item.evidence_json),
            }
            for item in candidates
        ]
        canonical = json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        latest = await self.session.scalar(
            select(func.max(EvaluationDatasetRevision.version)).where(
                EvaluationDatasetRevision.tenant_id == tenant_id
            )
        )
        revision = EvaluationDatasetRevision(
            tenant_id=tenant_id,
            version=(latest or 0) + 1,
            cases_json=canonical,
            checksum=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            created_by=created_by,
        )
        self.session.add(revision)
        await self.session.flush()
        return revision
