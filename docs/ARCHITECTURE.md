# 经纬分析工作台架构

## 架构目标

系统围绕“数据接入 → 证据生成 → 分析推理 → 人工复核 → 交付与复用”组织。前端使用 Vue 3，后端使用 Python/Flask；本地 SQLite 保存业务元数据，数据计算由 Pandas、DuckDB、SciPy 与 scikit-learn 执行。

## 领域边界

| 领域 | 主要职责 | 关键实现 |
|---|---|---|
| 工作空间 | 空间、会话、快照、记忆、审计、回收站 | `backend/api/workspace.py`、通用记录仓库 |
| 数据目录 | 文件、SQL、HTTP 数据接入；预览、结构、查询、清洗 | `backend/services/datasets.py` |
| 分析内核 | 质量画像、推断、分群、建模、预测、异常检测 | `backend/services/analytics.py` |
| 对话运行时 | 模型规划、只读查询、知识引用、SSE 事件、结果证据 | `backend/services/agent_runtime.py` |
| 知识与技能 | 文档解析、分块检索、可复用分析指令 | `backend/services/knowledge.py` |
| 自动化 | 后台任务、DAG 校验/发布/执行、审批、Hook、协作组 | `backend/services/jobs.py`、`backend/services/workflows.py` |
| 交付 | 图表规范、看板、CSV/Excel/Word/PPT/HTML 成果 | `backend/services/charts.py`、`backend/services/exports.py` |
| 集成 | 多模型、MCP、Webhook/协作平台、计算节点 | `backend/api/integration.py` |

## 关键约束

- 用户 SQL 只允许单条只读语句，并阻止 DDL、DML、文件加载和扩展安装。
- 文件清洗始终产生派生数据集，不覆盖原始文件。
- 模型、数据库、MCP、Webhook 与 SSH 凭据使用 Fernet 加密后落库，接口只返回脱敏状态。
- 工作流草稿必须通过 DAG、步骤类型与必填参数校验后才能发布。
- 审批步骤不会隐式通过；批准或拒绝都有运行状态记录。
- 消息保留 SQL、结果、图表、知识引用与假设，交付成果保留来源元数据。
- 删除默认进入逻辑回收站，永久删除需要显式确认。

## 对话事件协议

`POST /api/sessions/:id/messages` 返回 Server-Sent Events：

1. `accepted`：请求已接收。
2. `stage`：上下文、规划、查询等阶段状态。
3. `context`：数据结构和知识召回摘要。
4. `plan`：即将执行的只读 SQL 与假设。
5. `table`：可复核查询结果。
6. `chart`：前端可直接渲染的图表语义规范。
7. `message`：最终说明及证据元数据。
8. `done` / `error` / `cancelled`：终态。

## 扩展方式

- 新分析方法：在 `analytics.py` 注册方法并实现 `run_analysis` 分支。
- 新数据源：实现登记、结构发现与 `source_frames` 读取器。
- 新工作流步骤：加入 `STEP_TYPES` 并在 `execute_run` 中实现执行器。
- 新图表：加入图表目录，在 Vue `ChartView` 中把语义映射到 ECharts option。
- 新通知平台：在连接器发送函数中添加平台 payload 适配。

