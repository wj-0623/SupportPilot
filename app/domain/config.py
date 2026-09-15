from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PersonaConfig(BaseModel):
    name: str = "ShopSage"
    tone: str = "professional, concise and empathetic"
    languages: list[str] = Field(default_factory=lambda: ["zh-CN", "en"])
    response_rules: list[str] = Field(default_factory=list)


class IntentConfig(BaseModel):
    name: str
    keywords: list[str]
    route: Literal["workflow", "agent", "handoff"]
    confidence_threshold: float = Field(default=0.72, ge=0, le=1)


class ReturnPolicyConfig(BaseModel):
    window_days: int = Field(default=30, ge=0, le=365)
    require_human_approval: bool = True
    final_sale_tags: list[str] = Field(default_factory=lambda: ["final-sale"])
    excluded_categories: list[str] = Field(default_factory=list)


class PolicyConfig(BaseModel):
    returns: ReturnPolicyConfig = Field(default_factory=ReturnPolicyConfig)
    cancellations_allowed_statuses: list[str] = Field(default_factory=lambda: ["processing"])
    address_change_allowed_statuses: list[str] = Field(default_factory=lambda: ["processing"])
    discount_max_percent: float = Field(default=15, ge=0, le=100)
    customer_confirmation_actions: list[str] = Field(
        default_factory=lambda: [
            "cancel_order",
            "create_return",
            "exchange_item",
            "change_address",
            "apply_discount",
            "cancel_subscription",
        ]
    )
    human_approval_actions: list[str] = Field(
        default_factory=lambda: ["refund_payment", "discount_above_limit"]
    )


class ToolPermission(BaseModel):
    action: str
    enabled: bool = True
    risk: Literal["read", "low", "medium", "high"] = "read"
    roles: list[str] = Field(default_factory=lambda: ["customer", "agent", "admin"])


class KnowledgeConfig(BaseModel):
    minimum_score: float = Field(default=0.2, ge=0, le=1)
    max_hits: int = Field(default=3, ge=1, le=10)
    require_citations: bool = True
    refuse_when_ungrounded: bool = True
    freshness_hours: int = Field(default=24, ge=1, le=8760)


class HandoffConfig(BaseModel):
    enabled: bool = True
    provider: str = "internal"
    urgent_minutes: int = Field(default=15, ge=1)
    normal_minutes: int = Field(default=240, ge=1)
    triggers: list[str] = Field(
        default_factory=lambda: ["customer_request", "policy_exception", "low_confidence"]
    )


class SafetyConfig(BaseModel):
    max_agent_steps: int = Field(default=4, ge=1, le=12)
    max_intents_per_turn: int = Field(default=3, ge=1, le=5)
    deny_prompt_injection: bool = True
    redact_sensitive_data: bool = True
    allowed_connector_hosts: list[str] = Field(default_factory=list)


class QualityGateConfig(BaseModel):
    min_routing_accuracy: float = Field(default=0.95, ge=0, le=1)
    min_action_success_rate: float = Field(default=0.98, ge=0, le=1)
    min_grounded_answer_rate: float = Field(default=0.95, ge=0, le=1)
    max_cross_tenant_failures: int = Field(default=0, ge=0)
    max_error_rate: float = Field(default=0.02, ge=0, le=1)


class DomainPackConfig(BaseModel):
    """A portable, versioned definition of an e-commerce support agent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["3.0"] = "3.0"
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    name: str
    vertical: str = "general-commerce"
    default_locale: str = "zh-CN"
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    intents: list[IntentConfig]
    policies: PolicyConfig = Field(default_factory=PolicyConfig)
    tools: list[ToolPermission]
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    handoff: HandoffConfig = Field(default_factory=HandoffConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    quality_gates: QualityGateConfig = Field(default_factory=QualityGateConfig)
    workflow_order: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_names(self) -> DomainPackConfig:
        intent_names = [intent.name for intent in self.intents]
        tool_names = [tool.action for tool in self.tools]
        if len(intent_names) != len(set(intent_names)):
            raise ValueError("intent names must be unique")
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("tool actions must be unique")
        unknown = set(self.workflow_order) - set(intent_names)
        if unknown:
            raise ValueError(f"workflow_order references unknown intents: {sorted(unknown)}")
        return self

    def checksum(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def intent_patterns(self) -> dict[str, tuple[str, ...]]:
        return {item.name: tuple(item.keywords) for item in self.intents}

    def tool_permission(self, action: str) -> ToolPermission | None:
        return next((item for item in self.tools if item.action == action), None)
