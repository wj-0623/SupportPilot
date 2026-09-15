from __future__ import annotations

from typing import Any

from app.schemas import QualitySnapshot


def _recommendation(
    priority: str,
    area: str,
    evidence: str,
    action: str,
    validation: str,
) -> dict[str, str]:
    return {
        "priority": priority,
        "area": area,
        "evidence": evidence,
        "action": action,
        "validation": validation,
    }


def build_optimization_report(
    evaluation: dict[str, Any],
    production: QualitySnapshot,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Turn evaluation and production evidence into reviewable next actions.

    This function deliberately produces recommendations rather than modifying prompts,
    routes, or policies. A human reviews a proposed change and the release evaluation
    must pass again before promotion.
    """

    recommendations: list[dict[str, str]] = []
    metrics = evaluation["metrics"]
    gate = evaluation["gate"]
    thresholds = policy["production_thresholds"]

    if not gate["passed"]:
        recommendations.append(
            _recommendation(
                "P0",
                "release_gate",
                "; ".join(gate["violations"]),
                "修复失败用例对应的路由、政策或答案，并保持版本号可追踪。",
                "重新运行 python -m app.evaluation.runner，所有门禁必须通过。",
            )
        )

    failed_cases = [item for item in evaluation["results"] if not item["passed"]]
    for item in failed_cases[:10]:
        failed_checks = ", ".join(name for name, passed in item["checks"].items() if not passed)
        recommendations.append(
            _recommendation(
                "P1",
                f"case:{item['id']}",
                f"类别 {item['category']}，失败检查：{failed_checks}",
                "先复现该输入，再最小化修改知识、路由或确定性业务规则。",
                f"该用例通过，且完整评测相对基线无回归：{item['id']}。",
            )
        )

    min_samples = int(policy["minimum_production_samples"])
    if production.sample_size < min_samples:
        recommendations.append(
            _recommendation(
                "P2",
                "sample_size",
                f"当前生产样本 {production.sample_size}，决策下限 {min_samples}",
                "继续收集脱敏运行记录、用户反馈和人工抽检标签；仅允许小流量验证。",
                f"同一版本积累至少 {min_samples} 次运行后重新生成报告。",
            )
        )

    if production.error_rate > float(thresholds["max_error_rate"]):
        recommendations.append(
            _recommendation(
                "P0",
                "runtime_reliability",
                f"错误率 {production.error_rate:.2%}",
                "按 error_type 和失败节点聚合运行记录，优先修复最高频故障。",
                f"错误率不高于 {float(thresholds['max_error_rate']):.2%}。",
            )
        )
    if production.p95_latency_ms > float(thresholds["max_p95_latency_ms"]):
        recommendations.append(
            _recommendation(
                "P1",
                "latency",
                f"生产 P95 延迟 {production.p95_latency_ms:.1f} ms",
                "利用节点观测定位慢节点，缩短超时、缓存只读检索或减少无效工具调用。",
                f"P95 不高于 {float(thresholds['max_p95_latency_ms']):.0f} ms。",
            )
        )
    if production.handoff_rate > float(thresholds["max_handoff_rate"]):
        recommendations.append(
            _recommendation(
                "P1",
                "automation",
                f"人工交接率 {production.handoff_rate:.2%}",
                "抽样高频交接意图，补充已获授权的只读工具或明确流程；保留高风险审批。",
                f"交接率不高于 {float(thresholds['max_handoff_rate']):.2%}，且安全门禁通过。",
            )
        )
    if production.positive_feedback_rate is not None and production.positive_feedback_rate < float(
        thresholds["min_positive_feedback_rate"]
    ):
        reasons = ", ".join(
            f"{name}={count}" for name, count in production.negative_feedback_reasons.items()
        )
        recommendations.append(
            _recommendation(
                "P1",
                "answer_quality",
                f"正向反馈率 {production.positive_feedback_rate:.2%}；原因 {reasons or '未标注'}",
                "把负反馈运行加入黄金数据集，针对最高频原因修订知识或回答策略。",
                f"正向反馈率不低于 {float(thresholds['min_positive_feedback_rate']):.2%}。",
            )
        )
    if production.reviewed_run_rate < float(thresholds["min_reviewed_run_rate"]):
        recommendations.append(
            _recommendation(
                "P2",
                "human_review",
                f"人工抽检覆盖率 {production.reviewed_run_rate:.2%}",
                "按意图和风险分层抽检运行，标注正确意图与 1 至 5 分质量分。",
                f"抽检覆盖率不低于 {float(thresholds['min_reviewed_run_rate']):.2%}。",
            )
        )
    if (
        production.labeled_routing_accuracy is not None
        and production.labeled_routing_accuracy < float(thresholds["min_labeled_routing_accuracy"])
    ):
        recommendations.append(
            _recommendation(
                "P1",
                "routing",
                f"已标注路由准确率 {production.labeled_routing_accuracy:.2%}",
                "把误路由样本加入回归集，收紧规则优先级并提升路由版本。",
                f"标注准确率不低于 {float(thresholds['min_labeled_routing_accuracy']):.2%}。",
            )
        )

    production_violations = [item for item in recommendations if item["priority"] in {"P0", "P1"}]
    if not gate["passed"]:
        decision = "BLOCK"
        reason = "离线发布门禁失败"
    elif production.sample_size < min_samples:
        decision = "CANARY"
        reason = "离线门禁通过，但生产证据不足"
    elif production_violations:
        decision = "HOLD"
        reason = "生产质量指标仍有超限项"
    else:
        decision = "PROMOTE"
        reason = "离线门禁和生产质量阈值均满足"

    return {
        "release_decision": decision,
        "decision_reason": reason,
        "human_approval_required": True,
        "evaluation_gate": gate,
        "evaluation_metrics": metrics,
        "production_snapshot": production.model_dump(mode="json"),
        "recommendations": recommendations,
        "workflow": [
            "选择证据充分的单一改动",
            "提升 prompt/router/policy 对应版本号",
            "运行黄金数据集与发布门禁",
            "人工审阅报告后执行小流量验证",
            "用生产反馈与抽检结果决定推广或回滚",
        ],
    }
