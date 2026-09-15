# Changelog

## 3.0.0 - 2026-09-15

- 加入 tenant/customer/principal 隔离和 JWT RBAC；生产配置默认失败关闭。
- 加入七套 Domain Pack、Draft/Live、隔离模拟和 release/canary 数据模型。
- 加入 Mock、Generic REST、Shopify 和 Chatwoot 连接器参考实现。
- 加入带客户确认、人工审批、状态重查、幂等与 outbox 的动作状态机。
- 加入租户知识版本、注入扫描、真实解决结果、动作指标和脱敏评测候选闭环。
- 加入 Alembic、Redis 协调、Worker、TLS edge、Prometheus/OTel/Grafana 和生产运行手册。
- 离线黄金集从 13 项扩展到 40 项，并新增 V3 安全/动作/隔离/模拟集成测试。
