# Domain Pack 配置与发布

## 内置模板

仓库提供 `general-commerce`、`apparel`、`electronics`、`beauty`、`food`、`home` 和 `cross-border` 七套模板。模板是起点，不是商家最终政策。

一个 Domain Pack 包含：

- 品牌名、语气、语言和回复规则；
- 意图关键词、目标路由和置信阈值；
- 退货、取消、改址、折扣与人工审批政策；
- 工具开关、风险等级和角色；
- 知识检索阈值、新鲜度和无依据拒答；
- 连接器主机 allowlist、提示注入与最大 Agent 步数；
- 人工交接触发词、紧急判定词、普通/紧急 SLA 和发布质量门禁。

## 发布顺序

1. 在 `/admin` 选择最接近的模板并创建 Draft。
2. 修改 Draft JSON，填入真实商家政策和允许的连接器主机。
3. 为知识源创建 Draft；提示注入扫描通过后由管理员发布。
4. 调用 `POST /api/v1/admin/simulations/chat` 回放多轮场景。模拟完全隔离。
5. 人工复核政策边界和写动作权限后发布 Domain Pack。
6. 用通过门禁的评测报告创建 release，先设置小比例 canary。
7. 观察真实解决率、动作成功率、错误率和 P95，再提升流量。
8. 用 `/ops/knowledge-health` 检查知识新鲜度，用 `/ops/handoffs` 检查超时交接，
   用 `/ops/audit-events` 抽查发布和动作审计。

同一个 slug 每次修改都会形成新版本。发布新版本会把旧 Live 标记为 superseded，审计记录仍保留。禁止把生产策略交给在线自学习直接修改。
