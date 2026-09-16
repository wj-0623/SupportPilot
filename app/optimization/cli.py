from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from app.agent.versions import POLICY_VERSION, PROMPT_VERSION, ROUTER_VERSION
from app.core.config import ROOT_DIR, Settings
from app.db.database import Database
from app.db.repository import SupportRepository
from app.ops.quality import build_quality_snapshot
from app.optimization.analyzer import build_optimization_report


def _load_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _markdown(report: dict[str, Any]) -> str:
    production = report["production_snapshot"]
    lines = [
        "# SupportPilot optimization report",
        "",
        f"Generated: {report['generated_at']}",
        f"Release decision: **{report['release_decision']}**",
        f"Reason: {report['decision_reason']}",
        "",
        "优化建议不会自动修改提示词、路由或业务政策。每项变更均需人工审阅并重新通过发布评测。",
        "",
        "## Production snapshot",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| sample_size | {production['sample_size']} |",
        f"| error_rate | {production['error_rate']} |",
        f"| automation_rate | {production['automation_rate']} |",
        f"| handoff_rate | {production['handoff_rate']} |",
        f"| p95_latency_ms | {production['p95_latency_ms']} |",
        f"| positive_feedback_rate | {production['positive_feedback_rate']} |",
        f"| reviewed_run_rate | {production['reviewed_run_rate']} |",
        f"| labeled_routing_accuracy | {production['labeled_routing_accuracy']} |",
        "",
        "## Prioritized recommendations",
        "",
    ]
    if not report["recommendations"]:
        lines.append("No threshold violations detected.")
    for item in report["recommendations"]:
        lines.extend(
            [
                f"### {item['priority']} · {item['area']}",
                "",
                f"- Evidence: {item['evidence']}",
                f"- Action: {item['action']}",
                f"- Validation: {item['validation']}",
                "",
            ]
        )
    lines.extend(["## Controlled optimization workflow", ""])
    lines.extend(f"{index}. {step}" for index, step in enumerate(report["workflow"], 1))
    return "\n".join(lines) + "\n"


async def generate_report(
    evaluation_path: Path, policy_path: Path, database_url: str
) -> dict[str, Any]:
    database = Database(database_url)
    await database.create_schema()
    try:
        async with database.sessions() as session:
            repo = SupportRepository(session)
            runs = await repo.list_agent_runs(
                limit=10_000,
                prompt_version=PROMPT_VERSION,
                router_version=ROUTER_VERSION,
                policy_version=POLICY_VERSION,
            )
            feedback = await repo.list_feedback(limit=10_000)
            reviews = await repo.list_run_reviews(limit=10_000)
        snapshot = build_quality_snapshot(runs, feedback, reviews)
    finally:
        await database.dispose()
    report = build_optimization_report(
        _load_json(evaluation_path), snapshot, _load_json(policy_path)
    )
    report["generated_at"] = datetime.now(UTC).isoformat()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate SupportPilot optimization advice")
    parser.add_argument(
        "--evaluation",
        type=Path,
        default=ROOT_DIR / "artifacts" / "evaluation" / "latest.json",
    )
    parser.add_argument(
        "--policy", type=Path, default=ROOT_DIR / "config" / "optimization_policy.json"
    )
    parser.add_argument("--output", type=Path, default=ROOT_DIR / "artifacts" / "optimization")
    args = parser.parse_args()
    settings = Settings()
    if not args.evaluation.exists():
        parser.error("evaluation report not found; run python -m app.evaluation.runner first")
    report = asyncio.run(generate_report(args.evaluation, args.policy, settings.database_url))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = _markdown(report)
    (args.output / "latest.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    return 1 if report["release_decision"] == "BLOCK" else 0


if __name__ == "__main__":
    raise SystemExit(main())
