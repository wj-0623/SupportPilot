# Domain Packs

Domain Pack 是可审计、可发布、可回滚的客服领域配置。`general-commerce.json` 是通用基线；其余文件是垂直行业模板。生产租户应从模板创建数据库中的 Draft 修订，完成模拟和审批后再发布为 Live。

每个包定义品牌语气、意图、确定性政策、工具权限与风险、知识检索、安全规则、转人工 SLA 和发布门禁。密钥只保存为连接器的 `credential_ref`，不得写入 Domain Pack。
