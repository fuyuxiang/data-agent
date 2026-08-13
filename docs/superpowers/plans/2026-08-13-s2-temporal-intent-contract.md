# S2 时间语义与意图契约 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让时间语义完全确定：模型不再输出绝对日期，改由 `TimeResolver` 计算；移除全局可变时钟；IntentV2 落地含 per-metric confidence 与 7 类跨字段校验；LLM Provider 抽象与五类失败分类；scope policy 由代码强制；`trend`/`detail` 真正可执行；字面量按声明类型构造。

**Architecture:** 时间是一条单向流水线：模型输出 `TimeExpression`（相对表达式的结构化形式）→ `TimeResolver(clock, timezone, fiscal_calendar)` 计算 `ResolvedTimeRange` → 编译器只消费已解析的绝对区间。模型不碰日期算术，因为它不知道今天是几号且不会做月末 clamp。时钟从模块级可变状态改为协议 + 依赖注入，测试用 `FixedClock` 而非全局 `freeze()`——后者让测试互相污染，且 `now()` 恒返当天 09:00 的 bug 正源于这套设计。授权与禁区判定始终在确定性代码里：模型只输出候选与歧义，`PolicyResolver` 裁决。

**Tech Stack:** Python 3.13、Pydantic 2、sqlglot 25.x、zoneinfo、OpenAI Structured Outputs、pytest

## Global Constraints

约束来自 `docs/superpowers/specs/2026-08-13-s2-temporal-intent-contract-design.md`：

- **模型不输出绝对日期**。任何让模型算日期的路径都是缺陷。
- **模型不参与授权决策**。scope policy 由代码强制，模型无法绕过。
- 无模块级可变时钟。时间依赖一律注入。
- 日历运算在用户时区做，之后转 UTC；不在 UTC 上做日历运算。
- 拒绝文案不回显策略内容——否则策略可被逐次探测还原。
- 字面量按字段声明类型构造，不按字符串外观猜测；类型不匹配拒绝而非强转。
- `detail` 可见列是显式白名单（默认 false），不是"所有非 DENY 列"。
- 五类 provider 失败各自映射独立 `error_code`，不合并为"模型失败"。
- 代码注释与标识符用英文；文档与提交信息用中文。

## 前置

依赖 S1 的 `PrincipalContext`（principal 属性携带 `timezone`）、配置校验、`app/core/errors.py` 的 error taxonomy。

本计划负责修复 S1 遗留的那 1 个红用例：`tests/intent/test_schema.py::test_aggregate_intent_requires_at_least_one_metric`（P0-08）。本计划完成后测试应全绿。

---

### Task 1: 确定性时间基座

**Files:**
- Create: `backend/app/temporal/__init__.py`
- Create: `backend/app/temporal/clock.py`
- Create: `backend/app/temporal/timezone.py`
- Create: `backend/app/temporal/fiscal.py`
- Create: `backend/app/temporal/resolver.py`
- Create: `backend/tests/temporal/__init__.py`
- Create: `backend/tests/temporal/test_clock.py`
- Create: `backend/tests/temporal/test_fiscal.py`
- Create: `backend/tests/temporal/test_resolver.py`
- Delete: `backend/app/core/clock.py`
- Modify: 全部 `core.clock` 调用点与 `frozen_clock` fixture

**Interfaces:**
- Produces:
  - `app.temporal.clock.Clock` — 协议，`now() -> datetime`（tz-aware）
  - `app.temporal.clock.SystemClock` — 返回 `datetime.now(timezone.utc)`
  - `app.temporal.clock.FixedClock(instant)` — 测试注入，无模块级可变状态
  - `app.temporal.timezone.resolve_timezone(principal) -> ZoneInfo` — 默认 `Asia/Shanghai`
  - `app.temporal.fiscal.FiscalCalendar(year_start_month, week_start)`
  - `app.temporal.resolver.TimeResolver(clock, timezone, fiscal_calendar)`，`resolve(expression) -> ResolvedTimeRange`
  - `app.temporal.resolver.ResolvedTimeRange` — `start`/`end`/`grain`/`assumptions`

- [ ] **Step 1: 写失败的时钟测试**

`test_clock.py`：`SystemClock.now()` 返回 tz-aware UTC（`tzinfo is not None`）；`FixedClock` 返回注入值；两个 `FixedClock` 实例互不影响（无共享状态）；模块中不存在 `freeze`/`unfreeze`/模块级可变变量。

当前 `core/clock.py` 的 `now()` 恒返当天 09:00（P0-07），断言新实现返回真实时刻。

- [ ] **Step 2: 实现 `clock.py` 与 `timezone.py`**

`Clock` 用 `typing.Protocol`。`SystemClock` 无状态。`FixedClock` 持有实例级 instant。

- [ ] **Step 3: 写失败的财务日历与边界测试**

`test_fiscal.py` / `test_resolver.py` 逐条覆盖 spec §3.3 的边界规则：

| 场景 | 断言 |
|---|---|
| 月末偏移 | 1月31日 + 1 月 → 2月28/29 日（clamp 到目标月长度） |
| 闰年 | 2/29 在非闰年偏移后为 2/28 |
| 跨年 | 12 月 offset −1 落到上一年 11 月 |
| 季度边界 | 按财务日历季度定义，非固定自然季 |
| `to_date` 右端点 | **昨天**（当天数据通常不完整），且假设写入 `assumptions` |
| DST | 在用户时区计算日界后转 UTC |
| 跨时区 | 同一时刻在不同时区落在不同日期 |

外加每个 `unit × offset × to_date` 组合、财年起始非 1 月、周起始日两种配置（ISO 周一 / 周日）。

- [ ] **Step 4: 实现 `fiscal.py` 与 `resolver.py`**

`to_date=true` 时右端点取昨天，并把该假设显式写入 `ResolvedTimeRange.assumptions` 供答案层呈现——这是必须让用户看到的口径。

- [ ] **Step 5: 迁移 `comparison_range`**

`compiler/time_windows.py` 的 `comparison_range` 逻辑基本正确（`_shift_months` 已有月末 clamp），迁入 `app/temporal/`，改为接收 `ResolvedTimeRange`，补 DST 与财务日历支持。

- [ ] **Step 6: 删除 `core/clock.py` 并改造 fixture**

删除模块级 `_frozen`/`freeze()`/`unfreeze()`。所有现有时间相关测试的 `frozen_clock` fixture 改为注入 `FixedClock`。**这一步作为独立提交**——影响面覆盖全部时间相关测试。

注意 `tests/golden/conftest.py:19-25` 的 autouse 全局冻结也在此移除；Golden Set 的逐 case 时钟注入由 S5 完成。

---

### Task 2: IntentV2 契约与跨字段校验

**Files:**
- Create: `backend/app/intent/v2.py`
- Create: `backend/tests/intent/test_v2_schema.py`
- Modify: `backend/app/intent/schema.py`（保留一轮做兼容转换）
- Modify: `backend/app/intent/recognizer.py`
- Modify: `backend/app/intent/prompt.py`
- Modify: `backend/tests/intent/test_schema.py`

**Interfaces:**
- Produces:
  - `app.intent.v2.IntentV2`
  - `app.intent.v2.TimeExpression` — `kind`(relative/absolute/range/none)/`text`/`unit`/`offset`/`to_date`/`start`/`end`
  - `app.intent.v2.MetricRef` — `name`/`confidence`
  - 旧 `schema.py` → `v2` 的兼容转换（供未迁移的 Golden Set 与 `intent_snapshot` 读取）

- [ ] **Step 1: 写失败的 IntentV2 契约测试**

`test_v2_schema.py` 覆盖 §4.2 的 7 类跨字段校验：

| 规则 | 内容 |
|---|---|
| aggregate | 至少一个 metric |
| trend | 必须有 `time_expression` 且 unit 不为 none |
| ranking | 必须有 sort，且 `sort.limit` ∈ `1..max_top_n` |
| detail | 不允许 metrics |
| unsupported | 其余槽位必须为空 |
| confidence | 所有取值限 `[0,1]` |
| operator 值数量 | `eq/ne/gt/gte/lt/lte` 恰 1；`in/not_in` ≥1；`between` 恰 2 |

同时断言 `tests/intent/test_schema.py::test_aggregate_intent_requires_at_least_one_metric`（现为红，P0-08）转绿。

- [ ] **Step 2: 实现 `v2.py`**

契约变更：

| 变更 | 原因 |
|---|---|
| `time: TimeRange` → `time_expression: TimeExpression` | 模型不再算绝对日期 |
| 移除 `filters[].values` | 该字段是 resolver 阶段产物，不属于模型输出契约 |
| 新增 `domain_candidates[]` | 多数据域路由预留；当前单域填一项 |
| 新增 `ambiguities[]` | 模型主动报告歧义，取代靠低 confidence 反推 |
| `metrics: list[str]` → `list[MetricRef{name, confidence}]` | per-metric 置信度 |

`TimeExpression` 与 `MetricRef` 保持**扁平**——Structured Outputs 的 strict 模式对嵌套 schema 有限制。

- [ ] **Step 3: `metrics` 契约迁移**

`list[str]` → `list[MetricRef]` 影响编译器、resolver、answer 与 Golden Set 断言。**作为单独一次机械迁移提交**，旧 `schema.py` 保留兼容层使 `intent_snapshot` 读取不中断。

- [ ] **Step 4: 保留现有验证层不动**

`recognizer.py` 已有的能力全部保留：禁 SQL 片段检测（`_assert_no_sql`）、`extra="forbid"`、已知引用校验（`_assert_known_references`）、以及区分"幻觉指标"与"存在但无权限指标"（后者按权限拒答）。这套设计是对的，不要在迁移中弄丢。

- [ ] **Step 5: 改 prompt**

删除 `"time": {"start": "YYYY-MM-DD", ...}` 这个结构——prompt 要求模型输出绝对日期却从不告知今天是几号，模型只能猜（P0-07 的实际形态）。改为输出 `time_expression`。

同时给 prompt 模板加显式**版本号**（S5 的可复现三要素之一、S6 的 Trace 字段之一）。

---

### Task 3: LLM Provider 抽象与失败分类

**Files:**
- Create: `backend/app/llm/__init__.py`
- Create: `backend/app/llm/provider.py`
- Create: `backend/app/llm/structured.py`
- Create: `backend/app/llm/resilience.py`
- Create: `backend/tests/llm/__init__.py`
- Create: `backend/tests/llm/test_failure_classes.py`
- Create: `backend/tests/llm/test_prompt_injection.py`
- Modify: `backend/app/intent/recognizer.py`
- Move: `OpenAiCompatClient` → `app/llm/`

**Interfaces:**
- Produces:
  - `app.llm.provider.IntentModel` — 接口，业务层不接触 OpenAI SDK 对象
  - `app.llm.structured.schema_from_model(model)` — Pydantic → JSON Schema，strict 模式
  - `app.llm.resilience` — 超时、指数退避 + 抖动重试、熔断

- [ ] **Step 1: 写失败的五类失败测试**

`test_failure_classes.py`，每类映射**独立** `error_code`（不合并为"模型失败"）：

| 失败 | 处置 |
|---|---|
| refusal | 按拒答返回，记录 refusal 原因分类 |
| incomplete / max tokens | 重试一次并降低输出体积；仍失败则失败留痕 |
| provider error（429/5xx） | 指数退避 + 抖动重试；熔断后走 fallback |
| schema mismatch | **不重试原样请求**，记录原始输出到 admin 级 trace |
| timeout | 按 provider error 处理，但**单独计量** |

外加：fallback 模型输出仍须过同一 Schema（不降低安全契约）。

- [ ] **Step 2: 实现 `provider.py` 与 `structured.py`**

`OpenAiCompatClient` 移入并实现 `IntentModel`。Structured Outputs 用 `strict` 模式。**先以真实 provider 验证 schema 可被接受**，再固化。

- [ ] **Step 3: 实现 `resilience.py`**

熔断状态不做全局单例（避免测试污染），随 client 实例。

- [ ] **Step 4: 模型配置与记录**

生产用固定 snapshot 不用 alias（alias 仅用于 shadow / eval）。Trace 记录 response id、model snapshot、usage、latency、prompt version。设 `store=false`；`safety_identifier` 传 principal id 的 **HMAC**（稳定且不可反推）。

- [ ] **Step 5: 写失败的注入测试并加固 prompt**

`test_prompt_injection.py`：在字段描述、数据集描述、字段同义词中写入"忽略以上指令"等内容，断言输出结构不变。

语义元数据是管理面可写入的内容，构成 injection 面。Prompt 中以明确边界包裹并声明其为**不可信数据而非指令**。

---

### Task 4: 可执行 Scope Policy

**Files:**
- Create: `backend/app/semantic/scope_policy.py`
- Create: `backend/migrations/003_scope_policies.sql`
- Create: `backend/tests/semantic/test_scope_policy.py`
- Modify: `backend/app/semantic/orm.py`
- Modify: `backend/app/pipeline/orchestrator.py`

**Interfaces:**
- Produces:
  - `scope_policies` 表 — `dataset_name`/`policy_kind`/`subject_pattern`/`decision`/`required_filters[]`/`max_time_span_days`
  - `policy_kind` ∈ `topic_allow`/`topic_deny`/`metric_deny`/`required_filter`/`max_span`
  - `app.semantic.scope_policy.PolicyResolver.decide(candidates, ambiguities, principal) -> allow | deny | require_clarification`

- [ ] **Step 1: 写失败的 scope policy 测试**

`test_scope_policy.py`：`topic_deny` 命中时拒绝；`metric_deny` 命中时拒绝；`required_filter` 缺失触发澄清；`max_span` 超限时拒绝；**拒绝文案不含任何策略内容**（否则策略可被逐次探测还原）；模型输出 `topic_allow` 但策略表为 deny 时以策略表为准（模型无法绕过）。

- [ ] **Step 2: 建表与迁移脚本**

`forbidden_scenario` 目前只是拼进 prompt 的一句话，模型可无视——管理面写"禁止查询个人薪酬明细"没有任何代码强制（P1-07）。现有 `forbidden_scenario` 数据迁移为 `topic_deny` 行。

- [ ] **Step 3: 实现 `PolicyResolver` 并接入编排器**

模型输出 `domain_candidates` 与 `ambiguities`，`PolicyResolver` 做确定性裁决。裁决点在意图识别之后、编译之前。

---

### Task 5: kind 可执行契约

**Files:**
- Modify: `backend/app/compiler/query.py`
- Modify: `backend/app/semantic/orm.py`（`FieldDef` 增 `is_detail_visible`）
- Create: `backend/migrations/004_field_detail_visible.sql`
- Create: `backend/tests/compiler/test_trend.py`
- Create: `backend/tests/compiler/test_detail.py`

**Interfaces:**
- Changed: `compiler/query.py` 对 `trend` 做时间分桶、对 `detail` 产出分页明细
- Produces: `FieldDef.is_detail_visible`（默认 **false**）

`compiler/query.py` 现在对四种 kind 全部生成聚合 SQL：`trend` 不做时间分桶、`detail` 不返明细行——用户问趋势，拿到一个总数（P1-01）。

- [ ] **Step 1: 写失败的 trend 测试**

`test_trend.py`：生成 SQL 含 `date_trunc` 且按桶排序；无 dimension 时单序列；有 dimension 时多序列；grain 与指标 time basis 不兼容时（如日粒度指标问小时趋势）返回明确 `error_code`——**不静默降级为可执行但错误的查询**。

- [ ] **Step 2: 实现 trend 编译**

按 grain 做 time bucket，按桶 group / order。

- [ ] **Step 3: 写失败的 detail 测试**

`test_detail.py`：只返 `is_detail_visible=true` 的列；强制分页；默认排序为时间倒序；MASK 列在明细中同样掩码；`is_detail_visible` 未标注的列不出现在结果中。

明细行绕过聚合，列权限暴露面远大于聚合结果，所以是显式白名单而非"所有非 DENY 列"。

- [ ] **Step 4: 实现 detail 编译**

- [ ] **Step 5: 标注现有字段的 `is_detail_visible`**

迁移脚本按现有 `is_groupable` + 非敏感字段给出**建议初值**，仍需人工确认——默认 false 会让 detail 初期无可见列。这是本轮的配置工作。

- [ ] **Step 6: 新 SQL 形状过安全回归**

trend/detail 引入新 SQL 形状，安全面扩大。断言新形状同样过 S1 的函数白名单与 AST 守卫；detail 额外加列白名单断言。

---

### Task 6: 字面量类型

**Files:**
- Modify: `backend/app/compiler/predicates.py`
- Modify: `backend/app/semantic/orm.py`（`FieldDef` 增 `logical_type`/`physical_type`）
- Create: `backend/migrations/005_field_logical_type.sql`
- Create: `backend/tests/compiler/test_literal_types.py`

**Interfaces:**
- Produces: `FieldDef.logical_type` ∈ `string`/`integer`/`decimal`/`date`/`timestamp`/`boolean`/`enum`；`FieldDef.physical_type`

- [ ] **Step 1: 写失败的字面量类型测试**

`test_literal_types.py`：前导零 ID（`"007"`）不被转为数字；Decimal 保持精度不走 float；日期字符串按日期比较而非字符串比较；类型不匹配时**拒绝而非强转**。

`compiler/predicates.py` 现在按字符串外观猜类型（`float()` 试探），导致 ID、前导零、Decimal、日期比较出错（P1-03）。

- [ ] **Step 2: 加字段类型声明并按声明构造 literal**

AST literal 按字段声明类型构造，删除全部外观猜测代码。

---

## 验收

1. 时间边界集全部通过：月末、闰年、跨年、季度、DST、财务日历、周起始日。
2. `clock.now()` 返回 tz-aware UTC；无模块级可变时钟；测试通过注入 `FixedClock` 实现确定性。
3. P0-08 的 7 类跨字段校验全部生效，`test_aggregate_intent_requires_at_least_one_metric` 转绿，**测试全绿**。
4. `trend` 产出真正的分桶时间序列；`detail` 产出分页明细且只含白名单列。
5. `scope_policies` 中的禁区由代码强制，模型无法绕过；拒绝文案不泄漏策略。
6. 五类 provider 失败各自可观测、各有 `error_code`。
7. 字面量按字段声明类型构造，无 `float()` 外观猜测残留。
8. prompt 中不再有绝对日期结构；prompt 模板有显式版本号。
9. `recognizer.py` 现有验证层（禁 SQL、`extra="forbid"`、已知引用校验、幻觉/无权限指标区分）全部保留。
