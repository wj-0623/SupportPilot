from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypedDict

from app.db.repository import SupportRepository
from app.domain.config import DomainPackConfig


class SupportState(TypedDict, total=False):
    tenant_id: str
    domain_pack: DomainPackConfig
    knowledge_documents: list[dict[str, str]]
    customer_id: str
    conversation_id: str
    channel: str
    message: str
    sanitized_message: str
    history: list[dict[str, str]]
    safe_for_agent: bool
    safety_reasons: list[str]
    intent: str
    intents: list[str]
    confidence: float
    order_id: str | None
    order_id_inferred: bool
    response: str
    mode: str
    citations: list[dict[str, str]]
    ticket_id: str | None
    needs_human: bool
    ticket_reason: str
    ticket_priority: str
    repo: SupportRepository
    trace: Annotated[list[str], add]
    observations: Annotated[list[dict[str, Any]], add]
    token_usage: dict[str, int]
    agent_context: dict[str, Any]
    proposed_action: dict[str, Any]
