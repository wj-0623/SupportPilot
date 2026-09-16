# SupportPilot optimization report

Generated: 2026-09-16T02:00:31.108252+00:00
Release decision: **CANARY**
Reason: 离线门禁通过，但生产证据不足

优化建议不会自动修改提示词、路由或业务政策。每项变更均需人工审阅并重新通过发布评测。

## Production snapshot

| Metric | Value |
| --- | ---: |
| sample_size | 0 |
| error_rate | 0.0 |
| automation_rate | 0.0 |
| handoff_rate | 0.0 |
| p95_latency_ms | 0.0 |
| positive_feedback_rate | None |
| reviewed_run_rate | 0.0 |
| labeled_routing_accuracy | None |

## Prioritized recommendations

### P2 · sample_size

- Evidence: 当前生产样本 0，决策下限 100
- Action: 继续收集脱敏运行记录、用户反馈和人工抽检标签；仅允许小流量验证。
- Validation: 同一版本积累至少 100 次运行后重新生成报告。

### P2 · human_review

- Evidence: 人工抽检覆盖率 0.00%
- Action: 按意图和风险分层抽检运行，标注正确意图与 1 至 5 分质量分。
- Validation: 抽检覆盖率不低于 10.00%。

## Controlled optimization workflow

1. 选择证据充分的单一改动
2. 提升 prompt/router/policy 对应版本号
3. 运行黄金数据集与发布门禁
4. 人工审阅报告后执行小流量验证
5. 用生产反馈与抽检结果决定推广或回滚
