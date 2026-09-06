# Advanced Data Agent 实现规格

本目录是 `CODEX_DATA_AGENT_FINAL_V3.md` 的实现账本，不替代原规范。正式分析遵循一条不可旁路的链：

`TaskContract → AgentRun/Plan → ModelAdapter → AgentLoop → ToolExecutor/Action → DatasetRef → Validation → Claim/ResultManifest → Publication → Render/Deliver`

## 强制不变量

1. 对话、团队成员使用唯一 `backend/agent/loop.py`；工作流可保持确定性 DAG，但每个节点必须经过相同 ToolExecutor、预算、Action 和发布门禁。
2. Chat Completions 与 Responses 是两个真实协议适配器。测试脚本模型仅可在测试代码中显式注入，生产无模型时失败并报告 `model_not_configured`。
3. 四段任务契约经用户按版本确认后才可排队。目标、来源、口径或验收规则变化必须产生新版本。
4. `AgentRun`、Decision、Action、Attempt、预算预留、事件、外部 Job、DatasetRef、Validation、Manifest、Claim 和 Publication 都持久化；租约 epoch 阻止陈旧 Runner 写回。
5. 远端来源不进入隐式 DataFrame 路径。Trino 小结果只有在完整行数等于保留预览时才可登记；大结果必须 CTAS 到受管 scratch 表。Spark 只接受六个受信任 JobSpec 方法。
6. 系统截断、来源分页未完成、作业未知、验证 UNKNOWN/FAIL、授权撤销均阻止正式发布。
7. DOCX、四图 PNG、EML 和 SMTP 从同一已发布 ResultManifest 渲染；发送状态未知时禁止盲目重发。

## 主要入口

- `POST /api/analyses`：建立四段契约草稿。
- `POST /api/analyses/{id}/contract/confirm`：版本化确认并提交 typed JobSpec。
- `/api/analyses/{id}/events`：JSON 增量事件或 SSE 重连。
- `/api/analyses/{id}/pause|resume|cancel|clarifications|branch`：真实控制和分叉。
- `/api/analyses/{id}/result|evidence|validations|replay`：发布结果、证据、校验、复现。
- `/api/analyses/{id}/render` 与 `/deliveries`：统一渲染及 EML/SMTP 交付。
- `/api/warehouse/*`：Trino/Livy 引擎、目录、查询、分页、日志、取消。
- `/api/skills/*`：candidate、evaluate、publish、deprecate、rollback 生命周期。

## 状态语义

执行状态为 queued/running/waiting_input/waiting_approval/waiting_job/paused/cancelling/finished/failed/cancelled；结果状态独立为 complete/partial/no_data/failed/refused/cancelled，质量状态独立为 passed/failed/unknown/not_evaluated。`finished` 不等于发布成功。

## 数据与部署边界

SQLite 是控制面单写者；远端数据和计算在 Trino/Iceberg/Spark-Livy。参考 Compose 仅证明适配路径，不代表客户目标平台或 PB/EB 性能。目标平台、真实模型、SMTP、迁移恢复和规模验收均由 `scripts/verify_advanced_agent.py` 的独立 profile 收集证据。
