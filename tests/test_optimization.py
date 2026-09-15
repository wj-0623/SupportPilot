from app.optimization.analyzer import build_optimization_report
from app.schemas import QualitySnapshot


def _snapshot(sample_size: int) -> QualitySnapshot:
    return QualitySnapshot(
        sample_size=sample_size,
        successful_runs=sample_size,
        error_rate=0.0,
        automation_rate=0.9,
        handoff_rate=0.1,
        p50_latency_ms=20.0,
        p95_latency_ms=50.0,
        positive_feedback_rate=0.95,
        resolved_feedback_rate=0.9,
        reviewed_run_rate=0.2,
        labeled_routing_accuracy=0.98,
        top_intents={"faq": sample_size},
        negative_feedback_reasons={},
        versions={"prompt": ["v1"], "router": ["v1"], "policy": ["v1"]},
    )


POLICY = {
    "minimum_production_samples": 100,
    "production_thresholds": {
        "max_error_rate": 0.02,
        "max_p95_latency_ms": 1500.0,
        "max_handoff_rate": 0.35,
        "min_positive_feedback_rate": 0.8,
        "min_reviewed_run_rate": 0.1,
        "min_labeled_routing_accuracy": 0.95,
    },
}


def test_optimizer_requires_canary_when_production_evidence_is_small() -> None:
    evaluation = {"gate": {"passed": True, "violations": []}, "metrics": {}, "results": []}
    report = build_optimization_report(evaluation, _snapshot(10), POLICY)
    assert report["release_decision"] == "CANARY"
    assert any(item["area"] == "sample_size" for item in report["recommendations"])


def test_optimizer_blocks_failed_release_gate() -> None:
    evaluation = {
        "gate": {"passed": False, "violations": ["routing_accuracy below minimum"]},
        "metrics": {"routing_accuracy": 0.8},
        "results": [
            {
                "id": "bad-route",
                "category": "routing",
                "passed": False,
                "checks": {"intent": False},
            }
        ],
    }
    report = build_optimization_report(evaluation, _snapshot(100), POLICY)
    assert report["release_decision"] == "BLOCK"
    assert report["human_approval_required"] is True
