# S3 Canonical Plan 与编译器重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 引入 `CanonicalQueryPlan` / `SecuredExecutionPlan` 双层计划契约，重构流水线为三段式；修复多指标时间口径、比较查询 NULL 维度、freshness 越权、Decimal 漏判、成本护栏失效、截断与重试；VQ 从 Canonical Plan 重编译，从根消除列权限旁路。

**Architecture:** 项目当前缺一个稳定的中间契约：`intent_snapshot` 太靠前（枚举与时间未解析），`sql` 太靠后（已含权限改写），两者都不能做缓存键、trusted asset 存储单元或回放基准。`CanonicalQueryPlan` 填这个位置——纯语义、无 principal、可跨用户复用、可缓存；`SecuredExecutionPlan` 叠加 principal 与策略，绝不共享。关键设计是 `required_field_lineage`：计划**自己声明**它读取哪些物理列，权限编译对这份清单做检查，而不是遍历 AST 猜测。这是 P0-04 的修复基础，也让复合指标穿透变得可验证。

**Tech Stack:** Python 3.13、sqlglot 25.x、PostgreSQL 15+、Decimal、pytest

## Global Constraints

约束来自 `docs/superpowers/specs/2026-08-13-s3-canonical-plan-compiler-design.md`：

- **Canonical Plan 不含任何 principal 信息**；Secured Plan 绝不共享。结果缓存键必须含 `policy_hash`，同一问题的不同用户不得命中彼此的结果。
- 权限编译对计划声明的 `required_field_lineage` 做检查，不遍历 AST 猜测涉及哪些列。
- 同一 plan 在同一 semantic revision 上必须产出**字节相同**的 SQL。
- 多指标 time basis 不一致时**默认拒绝并澄清**，不静默错算。
- Trusted Asset 存 `TimeExpression` 而非已解析区间——存绝对区间等于把过期答案固化。
- 错误分类用 **SQLSTATE**，不匹配错误消息字符串。
- 财务值禁止浮点直接比较。
- VQ 与普通查询共用同一权限编译入口，无例外路径。
- 代码注释与标识符用英文；文档与提交信息用中文。

## 前置

依赖 S1（权限编译入口、只读执行、函数白名单、error taxonomy）与 S2（`TimeResolver`、`ResolvedTimeRange`、IntentV2、`MetricRef`、typed literal、`logical_type`）。

本计划恢复 S1 Task 7 关闭的 VQ 直接执行能力（S1 唯一的功能倒退）。

---

### Task 1: 计划契约

**Files:**
- Create: `backend/app/planning/__init__.py`
- Create: `backend/app/planning/canonical.py`
- Create: `backend/app/planning/secured.py`
- Create: `backend/app/planning/serialize.py`
- Create: `backend/tests/planning/__init__.py`
- Create: `backend/tests/planning/test_canonical.py`
- Create: `backend/tests/planning/test_plan_hash.py`

**Interfaces:**
- Produces:
  - `app.planning.canonical.CanonicalQueryPlan` — `plan_version`/`semantic_revision_id`/`domain`/`dataset`/`measures[]`/`group_by[]`/`typed_filters[]`/`resolved_time_range`/`comparison`/`sort`/`pagination`/`required_field_lineage[]`/`assumptions[]`/`clarification_evidence[]`
  - `app.planning.canonical.Measure` — metric name + version + `time_basis`
  - `app.planning.secured.SecuredExecutionPlan` — Canonical Plan + `principal_id`/`tenant_id`/`policy_revision`/`policy_hash`/`row_predicates[]`/`column_decisions[]`/`dialect`/`warehouse_profile`/`execution_budget`
  - `app.planning.serialize.plan_hash(plan) -> str` — 稳定序列化（字段显式排序、版本化）

- [ ] **Step 1: 写失败的计划契约与哈希测试**

`test_canonical.py`：Canonical Plan 是 frozen 的；**不含任何 principal 字段**（断言字段名集合中无 principal/user/tenant）；`required_field_lineage` 非空。

`test_plan_hash.py`：同一 plan 两次序列化字节相同；字段顺序变化不改变哈希（显式排序）；`plan_version` 或 `semantic_revision_id` 变化则哈希变化；同一 plan 在同一 revision 上编译出的 SQL **字节相同**。

这是可回放性与缓存正确性的前提，必须专门测。

- [ ] **Step 2: 实现两个计划类与序列化**

序列化显式排序并版本化（计划序列化稳定性依赖字段顺序，以哈希一致性测试锁住）。

---

### Task 2: 三段式流水线重构

**Files:**
- Modify: `backend/app/compiler/query.py`
- Modify: `backend/app/security/pipeline.py`
- Modify: `backend/app/pipeline/orchestrator.py`
- Modify: 大部分 `backend/tests/compiler/`、`backend/tests/security/` 测试

**Interfaces:**
- Changed: `compile_intent()` 拆为 `build_plan()`（纯语义、**无 IO**）与 `compile_plan()`（方言 SQL 生成）
- Changed: `secure_compiled()` → `compile_policy()`（产出 `SecuredExecutionPlan`）
- 流水线：`intent → resolve → CanonicalQueryPlan → policy compile → SecuredExecutionPlan → dialect compile → AST guard → execute`

**分两步提交**（重构面广）：先引入 plan 契约并让现有路径产出它，再切换执行路径。

- [ ] **Step 1: 引入契约，现有路径产出 plan 但不改执行**

`build_plan()` 产出 Canonical Plan，现有编译路径并行运行，断言两者语义等价。这一步不改变任何执行行为。

- [ ] **Step 2: 切换执行路径**

执行改为消费 `SecuredExecutionPlan`。`compile_policy()` 对 `required_field_lineage` 做列权限检查（不再遍历 AST）。

- [ ] **Step 3: 迁移编译器测试**

---

### Task 3: 多指标时间口径

**Files:**
- Modify: `backend/app/compiler/query.py`
- Modify: `backend/app/pipeline/answer.py`（或 Citation 所在模块）
- Create: `backend/tests/compiler/test_multi_metric_time_basis.py`

**Interfaces:**
- Changed: `Citation` 从单指标引证改为 `tuple[MetricCitation, ...]`，每指标独立展示时间字段与版本

`_build_select` 现用 `build_time_predicate(dataset, metrics[0].time_field, window)`：问"本月的订单金额和退款金额"时，若订单按 `order_date`、退款按 `refund_date`，两者被同一个 `order_date` 区间过滤，退款数据静默错算（P1-02）。

- [ ] **Step 1: 写失败的口径校验测试**

`test_multi_metric_time_basis.py`：不同 `time_basis` 的两个指标 → **拒绝并触发澄清**，澄清文案说明各指标的时间字段差异；用户显式要求分别统计时 → SQL 含两个子查询并按 `group_by` 维度 FULL OUTER 合并；引证逐指标展示各自时间字段与版本。

- [ ] **Step 2: 实现编译前校验与分别聚合路径**

`measures[]` 内所有指标 `time_basis` 必须一致，默认不一致即拒绝。

- [ ] **Step 3: Citation 结构改造**

现有 `citation.time_field_business_name` 单字段结构不足以表达多指标。保留单指标场景的兼容渲染；`MetricCitation` 变更同步 S7 前端。

---

### Task 4: 比较查询正确性

**Files:**
- Modify: `backend/app/compiler/query.py`
- Modify: `backend/app/execution/validator.py`（结果校验所在模块）
- Create: `backend/tests/compiler/test_comparison_dimensions.py`

`_build_comparison` 的维度投影为 `exp.column(name, table=_CURRENT_CTE)`。FULL OUTER JOIN 下，仅存在于基期的维度值会使维度列为 NULL，用户看到"省份=空，当期=空,基期=100"（P1-04）。

- [ ] **Step 1: 写失败的比较查询测试**

`test_comparison_dimensions.py`：仅存在于基期的维度值正确显示（非 NULL）；状态列正确标记「仅当期 / 仅基期 / 两期都有」；all-null 检测**只针对指标列**，维度列 NULL 不再被判异常。

- [ ] **Step 2: 维度投影改 COALESCE 并加状态列**

`COALESCE(current.dim, baseline.dim)`。

- [ ] **Step 3: 修结果校验的 all-null 检测范围**

---

### Task 5: Freshness 受权限约束

**Files:**
- Modify: `backend/app/semantic/orm.py`（摄取水位字段）
- Modify: freshness 查询所在模块
- Create: `backend/migrations/006_source_watermark.sql`
- Create: `backend/tests/security/test_freshness_authz.py`

freshness 目前以字符串拼接 `MAX(...)` 直接查询，**不过 AST 守卫、不带 RLS**。只能看华东的用户，引证中可能显示全国最新数据日期。且取的是第一个指标的时间字段（P1-09）。

- [ ] **Step 1: 写失败的 freshness 权限测试**

`test_freshness_authz.py`：受行级权限约束的用户看到的 freshness 不超出其可见范围；freshness 按**当前指标**的 time basis 取值而非第一个指标；freshness 查询过 AST 守卫。

- [ ] **Step 2: 实现摄取水位数据契约（优先方案）**

freshness 作为数据契约（source watermark）存入元数据，由数据管道写入。该字段同时是 S6 结果缓存键的成分。

- [ ] **Step 3: 实现过渡方案**

数据管道未就绪前：生成正常的 Canonical Plan（同数据集、同权限、`MAX(time_field)`），走完整流水线。两方案均按当前指标 time basis 取值。

---

### Task 6: Decimal 支持

**Files:**
- Modify: 数字格式化、同比计算、量级校验所在模块
- Create: `backend/tests/execution/test_decimal.py`

数字格式化、同比计算、量级校验目前用 `isinstance(x, (int, float))` 判断。PostgreSQL NUMERIC 返回 `Decimal`，**全部漏判**（P1-10）。

- [ ] **Step 1: 写失败的 Decimal 测试**

`test_decimal.py`：`Decimal` 值被正确识别（`numbers.Number`）；同比计算不转 float、保持精度；结果 schema 保留精度与单位；财务值不走浮点直接比较；量级校验对 Decimal 生效。

- [ ] **Step 2: 改判断与运算**

`isinstance` 改用 `numbers.Number`；同比等运算用 Decimal 安全算法。

---

### Task 7: 成本护栏递归化

**Files:**
- Modify: `backend/app/security/guardrails.py`
- Modify: `backend/app/core/config.py`（warehouse profile）
- Create: `backend/tests/security/test_guardrails_recursive.py`

`estimate_cost` 只读 `plan[0]["Plan"]["Plan Rows"]`，即**根节点输出行数**。`SELECT SUM(x) FROM huge_table` 全表扫描 1 亿行、根节点输出 1 行，护栏判 PASS 放行——护栏在最需要它的场景下完全失效（P1-11）。

- [ ] **Step 1: 写失败的递归护栏测试**

`test_guardrails_recursive.py`（用**真实 EXPLAIN**，不用手造 JSON）：大表扫描小聚合被 REJECT；warehouse profile 阈值生效；不同 profile 下同一查询判定不同。

- [ ] **Step 2: 实现递归遍历**

采集每节点的 `Plan Rows`、`Total Cost`、`Relation Name`、`Node Type`、`Plan Width`。判定信号：

| 信号 | 用途 |
|---|---|
| `max(所有节点 Plan Rows)` | 扫描量估计，取代根节点输出行数 |
| 根节点 `Total Cost` | 总成本 |
| 大表 `Seq Scan` 存在性 | 全表扫描告警 |
| sort/hash 的 `Plan Width × Plan Rows` | 内存压力估计 |
| join 节点数与类型 | 复杂度与 fanout 风险 |

- [ ] **Step 3: warehouse profile 阈值配置**

不同仓库成本量纲不可比，现有全局 `cost_reject_rows` 不足。同时记录估计值供 S5/S6 回看估计误差。

**先以 warn 观察一轮估计误差再收紧 reject**（避免误拒）。

---

### Task 8: 截断与重试

**Files:**
- Modify: `backend/app/execution/runner.py`
- Create: `backend/tests/execution/test_truncation_retry.py`

三个缺陷（P1-12）：

| 缺陷 | 现状 |
|---|---|
| 截断误报 | `truncated = len(rows) >= row_limit`，结果恰好等于 limit 行时误报 |
| 同连接重试 | 传入 `connection` 时超时后用同一连接重试；PostgreSQL 超时后事务 aborted，重试必然失败 |
| 字符串分类 | `_classify` 匹配 "timeout"、"canceling statement" 等消息文本，locale 或版本差异使分类失效 |

- [ ] **Step 1: 写失败的截断与重试测试**

`test_truncation_retry.py`：结果恰好 limit 行时 `truncated=false`；limit+1 行时裁剪正确且 `truncated=true`；超时后重试**用新连接**；`57014`/`08xxx`/`53xxx`/`40001` 各自分类正确；重试用指数退避 + 抖动。

- [ ] **Step 2: 改为请求 limit+1 并裁剪**

- [ ] **Step 3: 重试强制新连接**

或先显式 rollback。

- [ ] **Step 4: 分类改 SQLSTATE**

`57014` 查询取消、`08xxx` 连接类、`53xxx` 资源不足、`40001` 序列化失败。删除全部字符串匹配。该分类同时是 S6 `error_code` 的来源。

---

### Task 9: VQ 从 Plan 重编译

**Files:**
- Modify: `backend/app/observability/orm.py`（`VerifiedQueryRow`）
- Modify: `backend/app/pipeline/orchestrator.py`
- Create: `backend/migrations/007_verified_queries_canonical_plan.sql`
- Create: `backend/tests/pipeline/test_vq_recompile.py`

**Interfaces:**
- Changed: `verified_queries` 存 `canonical_plan`（JSONB）+ `semantic_revision_id`，**不再存 `fixed_sql`**；时间部分存 `TimeExpression`

S1 已关闭 VQ 直接执行 `fixed_sql` 的路径（列权限旁路 P0-04）。本任务恢复该能力并从根消除旁路。

- [ ] **Step 1: 写失败的 VQ 重编译测试**

`test_vq_recompile.py`：命中被 DENY 列的 VQ 时**拒绝**（不再有 `SUM(masked_col)` 逃过掩码的路径）；相对时间问题按**当前 anchor** 重算区间（同一个"本月销售额"在不同日期得到不同但都正确的区间）；`semantic_revision_id` 与当前版本不一致时不复用；VQ 与普通查询走**同一** `compile_policy()` 入口；响应标记所用 trusted asset 及验证时间。

- [ ] **Step 2: 存储迁移**

旧 `fixed_sql` **只读归档保留，不参与运行时**；无法转换为 plan 的条目标记 inactive 并记录原因（避免迁移丢失历史 VQ）。

- [ ] **Step 3: 实现运行时流程**

1. 精确文本命中只作为**高精度候选**，不是直接答案
2. 相对时间按当前 anchor 重新解析
3. 校验 `semantic_revision_id` 仍为当前版本
4. 从 plan 重走 `compile_policy()` → 权限编译 → 方言编译
5. 响应标记 trusted asset 及验证时间

- [ ] **Step 4: 确认 S1 的功能倒退已恢复**

跑 S1 Task 7 留下的降级测试，确认 VQ 路径恢复且断言已更新。

---

## 验收

1. 时间边界集、指标 DAG 前置校验、类型过滤、多指标口径集全部通过。
2. 相同 Canonical Plan 在同一 semantic revision 上生成稳定 AST（字节一致）。
3. Trusted Asset 不再保存可绕过当前语义与权限的物理 SQL。
4. 成本护栏对"大扫描小聚合"正确拒绝，不再误 PASS。
5. 截断判定精确；超时重试不复用 aborted 连接；分类基于 SQLSTATE。
6. 引证逐指标展示时间字段与版本。
7. VQ 恢复可用，且与普通查询共用同一权限编译入口。
8. 比较查询中仅存在于基期的维度值正确显示。
9. freshness 受行级权限约束，按当前指标 time basis 取值。
10. Decimal 值在格式化、同比、量级校验中不再漏判。
11. 测试全绿。
