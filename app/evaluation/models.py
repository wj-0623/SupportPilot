from __future__ import annotations

from pydantic import BaseModel, Field


class EvaluationExpected(BaseModel):
    intent: str
    mode: str
    ticket: bool
    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    citation_ids: list[str] = Field(default_factory=list)


class EvaluationCase(BaseModel):
    id: str
    category: str
    customer_id: str = "demo-001"
    turns: list[str] = Field(min_length=1)
    expected: EvaluationExpected


class CaseResult(BaseModel):
    id: str
    category: str
    passed: bool
    checks: dict[str, bool]
    actual: dict[str, object]
    latency_ms: float
