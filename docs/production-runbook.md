# 生产运行手册

## 上线前必需输入

- 商家域名、品牌语气、支持语言和已经法务/运营确认的政策；
- OIDC/JWT issuer、audience，以及 HTTPS JWKS 地址；小型自管部署也可使用至少 32 字符的 HS256 secret；
- PostgreSQL、Redis、模型、商店、帮助台和 webhook 密钥；
- 每个外部连接器的 HTTPS 主机 allowlist；
- 经过批准的不可变容器 digest、备份位置、告警接收人和 SLO。

仓库不能替商家提供这些外部凭据，也不会生成虚假生产值。

## 部署

1. 从 `.env.production.example` 创建 `.env.production`，把所有空值和 `REPLACE` 填完。
2. 用 `pip-audit -r requirements.lock`、Bandit、测试和 40 项评测门禁验证 release。
3. 构建镜像并生成 SBOM；把镜像推到受控 registry 后，将 `SHOPSAGE_IMAGE` 固定为 digest。
4. 先执行 `alembic upgrade head`。应用在 schema revision 不匹配时拒绝启动。
5. 用 `python scripts/provision_tenant.py --tenant-id ... --slug ... --name ... --created-by ...` 创建租户 Draft；只有策略已审核时才加 `--publish`。
6. 执行 `docker compose -f compose.production.yaml up -d`。
7. 检查 `/health/live`、`/health/ready`、Prometheus target、Grafana dashboard 和 OTel traces。
8. 创建连接器 Draft，健康检查通过后激活；再发布 Domain Pack 和 canary release。

## SLO 与告警

建议初始 SLO：API 可用性 99.9%，5xx 小于 2%，P95 小于 2 秒，写动作成功率至少 98%，跨租户失败必须为 0。`ops/alerts.yaml` 提供 5xx、P95 和动作失败告警。真实解决率、FCR 和知识有据率通过 `/api/v1/ops/quality` 查看。

## 备份与恢复

- PostgreSQL：每天全量备份、持续 WAL 归档，至少每季度做一次独立环境恢复演练；
- Redis：允许重建，不把 Redis 当作业务事实来源；
- 配置：Domain Pack、连接器 definition、release 和知识版本随数据库备份；密钥由密钥库独立备份和轮换；
- 恢复后先验证 schema revision，再跑跨租户、动作幂等、知识引用和 40 项离线回归。

## 故障处理

- 模型故障：固定工作流继续服务，模糊问题降级或转人工；
- 连接器瞬态故障：动作进入 retryable，保留同一幂等键并按指数退避；达到 `ACTION_MAX_ATTEMPTS` 后失败并告警；
- 动作状态不一致：以商店系统为事实源，核对 `ActionExecution`、outbox 和外部 id，不直接重放新键；
- 质量回退：停止提升 canary，把新 release 标为 superseded，重新激活上一个已通过版本；
- 安全事件：禁用连接器、轮换密钥、保存审计证据，按 `SECURITY.md` 流程处理。

## 数据保留

消息、运行轨迹、反馈和 webhook payload 只保留业务与法规需要的最短周期。当前代码保存 webhook hash，不保存 raw payload。生产环境应通过定时合规任务执行到期清理；任何清理都先验证 tenant、时间范围和备份。
