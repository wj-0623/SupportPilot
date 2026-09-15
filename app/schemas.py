from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    customer_id: str | None = Field(
        default=None, min_length=1, max_length=64, examples=["demo-001"]
    )
    message: str = Field(min_length=1, max_length=4_000, examples=["查询订单 ORD-1002"])
    conversation_id: str | None = Field(default=None, max_length=36)


class Citation(BaseModel):
    source_id: str
    title: str


class PendingAction(BaseModel):
    id: str
    action: str
    status: str
    confirmation_digest: str


class ChatResponse(BaseModel):
    conversation_id: str
    message_id: str
    reply: str
    intent: str
    mode: Literal["workflow", "agent", "offline_fallback"]
    citations: list[Citation] = Field(default_factory=list)
    ticket_id: str | None = None
    pending_action: PendingAction | None = None
    trace: list[str] = Field(default_factory=list)


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    intent: str | None
    metadata: dict[str, Any]
    created_at: datetime


class ConversationResponse(BaseModel):
    id: str
    customer_id: str
    status: str
    messages: list[MessageResponse]
    created_at: datetime
    updated_at: datetime


class TicketResponse(BaseModel):
    id: str
    conversation_id: str
    customer_id: str
    order_id: str | None
    status: str
    priority: str
    reason: str
    resolution: str | None
    created_at: datetime
    resolved_at: datetime | None


class ResolveTicketRequest(BaseModel):
    resolution: str = Field(min_length=2, max_length=2_000)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str = "3.0.0"
    llm_mode: Literal["openai", "offline"]


class FeedbackRequest(BaseModel):
    customer_id: str | None = Field(default=None, min_length=1, max_length=64)
    rating: Literal[-1, 1]
    resolved: bool | None = None
    reason: Literal["incorrect", "irrelevant", "unclear", "handoff", "other"] | None = None
    comment: str | None = Field(default=None, max_length=1_000)


class FeedbackResponse(BaseModel):
    id: str
    run_id: str
    message_id: str
    rating: int
    resolved: bool | None
    reason: str | None
    created_at: datetime


class AgentRunResponse(BaseModel):
    id: str
    conversation_id: str | None
    customer_id: str
    message_id: str | None
    status: str
    intent: str | None
    mode: str | None
    route_confidence: float | None
    duration_ms: float
    tool_calls: int
    input_tokens: int
    output_tokens: int
    handoff: bool
    citation_count: int
    prompt_version: str
    router_version: str
    policy_version: str
    model_name: str | None
    safety_flags: list[str]
    trace: list[str]
    observations: list[dict[str, Any]]
    error_type: str | None
    created_at: datetime


class RunReviewRequest(BaseModel):
    expected_intent: str | None = Field(default=None, max_length=64)
    quality_score: int = Field(ge=1, le=5)
    notes: str | None = Field(default=None, max_length=2_000)
    reviewer: str = Field(default="ops", min_length=1, max_length=120)


class RunReviewResponse(BaseModel):
    id: str
    run_id: str
    expected_intent: str | None
    quality_score: int
    notes: str | None
    reviewer: str
    created_at: datetime


class QualitySnapshot(BaseModel):
    sample_size: int
    successful_runs: int
    error_rate: float
    automation_rate: float
    containment_rate: float | None = None
    handoff_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    positive_feedback_rate: float | None
    resolved_feedback_rate: float | None
    confirmed_resolution_rate: float | None = None
    first_contact_resolution_rate: float | None = None
    action_success_rate: float | None = None
    grounded_answer_rate: float | None = None
    reviewed_run_rate: float
    labeled_routing_accuracy: float | None
    top_intents: dict[str, int]
    negative_feedback_reasons: dict[str, int]
    versions: dict[str, list[str]]


class DomainPackRevisionResponse(BaseModel):
    id: str
    tenant_id: str
    slug: str
    version: int
    status: str
    checksum: str
    config: dict[str, Any]
    created_by: str
    created_at: datetime
    published_at: datetime | None


class ConnectorCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    provider: Literal["mock", "generic-rest", "shopify", "chatwoot"]
    base_url: str | None = Field(default=None, max_length=500)
    credential_ref: str | None = Field(default=None, max_length=300)
    capabilities: list[str] = Field(default_factory=list, max_length=30)
    config: dict[str, Any] = Field(default_factory=dict)


class ConnectorResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    provider: str
    version: int
    status: str
    base_url: str | None
    credential_ref: str | None
    capabilities: list[str]
    config: dict[str, Any]
    created_at: datetime


class ActionPlanRequest(BaseModel):
    connector_id: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=80)
    conversation_id: str | None = Field(default=None, max_length=36)
    parameters: dict[str, Any] = Field(default_factory=dict)


class ActionConfirmRequest(BaseModel):
    confirmation_digest: str = Field(min_length=64, max_length=64)


class ActionResponse(BaseModel):
    id: str
    tenant_id: str
    customer_id: str
    conversation_id: str | None
    connector_id: str
    action: str
    risk_tier: str
    status: str
    idempotency_key: str
    parameters: dict[str, Any]
    result: dict[str, Any] | None
    error_code: str | None
    confirmation_digest: str | None
    confirmed_by: str | None
    created_at: datetime
    updated_at: datetime


class KnowledgeSourceCreateRequest(BaseModel):
    source_key: str = Field(min_length=2, max_length=160)
    source_type: Literal["text", "file", "url", "catalog", "help-center", "api"] = "text"
    location: str | None = Field(default=None, max_length=1_000)
    content: str = Field(min_length=1, max_length=1_000_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSourceResponse(BaseModel):
    id: str
    tenant_id: str
    source_key: str
    source_type: str
    version: int
    status: str
    location: str | None
    checksum: str
    injection_flags: list[str]
    synced_at: datetime
    published_at: datetime | None


class KnowledgeSyncRequest(BaseModel):
    source_key: str = Field(min_length=2, max_length=160)
    source_type: Literal["url", "sitemap", "help-center", "catalog", "api"]
    location: str = Field(min_length=8, max_length=1_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReleaseCreateRequest(BaseModel):
    domain_pack_revision_id: str = Field(min_length=1, max_length=64)
    canary_percent: int = Field(default=5, ge=0, le=100)
    evaluation: dict[str, Any]


class ResolutionOutcomeRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=36)
    run_id: str | None = Field(default=None, max_length=36)
    resolved: bool
    first_contact: bool | None = None
    reopened: bool = False


class EvaluationCandidateReviewRequest(BaseModel):
    accepted: bool


class SimulationCaseRequest(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    customer_id: str = Field(default="demo-001", min_length=1, max_length=64)
    turns: list[str] = Field(min_length=1, max_length=10)


class SimulationRequest(BaseModel):
    domain_pack_revision_id: str = Field(min_length=1, max_length=64)
    cases: list[SimulationCaseRequest] = Field(min_length=1, max_length=100)
