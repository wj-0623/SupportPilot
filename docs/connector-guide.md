# 连接器开发与安全指南

## 已实现适配器

- `mock`：本地演示、CI 与沙箱模拟；
- `generic-rest`：通过动作到 HTTP method/path 映射接任意电商中台；
- `shopify`：Shopify GraphQL Admin API 2026-07 订单查询与 returnCreate 参考实现；
- `chatwoot`：创建人工交接会话的参考实现。

## 配置原则

连接器定义只写 `credential_ref: env://SHOP_TOKEN`。生产环境应把 `EnvironmentCredentialResolver` 替换成组织的 Vault/KMS/Secret Manager resolver，并保持接口不变。外部连接器只能使用 HTTPS，不能带 URL 用户名或密码，不能指向私网、loopback 或 link-local 地址，主机还必须进入 Live Domain Pack allowlist。

Generic REST 示例：

```json
{
  "name": "merchant-commerce",
  "provider": "generic-rest",
  "base_url": "https://commerce.example.com/",
  "credential_ref": "env://COMMERCE_TOKEN",
  "capabilities": ["get_order", "cancel_order"],
  "config": {
    "webhook_secret_ref": "env://COMMERCE_WEBHOOK_SECRET",
    "actions": {
      "get_order": {"method": "POST", "path": "/agent/order"},
      "cancel_order": {"method": "POST", "path": "/agent/order/cancel"}
    }
  }
}
```

## 动作协议

所有写动作都要带稳定的 `Idempotency-Key`。服务首先生成不可变摘要并返回 `awaiting_customer_confirmation` 或 `awaiting_human_approval`。确认后 Worker 会重新查询订单状态，再调用连接器。连接器必须接受相同幂等键并对重试返回同一结果。

Webhook 使用 `X-Event-ID` 防重，使用 `X-Webhook-Timestamp` 防重放，签名是 `HMAC-SHA256(secret, timestamp + "." + raw_body)`，放在 `X-Webhook-Signature`。业务 API token 与 webhook secret 必须使用不同引用。

Shopify access token 通过 `X-Shopify-Access-Token` 发送，并应按最小 access scope 获取。公共应用应使用可过期的 offline token；细节见 [Shopify access token 官方文档](https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens)。
