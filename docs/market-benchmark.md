# ShopSage 市场与开源对标

调研日期：2026-09-15。目标是提取可验证的工程模式，不复制参考项目代码，也不把尚未实现的能力写成已完成。

## 成熟产品的共同模式

| 系统 | 公开能力 | ShopSage 对应实现 | 状态 |
| --- | --- | --- | --- |
| Intercom Fin | 将检索、数据、动作拆成可分析阶段；用真实问题组成可复用批量测试，并在上线前诊断来源和失败点 | 节点级观测、版本化黄金数据集、同一服务函数离线回放、发布门禁 | 已实现核心闭环 |
| Gorgias AI Agent | Train → Automate → Track → Optimize；动作具备条件与顺序，不可逆操作要求顾客确认 | 确定性路由、只读 Agent 工具、退货策略门、人工审批工单、优化报告 | 已实现安全子集 |
| Decagon | 对生成、检索和安全持续评测；提供测试、观测和实验能力 | 内容/引用/策略安全评测、运行审计、OTel、Prometheus、版本对比基础 | 已实现基础能力 |
| Sierra | 对生产会话审计；升级时运行按客户维护的回归套件 | 人工抽检标签、反馈聚合、黄金数据集、基线回归检测 | 已实现 |
| Zendesk | 质量分数持续 QA，结合客户上下文改善自动化 | 质量快照、订单归属上下文、多轮订单引用、人工质量评分 | 已实现基础能力 |
| Langfuse / Phoenix | 从生产 trace 建数据集，对同一生产函数做实验；跟踪工具、检索、成本与延迟 | 脱敏运行表、节点耗时、工具/Token 指标、JSON/Markdown 评测报告 | 已实现本地与开放标准出口 |

## 参考来源

- [Intercom：Fin AI Engine](https://www.intercom.com/help/en/articles/9929230-the-fin-ai-engine)
- [Intercom：Batch test Fin AI Agent](https://www.intercom.com/help/en/articles/10521711-batch-test-fin-ai-agent)
- [Intercom：Optimize Fin](https://www.intercom.com/help/en/articles/11390088-optimize-fin-instantly-with-the-help-of-ai)
- [Gorgias：AI Agent](https://www.gorgias.com/ai-agent)
- [Gorgias：Create an Action for AI Agent](https://docs.gorgias.com/en-US/create-an-action-for-ai-agent-567325)
- [Decagon：Evaluation engine for AI agents](https://decagon.ai/blog/evaluation-engine-ai-agents)
- [Sierra：Agent Development Life Cycle](https://sierra.ai/blog/agent-development-life-cycle)
- [Zendesk：Relate 2026 announcement](https://www.zendesk.com/newsroom/articles/relate-2026/)
- [Langfuse：Datasets](https://github.com/langfuse/langfuse-docs/blob/main/content/docs/evaluation/experiments/datasets.mdx)
- [Arize Phoenix：Tracing and evaluation skill](https://github.com/Arize-ai/phoenix/blob/main/docs/phoenix/skill.md)
- [OpenTelemetry：Semantic conventions](https://opentelemetry.io/docs/concepts/semantic-conventions/)
- [Promptfoo](https://github.com/promptfoo/promptfoo)

## 开源客服项目对照

| 项目 | 借鉴方向 | ShopSage 的补强 |
| --- | --- | --- |
| [LikhithV02/Customer-Support-Agent](https://github.com/LikhithV02/Customer-Support-Agent) | 端到端客服界面和退款策略门 | 加入订单归属隔离、幂等、持久化审计、发布评测 |
| [Amankhan1009/customer-support-agent](https://github.com/Amankhan1009/customer-support-agent) | 混合路由、持久化和人工转接 | 加入工具白名单、节点观测、反馈与人工抽检闭环 |
| [Pragatheswar-72/support-agent](https://github.com/Pragatheswar-72/support-agent) | 专用路径、工具约束和路由评估 | 加入多轮黄金集、基线回归、质量门禁和优化建议 |
| [promptfoo/promptfoo](https://github.com/promptfoo/promptfoo) | 自动化测试与红队评测 | 当前采用项目内轻量评测器，方便 Windows 直接运行 |

## 当前边界

ShopSage 已覆盖作品集需要展示的完整“执行—观测—优化”闭环，但还不是 Intercom、Gorgias 或 Zendesk 的商业替代品。以下能力需要接入真实企业系统后才能成立：

- Shopify、支付、仓储和 CRM 的正式连接器，以及动作级 OAuth/权限审批。
- 邮件、WhatsApp、语音和社交平台等全渠道接入。
- 大规模向量检索、知识发布审批、多语言和图片理解。
- Redis 分布式限流、消息队列、数据库迁移流水线和多区域容灾。
- 在线 A/B 实验、自动分层抽样和专职质检队列。

项目选择可直接验证的深度：关键业务动作由代码约束，Agent 只读；每次运行可审计；每次优化有数据依据、版本号、回归集和发布门禁。
