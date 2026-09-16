# 威胁模型

| 威胁 | 主要控制 | 剩余风险与生产措施 |
| --- | --- | --- |
| 跨租户读取 | JWT tenant/customer claim；生产强制成员表授权；scope mapping；仓储级 tenant 条件；404 隐藏资源存在性 | PostgreSQL 建议再加 Row Level Security 和集成测试 |
| 客户伪造身份 | JWT 模式忽略请求体身份并拒绝不一致 | 应使用 OIDC JWKS 与短 token；HS256 适合单组织部署 |
| 提示注入 | 输入检测；知识 Draft 扫描；Agent 工具白名单；模型不能直接写 | 增加编码/多语言攻击语料和外部 red-team |
| SSRF | HTTPS；禁止 URL 凭据、私网和 loopback；tenant host allowlist；不跟随重定向 | DNS rebinding 需在 egress proxy 再限制目的地址 |
| 重放/重复动作 | 客户确认摘要；幂等键；Redis 锁；上游幂等；执行前状态重查；outbox | 锁过期时依赖数据库与上游幂等作为最终防线 |
| Webhook/渠道消息伪造 | 独立 secret、原始 body HMAC、时间窗、连接器级事件幂等、渠道会话绑定、只存 hash | secret 轮换期需支持双 secret |
| 示例知识越权 | 生产不加载仓库内置知识与商品目录；tenant/source/version 隔离；外部知识过期即退出检索 | 发布前仍需人工核对来源所有权和抓取范围 |
| 密钥泄露 | 数据库只存 `env://` 引用；浏览器只用 sessionStorage；日志不写 token | 生产改用 Vault/KMS，启用 secret scanner 和短期 token |
| 恶意管理员 | RBAC、Draft/Live、人工发布、release gate、完整审计 | 对发布和高风险动作启用双人审批 |
| 依赖/镜像风险 | 两套锁文件、pip-audit、Bandit、SBOM、容器扫描、生产强制 digest | 建立补丁 SLA 与受控 registry 签名验证 |
| 可观测数据泄露 | 运行表只存 input hash、脱敏 trace 和计数；webhook 只存 hash | OTel exporter 仍需租户访问控制和保留策略 |
