from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Any, cast

from app.agent.graph import SupportGraph
from app.agent.versions import POLICY_VERSION, PROMPT_VERSION, ROUTER_VERSION
from app.core.config import ROOT_DIR, Settings
from app.db.database import Database
from app.db.seed import seed_demo_data
from app.evaluation.models import CaseResult, EvaluationCase
from app.knowledge import KnowledgeBase
from app.schemas import ChatRequest, ChatResponse
from app.service import SupportService


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[max(0, ceil(len(ordered) * percentile) - 1)], 3)


def _load_cases(path: Path) -> list[EvaluationCase]:
    payload = cast(list[dict[str, Any]], json.loads(path.read_text(encoding="utf-8")))
    return [EvaluationCase.model_validate(item) for item in payload]


def _path_exists(path: Path) -> bool:
    return path.exists()


def _score_case(case: EvaluationCase, response: ChatResponse, latency_ms: float) -> CaseResult:
    citation_ids = {citation.source_id for citation in response.citations}
    checks = {
        "intent": response.intent == case.expected.intent,
        "mode": response.mode == case.expected.mode,
        "handoff": bool(response.ticket_id) == case.expected.ticket,
        "content": all(text in response.reply for text in case.expected.must_contain),
        "forbidden_content": all(
            text not in response.reply for text in case.expected.must_not_contain
        ),
        "citations": set(case.expected.citation_ids).issubset(citation_ids),
    }
    return CaseResult(
        id=case.id,
        category=case.category,
        passed=all(checks.values()),
        checks=checks,
        actual={
            "intent": response.intent,
            "mode": response.mode,
            "ticket": bool(response.ticket_id),
            "citation_required": bool(case.expected.citation_ids),
            "citation_ids": sorted(citation_ids),
            "reply": response.reply,
        },
        latency_ms=latency_ms,
    )


def _metric(results: list[CaseResult], check: str) -> float:
    if not results:
        return 0.0
    return round(sum(item.checks[check] for item in results) / len(results), 4)


def _build_metrics(results: list[CaseResult]) -> dict[str, float | int]:
    policy_results = [
        result for result in results if result.category in {"policy", "safety", "privacy"}
    ]
    citation_results = [result for result in results if bool(result.actual["citation_required"])]
    return {
        "case_count": len(results),
        "pass_rate": round(sum(result.passed for result in results) / len(results), 4),
        "routing_accuracy": _metric(results, "intent"),
        "mode_accuracy": _metric(results, "mode"),
        "handoff_accuracy": _metric(results, "handoff"),
        "content_accuracy": round(
            sum(
                result.checks["content"] and result.checks["forbidden_content"]
                for result in results
            )
            / len(results),
            4,
        ),
        "citation_accuracy": (_metric(citation_results, "citations") if citation_results else 1.0),
        "policy_safety_rate": (
            round(sum(result.passed for result in policy_results) / len(policy_results), 4)
            if policy_results
            else 1.0
        ),
        "p50_latency_ms": _percentile([item.latency_ms for item in results], 0.50),
        "p95_latency_ms": _percentile([item.latency_ms for item in results], 0.95),
    }


def _evaluate_gate(
    metrics: dict[str, float | int], gate: dict[str, Any], baseline: dict[str, Any] | None
) -> tuple[bool, list[str]]:
    violations: list[str] = []
    for name, minimum in gate.get("minimums", {}).items():
        if metrics.get(name, 0) < minimum:
            violations.append(f"{name}={metrics.get(name)} is below minimum {minimum}")
    for name, maximum in gate.get("maximums", {}).items():
        if metrics.get(name, float("inf")) > maximum:
            violations.append(f"{name}={metrics.get(name)} exceeds maximum {maximum}")
    if baseline:
        allowed_regression = float(gate.get("allowed_regression", 0.0))
        for name, value in baseline.get("metrics", {}).items():
            if name in {"p50_latency_ms", "p95_latency_ms", "case_count"}:
                continue
            if float(metrics.get(name, 0)) < float(value) - allowed_regression:
                violations.append(f"{name} regressed from baseline {value} to {metrics.get(name)}")
    return not violations, violations


def _markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "# ShopSage evaluation report",
        "",
        f"Generated: {report['generated_at']}",
        f"Release gate: **{'PASS' if report['gate']['passed'] else 'FAIL'}**",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {name} | {value} |" for name, value in metrics.items())
    lines.extend(
        [
            "",
            "## Cases",
            "",
            "| Case | Category | Result | Latency (ms) | Failed checks |",
            "| --- | --- | --- | ---: | --- |",
        ]
    )
    for result in report["results"]:
        failed = ", ".join(name for name, passed in result["checks"].items() if not passed)
        lines.append(
            f"| {result['id']} | {result['category']} | "
            f"{'PASS' if result['passed'] else 'FAIL'} | {result['latency_ms']} | {failed or '-'} |"
        )
    if report["gate"]["violations"]:
        lines.extend(["", "## Gate violations", ""])
        lines.extend(f"- {item}" for item in report["gate"]["violations"])
    return "\n".join(lines) + "\n"


async def run_evaluation(
    dataset_path: Path, gate_path: Path, baseline_path: Path | None
) -> dict[str, Any]:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        llm_enabled=False,
        openai_api_key=None,
        expose_debug_trace=True,
    )
    database = Database(settings.database_url)
    await database.create_schema()
    await seed_demo_data(database, settings)
    service = SupportService(
        SupportGraph(settings, KnowledgeBase(settings.knowledge_base_path, settings.catalog_path)),
        settings,
    )
    results: list[CaseResult] = []
    try:
        for case in _load_cases(dataset_path):
            conversation_id: str | None = None
            final_response: ChatResponse | None = None
            started = asyncio.get_running_loop().time()
            async with database.sessions() as session:
                for turn in case.turns:
                    final_response = await service.chat(
                        session,
                        ChatRequest(
                            customer_id=case.customer_id,
                            conversation_id=conversation_id,
                            message=turn,
                        ),
                        idempotency_key=None,
                        tenant_id=settings.default_tenant_id,
                        customer_id=case.customer_id,
                    )
                    conversation_id = final_response.conversation_id
            latency_ms = round((asyncio.get_running_loop().time() - started) * 1_000, 3)
            if final_response is None:
                raise RuntimeError(f"Evaluation case {case.id} produced no response")
            results.append(_score_case(case, final_response, latency_ms))
    finally:
        await database.dispose()

    metrics = _build_metrics(results)
    gate_text = await asyncio.to_thread(gate_path.read_text, encoding="utf-8")
    gate = cast(dict[str, Any], json.loads(gate_text))
    baseline = None
    baseline_exists = False
    if baseline_path is not None:
        baseline_exists = await asyncio.to_thread(_path_exists, baseline_path)
    if baseline_path is not None and baseline_exists:
        baseline_text = await asyncio.to_thread(baseline_path.read_text, encoding="utf-8")
        baseline = cast(dict[str, Any], json.loads(baseline_text))
    gate_passed, violations = _evaluate_gate(metrics, gate, baseline)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": str(dataset_path),
        "versions": {
            "prompt": PROMPT_VERSION,
            "router": ROUTER_VERSION,
            "policy": POLICY_VERSION,
        },
        "metrics": metrics,
        "gate": {"passed": gate_passed, "violations": violations},
        "results": [item.model_dump() for item in results],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ShopSage offline release evaluation")
    parser.add_argument("--dataset", type=Path, default=ROOT_DIR / "evaluations" / "golden.json")
    parser.add_argument("--gate", type=Path, default=ROOT_DIR / "config" / "quality_gates.json")
    parser.add_argument("--baseline", type=Path, default=ROOT_DIR / "evaluations" / "baseline.json")
    parser.add_argument("--output", type=Path, default=ROOT_DIR / "artifacts" / "evaluation")
    args = parser.parse_args()
    report = asyncio.run(run_evaluation(args.dataset, args.gate, args.baseline))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output / "latest.md").write_text(_markdown(report), encoding="utf-8")
    print(_markdown(report))
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
