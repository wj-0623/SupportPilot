from __future__ import annotations

from collections import Counter
from math import ceil

from app.db.models import ActionExecution, AgentRun, Feedback, ResolutionOutcome, RunReview
from app.schemas import QualitySnapshot


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def build_quality_snapshot(
    runs: list[AgentRun],
    feedback: list[Feedback],
    reviews: list[RunReview],
    outcomes: list[ResolutionOutcome] | None = None,
    actions: list[ActionExecution] | None = None,
) -> QualitySnapshot:
    outcomes = outcomes or []
    actions = actions or []
    run_ids = {run.id for run in runs}
    feedback = [item for item in feedback if item.run_id in run_ids]
    reviews = [item for item in reviews if item.run_id in run_ids]
    successful = [run for run in runs if run.status == "success"]
    automated = [run for run in successful if not run.handoff]
    positive = [item for item in feedback if item.rating == 1]
    resolved_feedback = [item for item in feedback if item.resolved is True]
    feedback_with_resolution = [item for item in feedback if item.resolved is not None]
    run_by_id = {run.id: run for run in runs}
    labeled_reviews = [
        review
        for review in reviews
        if review.expected_intent is not None and review.run_id in run_by_id
    ]
    correct_labels = sum(
        run_by_id[review.run_id].intent == review.expected_intent for review in labeled_reviews
    )
    confirmed = [item for item in outcomes if not item.reopened]
    resolved_outcomes = [item for item in confirmed if item.resolved]
    first_contact = [item for item in confirmed if item.first_contact is not None]
    first_contact_resolved = [
        item for item in first_contact if item.resolved and item.first_contact
    ]
    terminal_actions = [item for item in actions if item.status in {"succeeded", "failed"}]
    succeeded_actions = [item for item in terminal_actions if item.status == "succeeded"]
    knowledge_runs = [item for item in successful if item.intent in {"faq", "product"}]
    grounded_runs = [item for item in knowledge_runs if item.citation_count > 0]
    return QualitySnapshot(
        sample_size=len(runs),
        successful_runs=len(successful),
        error_rate=_rate(len(runs) - len(successful), len(runs)),
        automation_rate=_rate(len(automated), len(successful)),
        containment_rate=_rate(len(automated), len(successful)),
        handoff_rate=_rate(len(successful) - len(automated), len(successful)),
        p50_latency_ms=_percentile([run.duration_ms for run in runs], 0.50),
        p95_latency_ms=_percentile([run.duration_ms for run in runs], 0.95),
        positive_feedback_rate=(_rate(len(positive), len(feedback)) if feedback else None),
        resolved_feedback_rate=(
            _rate(len(resolved_feedback), len(feedback_with_resolution))
            if feedback_with_resolution
            else None
        ),
        confirmed_resolution_rate=(
            _rate(len(resolved_outcomes), len(confirmed)) if confirmed else None
        ),
        first_contact_resolution_rate=(
            _rate(len(first_contact_resolved), len(first_contact)) if first_contact else None
        ),
        action_success_rate=(
            _rate(len(succeeded_actions), len(terminal_actions)) if terminal_actions else None
        ),
        grounded_answer_rate=(
            _rate(len(grounded_runs), len(knowledge_runs)) if knowledge_runs else None
        ),
        reviewed_run_rate=_rate(len(reviews), len(runs)),
        labeled_routing_accuracy=(
            _rate(correct_labels, len(labeled_reviews)) if labeled_reviews else None
        ),
        top_intents=dict(Counter(run.intent or "unknown" for run in runs).most_common(10)),
        negative_feedback_reasons=dict(
            Counter(item.reason or "unspecified" for item in feedback if item.rating == -1)
        ),
        versions={
            "prompt": sorted({run.prompt_version for run in runs}),
            "router": sorted({run.router_version for run in runs}),
            "policy": sorted({run.policy_version for run in runs}),
        },
    )
