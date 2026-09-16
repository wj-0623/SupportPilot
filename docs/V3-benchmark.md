# V3 市场与开源对标结论

本轮对标聚焦可验证的产品能力和仓库自述，不复制参考代码。

| 标杆能力 | V3 对应实现 |
| --- | --- |
| Intercom 的 preview、batch test、simulation 分层与无真实副作用沙箱 | Domain Pack Draft、`/admin/simulations/chat` 隔离模拟、42 项版本化回归、人工发布 |
| Intercom Procedures + Guidance + Workflows 的分工 | 确定性业务工作流、Domain Pack persona/规则、受限 Agent 分层，避免把业务状态机塞进提示词 |
| Intercom/Gorgias 的数据驱动升级、挫败与敏感主题人工接管 | Domain Pack 可配置升级词、紧急优先级、工单 SLA/负责人/状态流转和 `/ops/handoffs` |
| Intercom Data Connector 的 typed input、认证、test connection、Draft/Live 与回滚 | ConnectorDefinition、credential reference、healthcheck、Draft/Live、版本字段和 host allowlist |
| Gorgias 的 rules + AI、动作条件、不可逆操作客户确认与多应用步骤 | workflow + bounded agent；risk tier；确认摘要；人工审批；状态重查；worker/outbox |
| Salesforce Testing Center 的 topic/action/response ground truth 与 multi-turn | routing/content/citation/handoff 门禁、多轮集、动作集成测试、可选 live suite |
| Decagon 的人工 ground truth、在线渐进 rollout 与端到端评测 | 负反馈候选必须人工接受；dataset revision；canary release；检索/策略/动作观测 |
| Sierra 的每客户回归集 | tenant dataset revision 和 Domain Pack/release 绑定 |
| Chatwoot omnichannel inbox | Chatwoot handoff adapter；签名渠道消息入口；外部顾客/会话映射和事件幂等 |
| Langfuse 的 traces → human label → dataset → experiment | 脱敏 AgentRun、人工 review/candidate、dataset revision、simulation 和 release gate |
| Agent Health 的 trajectory evaluation | AgentRun observations、OTel node/connector spans、workflow checkpoint 和版本字段 |

参考资料：

- [Intercom Batch Test](https://www.intercom.com/help/en/articles/10521711-batch-test-fin-ai-agent)
- [Intercom Data Connectors](https://www.intercom.com/help/en/articles/9916497-how-to-set-up-data-connectors)
- [Intercom simulation / batch / preview](https://www.intercom.com/help/en/articles/14077180-simulations-vs-batch-tests-vs-previews)
- [Intercom Procedures vs Tasks vs Workflows](https://www.intercom.com/help/en/articles/14077835-procedures-vs-tasks-vs-workflows)
- [Intercom escalation guidance and rules](https://www.intercom.com/help/en/articles/12396892-manage-fin-ai-agent-s-escalation-guidance-and-rules)
- [Gorgias AI Agent](https://docs.gorgias.com/en-US/ai-agent-explained-497772)
- [Gorgias Actions](https://docs.gorgias.com/en-US/create-an-action-for-ai-agent-567325)
- [Gorgias handover controls](https://docs.gorgias.com/en-US/customize-how-ai-agent-hands-over-to-your-team-6008591)
- [Salesforce Agentforce Testing Center](https://help.salesforce.com/s/articleView?id=005228642&language=en_US&type=1)
- [Decagon evaluation engine](https://decagon.ai/blog/evaluation-engine-ai-agents)
- [Sierra agent lifecycle](https://sierra.ai/blog/agent-development-life-cycle)
- [Chatwoot repository](https://github.com/chatwoot/chatwoot)
- [Langfuse repository](https://github.com/langfuse/langfuse)
- [OpenSearch Agent Health](https://github.com/opensearch-project/agent-health)

仓库 `negativexq/agentic-customer-service-platform` 和 `VIVPM/ecommerce-agent` 的能力来自其 README 自述，应通过独立运行验证后再用于采购或架构决策。
