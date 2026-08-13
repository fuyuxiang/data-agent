# S2 时间语义与 Intent 契约 设计文档

> 上游依据：`fangan.md` 第 5.1（P0-07/P0-08）、5.2（P1-01/P1-03/P1-07）、6.2（IntentV2）、6.3（LLM 接入）节。
> 子项目定位：升级路线 S2，依赖 S1 的 `PrincipalContext` 与配置校验。
> 覆盖问题：P0-07、P0-08、P1-01、P1-03、P1-07。

## 1. 目标与非目标

### 目标

让"系统声称支持的意图"与"实际能正确执行的能力"一致，并把时间从模型猜测变为确定性计算：

- 绝对时间由代码计算，模型只输出相对表达式。
- 时钟 tz-aware，无全局可变状态。
- IntentV2 契约完备，非法意图在校验阶段即被拒绝，不延迟到编译期。
- LLM provider 可替换，五类调用失败各有明确归类。
- 业务禁区可执行，不再依赖模型自觉遵守 prompt。
- `trend` 与 `detail` 真正实现，不再伪装为聚合。

### 非目标

| 项 | 去向 |
|---|---|
| CanonicalQueryPlan 与 SecuredExecutionPlan | S3 |
| 多指标 time basis 校验、Decimal、COALESCE、EXPLAIN 递归 | S3 |
| 指标表达式 DAG、Predicate DSL、别名冲突 lint | S4 |
| 多数据域实际路由（本轮 `domain_candidates` 只填一项） | S4 |
| Golden Set 时间边界集的评测框架改造 | S5（本轮只写单测） |
| Prompt 缓存与成本优化 | S6 |

## 2. 核心设计决策

### 2.1 模型不输出绝对日期

当前 prompt 要求模型输出 `"time": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}`，却从不告知今天是几号。模型只能猜——这是 P0-07 的实际形态，也是"本月/上月"不可靠的根因。

IntentV2 改为输出 `TimeExpression`（相对表达式的结构化形式），绝对区间由 `TimeResolver` 计算。

### 2.2 移除全局可变时钟

`core/clock.py` 的模块级 `_frozen` + `freeze()`/`unfreeze()` 让测试互相污染，且 `now()` 恒返当天 09:00 的 bug 正源于这套设计。改为 `Clock` 协议 + 依赖注入。

### 2.3 `to_date` 右端点取昨天

"本月至今"的右端点默认为昨天，而非今天：当天数据通常不完整。这是一个必须显式声明的假设，写入 `assumptions` 由答案层呈现。

### 2.4 per-metric confidence

`metrics: list[str]` + 四槽位整体 confidence 无法表达"指标 A 确定、指标 B 不确定"。改为 `list[MetricRef{name, confidence}]`。改动面覆盖编译器、resolver、answer 与 Golden Set 断言，但这是澄清决策的正确输入。

### 2.5 LLM 只做候选分类，PolicyResolver 裁决

模型输出候选与歧义，确定性代码做最终授权与禁区判定。任何让模型参与授权决策的设计都不可审计。

### 2.6 `detail` 的允许列是显式白名单

明细行绕过聚合，列权限暴露面远大于聚合结果。`detail` 可见列须显式标记 `is_detail_visible`（默认 false），而非"所有非 DENY 列"。

## 3. 确定性时间语义

### 3.1 新增 `app/temporal/`

取代 `app/core/clock.py`（删除）。

- `clock.py`：`Clock` 协议；`SystemClock` 返回 `datetime.now(timezone.utc)`（tz-aware）；`FixedClock` 供测试注入。无模块级可变状态。
- `timezone.py`：用户时区解析。principal 属性携带 `timezone`，默认 `Asia/Shanghai`。绝对区间在用户时区计算后转 UTC 存储。
- `fiscal.py`：`FiscalCalendar(year_start_month, week_start)`。支持自然年（默认）与自定义财年；周起始日可配（ISO 周一 / 周日）。
- `resolver.py`：`TimeResolver(clock, timezone, fiscal_calendar)`，`resolve(expression) -> ResolvedTimeRange`。

### 3.2 `TimeExpression`

模型输出的时间契约，取代 `TimeRange`：

```
kind: relative | absolute | range | none
text: str            # 用户原话，用于引证
unit: day | week | month | quarter | year
offset: int          # 0=本期，-1=上期
to_date: bool        # 本月至今 vs 整月
start / end: date | None   # 仅 kind=absolute/range 时有值
```

### 3.3 边界规则

以下规则写死并逐一测试：

| 场景 | 规则 |
|---|---|
| 月末偏移 | 1月31日 + 1 月 = 2月28/29 日（clamp 到目标月长度） |
| 闰年 | 2/29 在非闰年偏移后为 2/28 |
| 跨年 | 12 月 offset -1 落到上一年 11 月 |
| 季度边界 | 按财务日历的季度定义，非固定自然季 |
| `to_date` 右端点 | 昨天；假设写入 `assumptions` |
| DST | 在用户时区计算日界后转 UTC，不在 UTC 上做日历运算 |

### 3.4 对比区间迁移

`compiler/time_windows.py` 的 `comparison_range` 逻辑基本正确（`_shift_months` 已有月末 clamp），迁入 `app/temporal/`，改为接收 `ResolvedTimeRange`，并补充 DST 与财务日历支持。

### 3.5 测试

每个 unit × offset × to_date 组合的边界；闰年 2/29；1月31日的 MoM；财年起始非 1 月；周起始日两种配置；DST 转换日；跨时区同一时刻落在不同日期。

## 4. IntentV2 契约

### 4.1 契约变更

新增 `app/intent/v2.py`。旧 `schema.py` 保留一轮做兼容转换（供未迁移的 Golden Set 与 `intent_snapshot` 读取）。

| 变更 | 原因 |
|---|---|
| `time: TimeRange` → `time_expression: TimeExpression` | 模型不再算绝对日期 |
| 移除 `filters[].values` | 该字段是 resolver 阶段产物，不属于模型输出契约 |
| 新增 `domain_candidates[]` | 多数据域路由预留；当前单域填一项 |
| 新增 `ambiguities[]` | 模型主动报告歧义，取代靠低 confidence 反推 |
| `metrics: list[str]` → `list[MetricRef{name, confidence}]` | 支持 per-metric 置信度 |

### 4.2 跨字段 validator（P0-08）

当前 `tests/intent/test_schema.py::test_aggregate_intent_requires_at_least_one_metric` 为红，即本节要修的缺口。

| 规则 | 内容 |
|---|---|
| aggregate | 至少一个 metric |
| trend | 必须有 `time_expression` 且 unit 不为 none |
| ranking | 必须有 sort，且 `sort.limit` ∈ `1..max_top_n` |
| detail | 不允许 metrics |
| unsupported | 其余槽位必须为空 |
| confidence | 所有取值限 `[0,1]` |
| operator 值数量 | `eq/ne/gt/gte/lt/lte` 恰 1；`in/not_in` ≥1；`between` 恰 2 |

### 4.3 保留现有验证层

`recognizer.py` 已有的能力保留不动：禁 SQL 片段检测、`extra="forbid"`、已知引用校验、以及区分"幻觉指标"与"存在但无权限指标"（后者按权限拒答处理）。这套设计是对的。

## 5. LLM Provider 抽象

### 5.1 新增 `app/llm/`

- `provider.py`：`IntentModel` 接口。业务层不接触 OpenAI SDK 对象。`OpenAiCompatClient` 移入并实现该接口。
- `structured.py`：从 Pydantic 生成 JSON Schema，以 Structured Outputs `strict` 模式调用。
- `resilience.py`：超时、指数退避 + 抖动重试、熔断。fallback 模型必须通过同一 Schema，不降低安全契约。

### 5.2 五类调用失败

各自映射独立 `error_code`，不合并为"模型失败"：

| 失败 | 处置 |
|---|---|
| refusal | 按拒答返回，记录 refusal 原因分类 |
| incomplete / max tokens | 重试一次并降低输出体积；仍失败则失败留痕 |
| provider error（429/5xx） | 指数退避 + 抖动重试；熔断后走 fallback |
| schema mismatch | 不重试原样请求，记录原始输出到 admin 级 trace |
| timeout | 按 provider error 处理，但单独计量 |

### 5.3 模型配置与记录

生产使用固定 snapshot，不用 alias（alias 仅用于 shadow / eval）。Trace 记录 response id、model snapshot、usage、latency、prompt version。设置 `store=false`；`safety_identifier` 传 principal id 的 HMAC（稳定且不可反推）。

### 5.4 Prompt 加固

语义元数据（数据集描述、字段同义词）是管理面可写入的内容，构成 prompt injection 面。Prompt 中以明确边界包裹这些内容，并声明其为不可信数据而非指令。

同时删除 prompt 中的绝对日期结构，改为 `time_expression`。

### 5.5 测试

7 类跨字段校验各自用例；5 类 provider 失败映射；refusal 处理；字段描述中注入"忽略以上指令"不改变输出结构；fallback 输出仍过 schema。

## 6. 可执行 Scope Policy（P1-07）

### 6.1 问题

`forbidden_scenario` 目前只是拼进 prompt 的一句话，模型可无视。管理面写"禁止查询个人薪酬明细"没有任何代码强制。

### 6.2 元数据表 `scope_policies`

```
dataset_name / policy_kind / subject_pattern / decision
required_filters[] / max_time_span_days
```

`policy_kind`：`topic_allow`、`topic_deny`、`metric_deny`、`required_filter`（某数据集必须携带某过滤）、`max_span`。

### 6.3 裁决流程

模型输出 `domain_candidates` 与 `ambiguities`；确定性 `PolicyResolver` 按策略表判定 allow / deny / require_clarification。

拒绝理由使用预写安全文案，不回显策略内容——否则策略本身可被逐次探测还原。

## 7. kind 可执行契约（P1-01）

### 7.1 现状

`compiler/query.py` 对四种 kind 全部生成聚合 SQL。`trend` 不做时间分桶，`detail` 不返明细行：用户问趋势，拿到一个总数。

### 7.2 各 kind 契约

| kind | 编译契约 | 本轮 |
|---|---|---|
| `aggregate` | 已实现 | 保持 |
| `ranking` | sort + limit，已实现 | 保持，补 limit 上界校验 |
| `trend` | 按 grain 做 time bucket，按桶 group / order；无 dimension 时单序列，有 dimension 时多序列 | 实现 |
| `detail` | 允许列白名单（`is_detail_visible`）、默认排序（时间倒序）、强制分页 | 实现 |
| `unsupported` | 明确拒答 | 保持 |

### 7.3 `detail` 的列安全

字段新增 `is_detail_visible` 标记，默认 false，须显式开启。MASK 列在明细中同样掩码。现有字段需逐一标注，属本轮配置工作。

### 7.4 不兼容形状明确拒绝

trend 的 grain 与指标 time basis 不兼容时（如日粒度指标问小时趋势）返回明确 `error_code`，不静默降级为可执行但错误的查询。

### 7.5 测试

trend 生成 SQL 含 `date_trunc` 且按桶排序；trend 带维度时产出多序列；detail 只返白名单列；detail 强制分页；MASK 列在 detail 中被掩码；scope policy 命中 `topic_deny` 时拒绝；`required_filter` 缺失触发澄清；拒绝文案不含策略内容。

## 8. 字面量类型（P1-03）

`compiler/predicates.py` 目前按字符串外观猜类型（`float()` 试探），导致 ID、前导零、Decimal、日期比较出错。

`FieldDef` 增加 `logical_type`（`string` / `integer` / `decimal` / `date` / `timestamp` / `boolean` / `enum`）与 `physical_type`。AST literal 按字段声明类型构造，不做外观猜测。类型不匹配时拒绝而非强转。

测试：前导零 ID 不被转为数字；Decimal 保持精度；日期字符串按日期比较；类型不匹配被拒绝。

## 9. 验收标准

1. 时间边界集全部通过：月末、闰年、跨年、季度、DST、财务日历、周起始日。
2. `clock.now()` 返回 tz-aware UTC；无模块级可变时钟；测试通过注入 `FixedClock` 实现确定性。
3. P0-08 的 7 类跨字段校验全部生效，`test_aggregate_intent_requires_at_least_one_metric` 转绿。
4. `trend` 产出真正的分桶时间序列；`detail` 产出分页明细且只含白名单列。
5. `scope_policies` 中的禁区由代码强制，模型无法绕过；拒绝文案不泄漏策略。
6. 五类 provider 失败各自可观测、各有 `error_code`。
7. 字面量按字段声明类型构造，无 `float()` 外观猜测残留。

## 10. 风险

| 风险 | 处置 |
|---|---|
| `metrics` 契约变更影响面广 | 单独一次机械迁移；旧 `schema.py` 保留兼容层，`intent_snapshot` 读取不中断 |
| `is_detail_visible` 默认 false 会让 detail 初期无可见列 | 迁移脚本按现有 `is_groupable` + 非敏感字段给出建议初值，仍需人工确认 |
| 移除全局 `freeze()` 影响所有现有时间相关测试 | 与 `frozen_clock` fixture 一并改造，作为独立提交 |
| Structured Outputs 的 strict 模式对嵌套 schema 有限制 | `TimeExpression` 与 `MetricRef` 保持扁平；先以真实 provider 验证 schema 可被接受 |
| trend/detail 实现引入新 SQL 形状，安全面扩大 | 新形状同样过 S1 的函数白名单与 AST 守卫；detail 额外加列白名单断言 |
