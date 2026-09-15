# 执行—观测—优化运行手册

## 1. 执行

客户消息进入确定性输入防护和意图路由。订单、退货、FAQ、商品和人工转接进入固定工作流；只有模糊问题进入最多 4 步、工具白名单为只读查询的 Agent。模型不能退款、取消订单或修改金额。

每次成功或失败运行都会写入 `agent_runs`。记录只保存输入哈希、结构化结果和脱敏观测，不保存新的原始提示副本。

## 2. 观测

### 运维 API

设置 `ADMIN_API_KEY` 后通过 `X-Admin-Key` 调用：

- `GET /api/v1/ops/runs`：运行列表。
- `GET /api/v1/ops/runs/{run_id}`：节点耗时、路由、版本、工具和安全标记。
- `POST /api/v1/ops/runs/{run_id}/review`：人工标注意图与质量分。
- `GET /api/v1/ops/quality`：成功率、自动化率、交接率、P50/P95、反馈与抽检准确率。
- `POST /api/v1/messages/{message_id}/feedback`：顾客对一次回答提交反馈。

### 指标与 trace

- `/metrics` 输出 Prometheus 指标，包括运行状态、节点延迟、工具调用和反馈。
- 设置 `OTEL_EXPORTER_OTLP_ENDPOINT` 后输出 OpenTelemetry trace。只附带节点名称、耗时和状态等属性，不导出消息正文。
- JSON 日志包含请求 ID，便于和网关日志关联。
- `EXPOSE_DEBUG_TRACE=false` 是默认值，客户响应不携带内部轨迹。

## 3. 离线评测

```powershell
.\.venv\Scripts\python.exe -m app.evaluation.runner
```

评测器用内存数据库执行 `evaluations/golden.json`，调用与生产相同的 `SupportService`，检查多轮上下文、跨顾客隔离、政策、安全、引用、人工转接和延迟。结果写入：

- `artifacts/evaluation/latest.json`：机器可读证据。
- `artifacts/evaluation/latest.md`：代码评审报告。

`config/quality_gates.json` 定义硬门槛；CI 中任何失败都会阻止构建通过。

## 4. 优化

```powershell
.\.venv\Scripts\python.exe -m app.optimization.cli
```

优化器合并最新离线评测和本地运行数据，输出优先级、证据、建议动作与验收条件，并给出：

- `BLOCK`：离线发布门禁失败。
- `HOLD`：样本已足够，但生产质量指标超限。
- `CANARY`：离线门禁通过，生产样本不足，只适合小流量验证。
- `PROMOTE`：离线门禁和生产阈值均满足。

结果写入 `artifacts/optimization/latest.json` 和 `latest.md`。优化器不会自动改提示词、路由或业务政策。

## 5. 受控变更

1. 从报告中选择证据充分的单一问题。
2. 修改知识、路由或业务规则，并更新对应版本号。
3. 将生产失败或负反馈案例脱敏后加入黄金数据集。
4. 重新运行测试和发布评测。
5. 人工审核报告后小流量验证，再根据生产快照推广或回滚。
