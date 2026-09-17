# 连接器开发与安全指南

## 已实现适配器

- `mock`：本地演示、CI 与沙箱模拟；
- `generic-rest`：通过动作到 HTTP method/path 映射接任意电商中台；
- `shopify`：Shopify GraphQL Admin API 2026-07 订单查询与 returnCreate 参考实现；
- `chatwoot`：创建人工交接会话和发送会话消息的参考实现。

## 配置原则

连接器定义只写 `credential_ref: env://SHOP_TOKEN`。生产编排器从 Docker/Kubernetes secret 或组织密钥库把秘密注入环境变量，数据库不保存秘密值。外部连接器只能使用 HTTPS，不能带 URL 用户名或密码，不能指向私网、loopback 或 link-local 地址，主机还必须进入 Live Domain Pack allowlist。

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

## 通用渠道入口

连接器包含 `inbound_chat` capability 后，可向 `POST /api/v1/channels/{connector_id}/messages` 发送：

```json
{
  "external_customer_id": "merchant-customer-42",
  "external_conversation_id": "channel-thread-99",
  "message": "我的订单到哪里了？",
  "locale": "zh-CN"
}
```

请求使用相同的 webhook 签名头。外部顾客必须先通过 `/api/v1/admin/customer-mappings` 按连接器映射到当前租户；同一顾客可在不同连接器使用不同平台 ID。同一连接器的外部会话固定绑定一个内部会话。同一个 `X-Event-ID` 可安全重试并返回第一次处理结果。

需要异步回传回复时，适配器必须实现 `send_message`，连接器同时声明 `send_message` 和 `outbound_chat`。系统会先保存 `OutboundMessage`，再由 Worker 使用租户级幂等键投递。瞬态错误指数退避；不可重试错误或达到最大次数后进入 `dead_letter`。运营人员可在 `/api/v1/ops/outbound-messages` 查看并在修复配置后调用 `/{id}/retry`。

创建人工工单会把会话设为 `human`。此时新渠道消息只保存给客服，不会进入 Agent。客服通过 `/api/v1/tickets/{id}/reply` 回传消息，处理完成后通过 `/api/v1/conversations/{id}/automation` 显式恢复 `auto`。

Shopify access token 通过 `X-Shopify-Access-Token` 发送，并应按最小 access scope 获取。公共应用应使用可过期的 offline token；细节见 [Shopify access token 官方文档](https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens)。
