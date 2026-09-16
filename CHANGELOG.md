# Changelog

## 3.0.1 - 2026-09-16

- 加入签名全渠道消息入口、外部顾客/会话映射和渠道事件幂等恢复。
- 加入可配置的挫败/敏感主题升级、工单负责人、状态机、首响时间和 SLA 观测。
- 加入租户级变更审计、生产租户成员二次授权、生产 OpenAPI/Mock 禁用。
- 生产检索不再加载演示知识；外部知识超过 Domain Pack 新鲜度后停止参与回答。
- 统一连接器生命周期并关闭 HTTP 客户端；Chatwoot 使用外部顾客标识。
- 离线黄金集扩展到 42 项。

## 3.0.0 - 2026-09-15

- 加入 tenant/customer/principal 隔离和 JWT RBAC；生产配置默认失败关闭。
- 加入七套 Domain Pack、Draft/Live、隔离模拟和 release/canary 数据模型。
- 加入 Mock、Generic REST、Shopify 和 Chatwoot 连接器参考实现。
- 加入带客户确认、人工审批、状态重查、幂等与 outbox 的动作状态机。
- 加入租户知识版本、注入扫描、真实解决结果、动作指标和脱敏评测候选闭环。
- 加入 Alembic、Redis 协调、Worker、TLS edge、Prometheus/OTel/Grafana 和生产运行手册。
- 离线黄金集从 13 项扩展到 40 项，并新增 V3 安全/动作/隔离/模拟集成测试。
