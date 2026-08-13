# S6 可观测性、缓存与异步 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 Trace 全部标识与度量并与 OpenTelemetry 双写、建立七项 SLO 的可测指标、落地三层缓存、建立 Job/Run 状态机承载长任务，并承接 S4 的 drift scan 调度与 S5 的 Eval Run 落库。

**Architecture:** 当前项目零缓存（仅 `functools.lru_cache` 装饰配置读取）、零异步任务、零 OpenTelemetry；`TraceStageRow` 只有七个字段。本轮的关键判断是**缓存键构造是安全边界而非性能优化**——结果缓存若不含 `policy_hash`，A 用户的结果会被返给 B 用户。因此实现顺序是先 Trace 与指标（让缓存命中可解释），再缓存，再异步：反过来做会得到一个无法归因 P95 的系统。

**Tech Stack:** Python 3.13、SQLAlchemy 2.0、opentelemetry-sdk、Redis 或进程内缓存、PostgreSQL 15+、pytest

## Global Constraints

约束来自 `docs/superpowers/specs/2026-08-13-s6-observability-cache-async-design.md`：

- **失败阶段必须落库，这个保证不能被破坏**。现有 `trace.py::stage_timer` 在异常路径先 `record` 再 `raise`。升级 Trace 模型与接入 OTel 时必须保留该语义——**失败恰恰是 Trace 存在的理由**。
- **缓存键缺任一必要成分 → 拒绝缓存**，不降级为"不带该成分的键"。
- **绝不按自然语言或 SQL 字符串缓存**，只按 `canonical_plan_hash`。
- **数据新鲜度进缓存键，不靠 TTL 赌**。TTL 是兜底而非主手段。
- **OTel 不替代业务 Trace**。两者并行，共享 `trace_id`。sensitive/admin 级内容**不进 OTel**（观测平台的访问控制弱于业务库）。
- **同步路径不动**。异步只承接明确的长任务；"全部改异步"会把 P95 延迟问题变成排队问题。
- **部分进度必须可见**。取消与超时时保留已完成部分。
- **不静默截断结果**——那会产出错误结论。
- 观测不能拖垮业务：OTel 可开关，失败不影响主流程。
- 代码注释与标识符用英文；文档与提交信息用中文。

## 前置

依赖 S1（Trace 四级可见性列拆分、事务边界、`error_code` 分类）、S2（DI Clock、prompt 版本号）、S3（CanonicalQueryPlan 与 plan hash、递归 EXPLAIN 护栏、SQLSTATE 分类）、S4（semantic revision、drift scan 检查逻辑）、S5（Eval Run 归档结构）。

Task 8 的 Eval Run 迁库需要迁移基线，**排在 S7 的 Alembic 基线之后**，或用本轮自建迁移脚本。

HTTP API 与 SSE 进度推送在 S7，本轮只提供 Job 状态查询的服务层能力。

---

### Task 1: Trace 模型补齐

**Files:**
- Modify: `backend/app/observability/orm.py`
- Create: `backend/migrations/012_trace_model_upgrade.sql`
- Modify: `backend/app/observability/trace.py`
- Create: `backend/tests/observability/test_trace_model.py`

**Interfaces:**
- Changed: `TurnRow` / `TraceStageRow` 扩展，新增编译执行专项表

`TraceStageRow`（`app/observability/orm.py:62-83`）现有字段只有 `turn_id`/`stage`/`sequence`/`input_payload`/`output_payload`/`model`/`prompt_tokens`/`completion_tokens`/`elapsed_ms`/`error`/`created_at`。

- [ ] **Step 1: 写失败的失败落库保护测试**

先写这条：**失败 stage 在异常路径仍落库**。这是保护既有保证不被本轮升级破坏的回归测试，必须在改模型之前立起来。

同时断言：**Trace 写入不因主流程回滚而丢失**（Trace 用独立 session 提交）。

- [ ] **Step 2: 按归属分组扩展字段**

| 归属 | 字段 |
|---|---|
| Turn 级（`TurnRow`） | `request_id`、`trace_id`、`run_id`、`principal_hash`、`tenant_id`、`domain`、`semantic_revision`、`policy_revision`、`prompt_version`、`model_snapshot`、`status`、`error_code`、`cancellation`、`idempotency_outcome` |
| Stage 级（`TraceStageRow`） | `payload_schema_version`、`cache_tokens`、`reasoning_tokens`、`retry_count`、`error_code`、`cache_outcome` |
| 编译/执行专项（**独立表**） | `plan_hash`、`secured_sql_hash`、`trusted_asset_id`、`explain_total_cost`、`explain_max_plan_rows`、`scan_relations`、`warehouse_queue_ms`、`warehouse_exec_ms`、`result_rows`、`truncated`、`validator_issues` |

用 `principal_hash` 而非 `user_id`：Trace 的运维视角不需要可反查的用户身份，哈希足以做聚合与关联，同时降低泄漏面。产品回看侧的归属判定仍用 S1 的 `user_id` 对象级鉴权。

编译执行专项字段**单表存放**，避免 stage 行过宽。

- [ ] **Step 3: 新增字段纳入 S1 四级可见性**

沿用 S1 的 public / user / sensitive / admin 列拆分，逐字段明确级别归属。

---

### Task 2: OpenTelemetry 双写

**Files:**
- Create: `backend/app/observability/otel.py`
- Modify: `backend/app/observability/trace.py`
- Create: `backend/tests/observability/test_otel.py`
- Modify: `backend/requirements.txt`

- [ ] **Step 1: 写失败的 OTel 泄漏测试**

核心断言：**sensitive / admin 级内容不出现在 OTel span 属性中**。span 属性只取 public/user 级字段与 `error_code`。

- [ ] **Step 2: 实现双写**

span 层级：turn 为 root span，stage 为子 span，仓库执行为孙 span。`trace_id` 由入口生成并同时写入业务 Trace 与 OTel，两侧可互跳。

- [ ] **Step 3: OTel 失败不影响主流程**

可开关配置，默认开启但失败降级——观测不能拖垮业务。

---

### Task 3: SLO 指标与违约判定

**Files:**
- Create: `backend/app/observability/metrics.py`
- Create: `backend/app/observability/slo.py`
- Create: `backend/tests/observability/test_slo.py`

- [ ] **Step 1: 写失败的违约判定测试**

两条把 SLO 从口号变成可验证约束的断言：
- **answered turn 缺任一必需 stage**（resolve/plan/security/execute/answer）→ Trace 完整率指标计为违约
- **security stage 缺 `policy_hash`** → 计为「权限漏施加」违约事件并告警

- [ ] **Step 2: 实现七项 SLO 指标**

| SLO | 目标 |
|---|---|
| API 可用性 | 99.9% |
| 基础问数 P95 / P99 | < 5s / < 10s（不含明确异步任务） |
| 权限策略漏施加 | 0 |
| 元数据写入持久化成功率 | 99.99% |
| Trace 完整率 | > 99.9% |
| 安全 / 关键 Golden 回归 | 0 |
| 语义发布回滚时间 | < 15 分钟 |

「元数据写入持久化成功率」直接对应 S1 修复的 P0-05（`get_meta_session` 无 commit）——本轮为其加上可观测的确认。

- [ ] **Step 3: 实现指标集**

latency（分阶段与端到端）、token（prompt/completion/cache/reasoning）、cost、refusal rate、clarify rate、eval regression（来自 S5）、warehouse scan rows 与排队时间、cache hit rate、Trace 完整率、`error_code` 分布。

- [ ] **Step 4: `error_code` 由 SQLSTATE 映射**

断言不依赖字符串匹配（复用 S3 的 SQLSTATE 分类）。

---

### Task 4: 三层缓存

**Files:**
- Create: `backend/app/cache/keys.py`
- Create: `backend/app/cache/layers.py`
- Create: `backend/tests/cache/test_cache_keys.py`
- Create: `backend/tests/cache/test_cache_isolation.py`

**Interfaces:**
- Produces: 三层缓存，键与失效条件各不相同

| 层 | 键 | TTL | 失效条件 |
|---|---|---|---|
| 语义定义 | `semantic_revision_id` | 长 | revision 不可变（S4），故永不脏 |
| 权限可见视图 | `(principal_policy_hash, semantic_revision_id)` | 短 | 策略或 revision 变更 |
| 结果 | `(canonical_plan_hash, semantic_revision_id, policy_hash, source_watermark, dialect)` | 短 | watermark 推进或任一成分变更 |

- [ ] **Step 1: 写失败的跨用户泄漏回归测试**

`test_cache_isolation.py`——本任务最重要的测试：**相同 plan、不同 `policy_hash` 的两个 principal 不共享缓存结果**。

- [ ] **Step 2: 写失败的缓存键约束测试**

| 约束 | 断言 |
|---|---|
| 键缺任一成分 | **不缓存**，而非用残缺键缓存 |
| source watermark 推进 | 缓存自然失效 |
| `truncated=true` | **不进缓存**（分页语义下缓存部分结果会误导） |
| 含脱敏列 / sensitive 级结果 | 可配置为完全禁用共享缓存 |
| 缓存命中 | **必须记入 Trace 的 `cache_outcome`**，否则 P95 数据会失真且无法解释 |

- [ ] **Step 3: 实现三层缓存**

`canonical_plan_hash` 来自 S3。

- [ ] **Step 4: 提供强制绕过缓存的开关**

缓存引入后 bug 更难复现，排查需要这个开关。

---

### Task 5: Prompt 缓存

**Files:**
- Modify: `backend/app/llm/provider.py`
- Create: `backend/tests/llm/test_prompt_cache.py`

- [ ] **Step 1: 写失败的前缀泄漏测试**

断言：**Prompt 共享前缀中不含用户或权限上下文**——那是跨用户泄漏路径。前缀只含指令与语义 schema 描述。

- [ ] **Step 2: 按 provider 官方能力实现固定前缀缓存**

记录 cache hit / write token（进 Task 1 的 `cache_tokens` 字段）。

---

### Task 6: Job / Run 状态机

**Files:**
- Create: `backend/app/jobs/state_machine.py`
- Create: `backend/app/jobs/orm.py`
- Create: `backend/migrations/013_jobs.sql`
- Create: `backend/tests/jobs/test_state_machine.py`
- Create: `backend/tests/jobs/test_job_authz.py`

**Interfaces:**
- Produces: `queued → running → (succeeded | failed | cancelled | timed_out | budget_exceeded)`
- Produces: Job 记录 — `job_id`/`kind`/`principal`(user_id)/`state`/`created_at`/`started_at`/`finished_at`/`checkpoints[]`/`partial_result_ref`/`budget`(token/时长/扫描行数)/`consumed`/`error_code`/`trace_id`/`cancel_requested_at`

- [ ] **Step 1: 写失败的状态机与取消测试**

- **Job 取消在 checkpoint 边界生效并保留部分结果**——长任务失败时"什么都没有"和"完成了 7 步中的 5 步"对用户价值完全不同
- **超时 / 预算超限产出各自 `error_code` 而非同一个**（两者走同一退出路径，区别只在终态与 `error_code`）

- [ ] **Step 2: 实现状态机与 checkpoint**

取消是**协作式**的：置 `cancel_requested_at`，执行侧在 checkpoint 边界检查并退出，同时取消底层仓库查询（SQLSTATE `57014`）。终态均保留已完成的部分进度与已产出的部分结果。

状态机边界设计为**可被外部编排器替换**——自建长期可能不够用，但 P2-06 主张先自建再选框架。

- [ ] **Step 3: 写失败的归属与策略固化测试**

- **Job 读取与取消对非本人返回 404**（复用 S1 对象级鉴权）
- **Job 执行期间策略变更不改变已固化的 `policy_hash`**——避免长任务期间策略变更导致判定不一致。要么用提交时策略并记录，要么拒绝，**不静默混用**

- [ ] **Step 4: 实现服务层能力**

`trace_id` 与同步链路共享同一套 Trace，异步任务不另建观测体系。

---

### Task 7: 成本护栏与预聚合识别

**Files:**
- Create: `backend/app/guardrails/budget.py`
- Create: `backend/app/cache/preaggregation.py`
- Create: `backend/tests/guardrails/test_budget.py`

- [ ] **Step 1: 实现四维度护栏**

时间范围、估计扫描行数、EXPLAIN Total Cost、并发与租户预算。

与 S3 的关系：S3 是**单查询**准入（递归 EXPLAIN 分析、最大 Plan Rows），本节增加**跨查询**的并发与预算维度。两者串联，任一拒绝即拒绝。

- [ ] **Step 2: 预算超限的处置优先级**

先建议收窄时间范围或加维度过滤（可执行的补救），无法收窄再拒绝。**不静默截断结果。**

阈值可配置；先观测期只告警不拒绝，取得分布后再收紧，避免误杀正常大查询。

- [ ] **Step 3: 预聚合候选识别**

从 Eval Run 与真实 Trace 统计中识别高频 `(metric, grain, dimension)` 组合，产出候选清单与预估收益。**物理建设需业务侧决策，不自动执行。**

与 S4 的 fanout 预聚合区分：S4 是**正确性**手段（防重复计数），本节是**性能**手段，实现可复用但触发条件无关。

---

### Task 8: 调度与留存

**Files:**
- Create: `backend/app/scheduler/jobs.py`
- Create: `backend/app/observability/retention.py`
- Create: `backend/tests/observability/test_retention.py`

| 任务 | 来源 | 语义 |
|---|---|---|
| drift scan | S4 委派（S4 定义检查逻辑） | 定期校验语义定义与物理契约；**只告警不阻断** |
| Eval Run 落库 | S5 委派 | `artifacts/eval/<run_id>.json` → `eval_run` / `eval_case_result` 表 |
| Trace 留存清理 | 本轮 | 按级别设不同保留期 |

- [ ] **Step 1: 实现 drift scan 调度**

只告警不阻断——上游一次 DDL 不应打挂整个问数服务。

- [ ] **Step 2: 写失败的留存清理测试**

- **Trace 清理按 turn 粒度，不产生半截 Trace**
- **清理前检查 Trusted Asset 与 Golden case 引用；被引用的 turn 不清理**

- [ ] **Step 3: 实现分级留存清理**

public/user 长，**sensitive/admin 短**（含 SQL 与 LLM 原文，泄漏面最大）。

- [ ] **Step 4: Eval Run 迁库**

归档 JSON 结构已按可映射为表设计（S5 §7.1）。本步排在 S7 迁移基线之后，或用本轮自建迁移脚本。

- [ ] **Step 5: Eval / 批处理使用独立 warehouse 或 resource group**

不影响在线问数。

---

## 验收

1. Trace 模型补齐全部字段，按 turn / stage / 编译执行专项分组落位。
2. 新增字段纳入 S1 的四级可见性列拆分，级别归属明确。
3. `error_code` 落地，SLO 与告警不再依赖字符串匹配。
4. OTel 双写生效，与业务 Trace 共享 `trace_id`，sensitive/admin 内容不外泄。
5. 七项 SLO 均有对应可测指标；「权限漏施加」与「Trace 完整率」有明确违约判定。
6. 三层缓存落地，结果缓存键含 `policy_hash` 与 source watermark。
7. 跨用户缓存泄漏有回归测试守住。
8. 预聚合候选识别可产出清单，不自动执行物理建设。
9. Job/Run 状态机落地，支持 checkpoint、取消、超时、预算、部分进度。
10. Job 归属走对象级鉴权，`policy_hash` 提交时固化。
11. drift scan 定时执行且只告警。
12. Eval Run 从文件归档迁入元数据库表。
13. Trace 按级别分期清理，按 turn 粒度，被引用的 turn 不清理。
14. 成本护栏四维度生效，预算超限优先建议收窄。
15. **失败 stage 落库的既有保证未被破坏。**
16. 后端测试基线不回退，新增测试全绿。
