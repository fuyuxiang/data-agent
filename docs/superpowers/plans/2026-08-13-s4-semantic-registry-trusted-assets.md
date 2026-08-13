# S4 语义层规模化与可信资产 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把语义层从"单张宽表 + 一个布尔发布位"升级为可治理的注册中心：统一指标 lineage、Predicate DSL 取代自由 SQL、枚举冲突发布期拦截、不可变 revision 状态机、物理契约校验、两跳 Join Graph 与 fanout 处置、Trusted Assets 治理。

**Architecture:** 核心是消除两套表达式解析器。当前 `compiler/metrics.py` 用 SQLGlot AST 找依赖，`security/columns.py` 的 `_metric_field_names` 用 `expression.replace("(", " ").split()` 字符串分词——**权限判断走字符串那套，编译走 AST 那套**，两者漂移即权限漏判（P1-05 的实际形态）。新的 `lineage.py` 是唯一入口，同一份 lineage 同时服务权限与编译。revision 完全不可变：修改语义等于新建 draft 并存整个数据集定义的快照，因为历史 Turn 的 `semantic_revision_id` 必须永远能解析出当时的定义，否则回放无意义。

**Tech Stack:** Python 3.13、SQLAlchemy 2.0、sqlglot 25.x、PostgreSQL 15+、pytest

## Global Constraints

约束来自 `docs/superpowers/specs/2026-08-13-s4-semantic-registry-trusted-assets-design.md`：

- **lineage 只有一个实现**。任何第二套表达式解析都是权限漏判的来源。
- **revision 完全不可变**。修改即新建 draft，存整个定义的快照而非增量。
- **LLM 不拼 Join**。路径只从预定义、已审核的 `relations` 中选取；多条路径且无默认路径时拒绝，不猜。
- **fanout 绝不静默放大**。检测到 grain 被放大时优先预聚合，不能预聚合则拒绝。不存在"产出放大数字"这个选项。
- drift scan 只告警不阻断——上游一次 DDL 不应打挂整个问数服务；发布门禁才阻断。
- **Trusted Asset 必须过期**。"半年前验证过的答案"不构成可信。
- Predicate DSL 禁子查询、禁非白名单函数（复用 S1 的函数白名单）。
- 代码注释与标识符用英文；文档与提交信息用中文。

## 前置

依赖 S1（角色与鉴权、函数白名单）、S2（typed literal、`logical_type`）、S3（Canonical Plan、`required_field_lineage`、VQ 已迁移为存 canonical_plan）。

本计划的 drift scan 只实现检查逻辑与告警语义，**调度在 S6**。`eval_excluded` 标记本轮只提供，**评测隔离的强制执行在 S5**。

---

### Task 1: 统一指标 lineage

**Files:**
- Create: `backend/app/semantic/lineage.py`
- Create: `backend/tests/semantic/test_lineage.py`
- Modify: `backend/app/security/columns.py`（删除 `_metric_field_names`）
- Modify: `backend/app/compiler/metrics.py`
- Modify: `backend/tests/security/test_columns.py`

**Interfaces:**
- Produces:
  - `app.semantic.lineage.metric_dag(dataset) -> MetricDag` — 全数据集指标依赖图
  - `app.semantic.lineage.field_lineage(dataset, metric_name) -> frozenset[str]` — 穿透到物理列
- Removed: `app.security.columns._metric_field_names`（字符串分词那套）

- [ ] **Step 1: 写失败的 lineage 一致性测试**

`test_lineage.py`：
- 复合指标穿透到全部底层物理列
- 嵌套两层以上的复合指标同样穿透
- **权限检查与编译得到完全相同的列集合**（这是本任务的核心断言——两套解析器漂移即权限漏判）
- 字符串分词会漏掉的场景（如 `SUM(CASE WHEN a THEN b END)`）现在能正确解析出 `a`、`b`

- [ ] **Step 2: 实现 `lineage.py`**

`MetricDag` 在**发布时构建并缓存于 semantic revision**，运行时不重复解析。

- [ ] **Step 3: 删除 `_metric_field_names`，两处改调 `field_lineage`**

`security/columns.py` 与 `compiler/metrics.py` 共用同一入口。删除后跑全量回归，确认无残留调用。

- [ ] **Step 4: 写失败的发布门禁检测测试**

| 检查 | 说明 |
|---|---|
| 循环依赖 | 现有 `resolve_metric_dependencies` 只排除自引用，间接循环（A→B→A）会**无限递归** |
| 单位运算合法性 | 金额 ÷ 金额 = 无单位比率；金额 + 数量 = 错误 |
| 可加性 | `recalculate` 指标不得被 SUM 包裹。现有检查只覆盖直接聚合，未覆盖复合指标内的嵌套 |
| 时间字段 | 存在且可用 |
| 引用范围 | 表达式只引用同数据集内已知指标/字段 |

- [ ] **Step 5: 实现五项检测**

---

### Task 2: Predicate DSL

**Files:**
- Create: `backend/app/semantic/predicate_dsl.py`
- Create: `backend/tests/semantic/test_predicate_dsl.py`
- Modify: `backend/app/semantic/orm.py`
- Create: `backend/migrations/008_predicate_dsl.sql`

**Interfaces:**
- Produces: `Predicate` DSL — 字段 + 操作符 + 类型化值 + and/or 组合
- Changed: `fixed_filter: str` → 结构化 DSL

`fixed_filter` 是自由 SQL 文本，经 `sqlglot.parse_one` 直接塞入 `exp.Filter`：方言绑定、可写任意函数、**且 filter 中引用的列不进入任何 lineage 检查——列权限完全绕过**（P1-06）。

- [ ] **Step 1: 写失败的 DSL 测试**

`test_predicate_dsl.py`：DSL 中的字段**自动进入 `required_field_lineage`** 因此受列权限检查；子查询被拒；非白名单函数被拒（复用 S1 函数白名单）；and/or 嵌套组合正确编译；类型化值按 S2 的 `logical_type` 构造。

关键断言：DSL 中引用 DENY 列的过滤器被拒绝——这是当前完全缺失的检查。

- [ ] **Step 2: 实现 DSL 与编译**

- [ ] **Step 3: 迁移旧文本**

发布前解析并规范化为 DSL；**无法转换的标 ERROR 阻止发布**。

DSL 表达力弱于自由 SQL（如复杂 CASE 无法表达），这是有意的取舍——当前形态是一个完整的列权限旁路。

---

### Task 3: 枚举别名冲突

**Files:**
- Modify: `backend/app/semantic/resolver.py`（`resolve_enum` 所在模块）
- Modify: `backend/app/semantic/lint.py`
- Create: `backend/tests/semantic/test_enum_conflict.py`

**Interfaces:**
- Changed: `resolve_enum` 返回**候选列表**而非首个匹配

`resolve_enum` 遍历时第一个匹配即返回。两个枚举值持有相同别名时静默选择前者，**可能映射到错误实体**（P1-08）。

- [ ] **Step 1: 写失败的枚举冲突测试**

`test_enum_conflict.py`：归一化（**NFKC + casefold + 去空白**）后的别名冲突在发布 lint 中为 ERROR；`resolve_enum` 返回 0 个候选 → 澄清；1 个 → 使用；N 个 → **必须澄清**（不再静默取首个）。

- [ ] **Step 2: 实现 lint 检测与候选列表返回**

- [ ] **Step 3: 大字典检索索引**

引入受权限约束的检索索引，不把全部枚举值塞入 prompt（既是成本问题也是泄漏面）。

---

### Task 4: Semantic Revision 状态机

**Files:**
- Create: `backend/app/semantic/revision.py`
- Create: `backend/app/semantic/diff.py`
- Create: `backend/migrations/009_semantic_revisions.sql`
- Create: `backend/tests/semantic/test_revision.py`
- Create: `backend/tests/semantic/test_revision_diff.py`
- Modify: `backend/app/semantic/orm.py`（`DatasetRow` 增 `published_revision_id`）
- Modify: `backend/app/semantic/loader.py`（按 revision 加载）
- Modify: `backend/app/api/semantic.py`

**Interfaces:**
- Produces: `semantic_revisions` 表 — 存整个数据集定义的快照（JSONB）
- 状态机：`draft → linted → approved → published → retired`
- Changed: 运行时语义模型**按 revision 加载**，而非按 `DatasetRow` 拼装

`DatasetRow.is_published` 是一个布尔，发布即 `row.is_published = True`。无版本、无历史、无法回滚、无法 diff、无审批人记录。而 S3 的 Canonical Plan 已引用 `semantic_revision_id`——**该实体必须真实存在**（P2-02）。

- [ ] **Step 1: 写失败的状态机测试**

`test_revision.py`：

| 状态 | 断言 |
|---|---|
| `draft` | 可编辑，**不可用于问答** |
| `linted` | 通过语义体检，记录 lint 报告 |
| `approved` | 由 `semantic_approver` 审批（复用 S1 角色），记录审批人与时间 |
| `published` | 同一 dataset **只能有一个** |
| `retired` | 被取代，仍可用于回放历史 Turn |

外加：revision 一旦离开 draft 即不可修改（不可变性）；历史 Turn 的 `semantic_revision_id` 永远能解析出当时定义；非法状态转移被拒。

- [ ] **Step 2: 建表与实现状态机**

修改语义 = 从当前 published 复制出 draft → 走流程 → 发布时原 published 转 retired。

- [ ] **Step 3: 写失败的 diff 测试**

`test_revision_diff.py`：

| 类别 | 变更 | 处置 |
|---|---|---|
| 兼容 | 新增字段/指标、补充同义词 | 可直接发布 |
| 破坏性 | 删除指标、改指标口径、改 time basis、改物理列映射 | 需额外确认，**并列出受影响的 trusted assets 与 Golden Case** |

破坏性变更是重点：改了指标口径而不重跑 VQ 验证，等于让已验证答案静默变错。

- [ ] **Step 4: 实现 diff 与兼容性分类**

- [ ] **Step 5: 实现回滚**

把某个 retired revision 重新置为 published：**新建一条指向它的发布记录，不修改历史行**。目标 15 分钟内完成（对应 `fangan.md` 9.2 SLO）。

- [ ] **Step 6: 运行时改为按 revision 加载**

`loader.py` 不再按 `DatasetRow` 拼装。`DatasetRow` 保留为当前指针。

---

### Task 5: 物理契约校验与发布门禁

**Files:**
- Create: `backend/app/semantic/physical_contract.py`
- Create: `backend/app/semantic/drift.py`
- Create: `backend/tests/semantic/test_physical_contract.py`
- Create: `backend/tests/semantic/test_publish_gate.py`
- Modify: `backend/app/semantic/lint.py`

- [ ] **Step 1: 写失败的物理契约测试**

`test_physical_contract.py`：表不存在 → 阻止发布；列不存在 → 阻止；类型不匹配 → 阻止；**当前 DB role 无 SELECT 权限 → 阻止**（连接 `information_schema` 校验）。

- [ ] **Step 2: 实现物理契约校验**

- [ ] **Step 3: 实现 drift scan 检查逻辑**

漂移作为**告警，不阻断线上查询**——上游一次 DDL 不应打挂整个问数服务。调度在 S6，本轮只实现检查逻辑与告警语义。

- [ ] **Step 4: 数据健康检查**

进入 revision：freshness（配合 S3 的摄取水位）、唯一性、非空率、基数。

- [ ] **Step 5: 完整发布门禁清单**

`test_publish_gate.py` 逐项覆盖：物理表/列与类型、指标 DAG 循环、单位运算、聚合合法性、时间字段、枚举冲突、join fanout、策略引用、**VQ 重编译**、Golden smoke set、兼容性 diff。

其中 VQ 重编译：发布前用新 revision 重编译所有关联 trusted asset，**编译失败或 lineage 变化的必须处理，否则阻止发布**。

---

### Task 6: 关系与 Join Graph

**Files:**
- Create: `backend/app/semantic/relations.py`
- Create: `backend/app/compiler/join_path.py`
- Create: `backend/migrations/010_relations.sql`
- Create: `backend/tests/compiler/test_join_path.py`
- Create: `backend/tests/compiler/test_fanout.py`
- Modify: `backend/app/planning/canonical.py`（`required_field_lineage` 带数据集限定）

**Interfaces:**
- Produces: `relations` 表 — `left_dataset`/`right_dataset`/`join_keys[]`/`cardinality`/`optionality`/`fanout_risk`/`allowed_directions[]`/`is_default_path`
- Changed: `required_field_lineage` 扩展为带数据集限定的列（`dataset.column`）

本轮范围：**已审核的静态 Join Graph，最多两跳**。

- [ ] **Step 1: 写失败的路径选择测试**

`test_join_path.py`：单一路径正常选取；**多条路径且无默认路径时拒绝**（要求语义管理员显式定义，不猜）；有 `is_default_path` 时选默认；超过两跳时拒绝；未在 `allowed_directions` 中的方向被拒。

- [ ] **Step 2: 建表并实现路径选择**

编译器按 `measures` 与 `group_by` 涉及的数据集在静态图上寻路。

- [ ] **Step 3: 写失败的 fanout 测试**

`test_fanout.py`：`one_to_many` join 后对左表指标求和会重复计数——断言检测到 grain 被放大；可预聚合时**先在原 grain 聚合为子查询再 join**，结果数值正确；不可预聚合时**拒绝**；不存在"产出放大数字"的输出路径。

- [ ] **Step 4: 实现 fanout 检测与预聚合**

指标声明所属 grain，编译时检测 join 后 grain 是否被放大。

- [ ] **Step 5: 权限检查覆盖 join key**

`required_field_lineage` 带数据集限定。**join key 本身也是列，也可能敏感**——断言引用 DENY join key 时被拒。

---

### Task 7: Trusted Assets 治理

**Files:**
- Modify: `backend/app/observability/orm.py`（`VerifiedQueryRow` → trusted asset）
- Create: `backend/app/semantic/trusted_asset.py`
- Create: `backend/migrations/011_trusted_assets.sql`
- Create: `backend/tests/semantic/test_trusted_asset.py`

**Interfaces:**
- 完整字段：`id`/`name`/`domain`/`trigger_questions[]`/`canonical_plan`（S3 已迁移）/`parameter_schema`/`semantic_revision_id`/`verified_by`/`verified_at`/`review_status`/`last_validated_at`/`last_validation_result`/`expires_at`/`is_active`/`usage_count`/`failure_count`/`eval_excluded`

- [ ] **Step 1: 写失败的治理能力测试**

`test_trusted_asset.py`：

| 能力 | 断言 |
|---|---|
| 参数化 | "华东本月销售额"与"华南上月销售额"共用一个 asset + 参数，而非两条记录 |
| 过期 | `expires_at` 到期后**停止召回**，需重新验证 |
| 失效追踪 | `failure_count` 累积，连续失败**自动置 inactive 并告警** |
| 召回升级 | 精确文本 → canonical signature（结构化匹配）→ 可选 embedding **仅作候选生成**；最终必须按结构化兼容性 + semantic revision + 权限过滤；记录 `match_reason` 与 `confidence` |

- [ ] **Step 2: 建表并实现治理能力**

- [ ] **Step 3: `eval_excluded` 标记**

本轮只提供标记支持。被测 trusted asset 必须从运行时召回集合中移除（否则等于"拿答案做提示再证明自己答对"），该约束**由 S5 评测框架强制执行**。

---

## 验收

1. lineage 只有一个实现，权限与编译得到相同列集合；`_metric_field_names` 已删除且无残留调用。
2. 五项指标 DAG 检测生效，间接循环依赖不再无限递归。
3. `fixed_filter` 改为 Predicate DSL，DSL 字段进入 `required_field_lineage` 并受列权限检查；无法转换的旧文本阻止发布。
4. 枚举别名冲突在发布期为 ERROR；运行时多候选必须澄清。
5. `semantic_revisions` 状态机落地，revision 不可变，历史 Turn 可解析当时定义。
6. 破坏性 diff 列出受影响的 trusted assets 与 Golden Case。
7. 回滚为新建发布记录，不修改历史行。
8. 物理契约校验四项在发布门禁生效；drift scan 检查逻辑可用且只告警。
9. 完整发布门禁 11 项全部覆盖，含 VQ 重编译。
10. Join 路径多解无默认时拒绝；最多两跳；fanout 可预聚合则预聚合、否则拒绝。
11. 权限检查覆盖 join key。
12. Trusted Asset 四项治理能力生效，`expires_at` 到期停止召回。
13. 测试全绿。
