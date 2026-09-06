# 能力与降级矩阵

| 能力 | 可用条件 | 缺失条件下行为 |
|---|---|---|
| 本地文件/小型数据库查询 | 来源授权且通过行数、字节、超时门禁 | 明确拒绝或标记 partial，不作全量推断 |
| HTTP/BI 数据 | 安全出站、认证、显式 page/cursor/next_url 分页 | 到达页/记录上限标 source_partial，阻止正式发布 |
| Trino | 引擎配置、身份/ACL、资源组硬限制或可用估算 | 成本未知且无原生限制时拒绝提交 |
| Trino 大结果 | 配置 scratch catalog/schema 且 CTAS 成功 | 不以 query id 登记完整 DatasetRef |
| Spark-Livy | Livy、对象存储、已授权 DatasetRef、六种受信方法 | UNKNOWN/BLOCKED，不回退到宿主代码 |
| Python 分析 | 固定 Docker sandbox 镜像、独立鉴权宿主代理和共享受管卷可用，且输入通过有界策略 | fail closed；Web/Agent 不持有 Docker socket，不回退主进程 |
| 正式自主分析 | 有真实模型提供商且工具协议可用 | `model_not_configured`，不生成伪结论 |
| 工作流/调度 | 已发布版本、预授权 actor/source、typed JobSpec | 授权撤销时停止；导出/通知无发布证据时拒绝 |
| 团队/子 Agent | 成员模型、收窄工具和父预算 | 任一成员未发布则团队进入 needs_review |
| Skill | 当前版本评估 PASS 且所有者发布 | candidate/tested/deprecated 不进入正式分析 |
| DOCX/PNG/EML | 已发布 ResultManifest | 未发布只返回 not_published |
| SMTP | 已发布附件、SMTP/TLS 凭据和连接 | failed/unknown 可见；未知状态不自动重复发 |
