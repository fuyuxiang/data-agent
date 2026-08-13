# S3 CanonicalQueryPlan 与编译器正确性 设计文档

> 上游依据：`fangan.md` 第 5.1（P0-04）、5.2（P1-02/P1-04/P1-09/P1-10/P1-11/P1-12）、6.2（契约升级）、6.5（Trusted Asset）节。
> 子项目定位：升级路线 S3，依赖 S1（权限编译入口、只读执行）与 S2（TimeResolver、IntentV2、typed literal）。
> 覆盖问题：P0-04、P1-02、P1-04、P1-09、P1-10、P1-11、P1-12。

## 1. 目标与非目标

### 目标

建立一个稳定、可缓存、可回放、不含权限的语义计划契约，并修复编译与执行层的正确性缺陷：

- Canonical / Secured 双层计划契约，权限绝不进入可复用层。
- 多指标时间口径不再静默错算。
- 比较查询在 FULL OUTER 下维度值正确。
- 成本护栏读真实扫描量，而非根节点输出行数。
- 截断判定与重试边界准确。
- Decimal 被数字逻辑正确识别。
- Verified Query 从 Canonical Plan 重编译，列权限旁路（P0-04）从根消除。

### 非目标

| 项 | 去向 |
|---|---|
| 指标表达式 DAG、Predicate DSL、别名冲突 lint（P1-05/06/08） | S4 |
| 多表 Join 与 Join Graph（P2-01） | S4 |
| semantic revision 状态机（P2-02） | S4。本轮只消费 `semantic_revision_id` |
| 结果缓存与预聚合（P2-05） | S6。本轮只定义缓存键构成 |
| Golden Set 的 plan diff 断言框架 | S5 |

## 2. 核心设计决策

### 2.1 双层计划：语义可复用，权限不可共享

当前缺少一个稳定的中间契约：`intent_snapshot` 太靠前（枚举与时间未解析），`sql` 太靠后（已含权限改写）。两者都不能作为缓存键、trusted asset 存储单元或回放基准。

### 2.2 `required_field_lineage` 由计划显式声明

权限编译不再遍历 AST 猜测涉及哪些列，而是对计划自己声明的物理列清单做检查。这既是 P0-04 的修复基础，也让复合指标穿透变得可验证。

### 2.3 多指标 time basis 不一致时默认拒绝并澄清

不一致的时间口径静默错算，比多一轮交互的代价大得多。仅在用户显式要求分别统计时才走分别聚合合并。

### 2.4 Trusted Asset 存 `TimeExpression`，不存已解析区间

同一个"本月销售额"的 VQ 在不同日期必须得到不同但都正确的区间。存已解析的绝对区间等于把过期答案固化。

### 2.5 SQLSTATE 而非错误字符串分类

错误消息随 PostgreSQL 版本与 locale 变化，字符串匹配的分类会静默失效。

## 3. 计划契约

### 3.1 新增 `app/planning/`

```
CanonicalQueryPlan              # 语义层面的确定性计划，可跨用户复用
  plan_version
  semantic_revision_id
  domain / dataset
  measures[]                    # metric name + version + time_basis
  group_by[]
  typed_filters[]               # 已解析枚举、已定类型的字面量
  resolved_time_range           # S2 TimeResolver 产物
  comparison / sort / pagination
  required_field_lineage[]      # 该计划最终读取的物理列
  assumptions[] / clarification_evidence[]

SecuredExecutionPlan = CanonicalQueryPlan
  + principal_id / tenant_id / policy_revision / policy_hash
  + row_predicates[] / column_decisions[]
  + dialect / warehouse_profile / execution_budget
```

Canonical Plan 可缓存、可作为 trusted asset 存储单元、可跨用户比较。Secured Plan 绝不共享；结果缓存键必须包含 `policy_hash`，同一问题的不同用户不得命中彼此的结果。

### 3.2 流水线重构

```
intent → resolve → CanonicalQueryPlan → policy compile
       → SecuredExecutionPlan → dialect compile → AST guard → execute
```

现有 `compile_intent()` 拆为 `build_plan()`（纯语义，无 IO）与 `compile_plan()`（方言 SQL 生成）；`secure_compiled()` 变为 `compile_policy()`（产出 SecuredExecutionPlan）。

触及范围：`app/compiler/`、`app/security/pipeline.py`、`app/pipeline/orchestrator.py` 及大部分编译器测试。

### 3.3 计划哈希与确定性

Canonical Plan 有稳定序列化（字段排序、版本化）。同一 plan 在同一 semantic revision 上必须产出**字节相同**的 SQL——这是可回放性与缓存正确性的前提，需专门测试。

## 4. 多指标时间口径（P1-02）

### 4.1 问题

`_build_select` 使用 `build_time_predicate(dataset, metrics[0].time_field, window)`：问"本月的订单金额和退款金额"时，若订单按 `order_date`、退款按 `refund_date`，两者被同一个 `order_date` 区间过滤，退款数据静默错算。

### 4.2 处置

编译前校验：`measures[]` 内所有指标的 `time_basis` 必须一致。

| 情况 | 行为 |
|---|---|
| 不一致（默认） | 拒绝并触发澄清，说明各指标的时间字段差异 |
| 用户显式要求分别统计 | 各指标独立聚合为子查询，按 `group_by` 维度 FULL OUTER 合并 |

### 4.3 引证逐指标化

`Citation` 从"单指标引证"改为 `tuple[MetricCitation, ...]`，每个指标独立展示时间字段与版本。现有 `citation.time_field_business_name` 单字段结构不足以表达多指标。

## 5. 比较查询正确性（P1-04）

### 5.1 问题

`_build_comparison` 的维度投影为 `exp.column(name, table=_CURRENT_CTE)`。FULL OUTER JOIN 下，仅存在于基期的维度值会使维度列为 NULL，用户看到"省份=空，当期=空，基期=100"。

### 5.2 处置

- 维度投影改为 `COALESCE(current.dim, baseline.dim)`。
- 增加显式状态列：仅当期 / 仅基期 / 两期都有。
- 结果校验的 all-null 检测只针对指标列。维度列出现 NULL 不再被判为异常。

## 6. Freshness 受权限约束（P1-09）

### 6.1 问题

freshness 目前以字符串拼接 `MAX(...)` 直接查询，不过 AST 守卫、不带 RLS。只能看华东的用户，引证中可能显示全国最新数据日期。且取的是第一个指标的时间字段。

### 6.2 处置

优先方案：freshness 作为数据契约（摄取水位）存入元数据，由数据管道写入。

过渡方案：生成正常的 Canonical Plan（同数据集、同权限、`MAX(time_field)`），走完整流水线。

两方案均按**当前指标**的 time basis 取值，而非第一个指标。

## 7. Decimal 支持（P1-10）

数字格式化、同比计算、量级校验目前用 `isinstance(x, (int, float))` 判断。PostgreSQL NUMERIC 返回 `Decimal`，全部漏判。

处置：改用 `numbers.Number`；同比等运算使用 Decimal 安全算法，不转 float；结果 schema 保留精度与单位；财务值禁止浮点直接比较。

## 8. 成本护栏递归化（P1-11）

### 8.1 问题

`estimate_cost` 只读 `plan[0]["Plan"]["Plan Rows"]`，即**根节点输出行数**。`SELECT SUM(x) FROM huge_table` 全表扫描 1 亿行、根节点输出 1 行，护栏判 PASS 放行——护栏在最需要它的场景下完全失效。

### 8.2 处置

递归遍历 EXPLAIN 计划树，采集每个节点的 `Plan Rows`、`Total Cost`、`Relation Name`、`Node Type`、`Plan Width`。判定依据：

| 信号 | 用途 |
|---|---|
| `max(所有节点 Plan Rows)` | 扫描量估计，取代根节点输出行数 |
| 根节点 `Total Cost` | 总成本 |
| 大表 `Seq Scan` 存在性 | 全表扫描告警 |
| sort/hash 的 `Plan Width × Plan Rows` | 内存压力估计 |
| join 节点数与类型 | 复杂度与 fanout 风险 |

阈值按 `warehouse_profile` 配置，不同仓库的成本量纲不可比，现有全局 `cost_reject_rows` 不足。同时记录估计值，供 S5/S6 回看估计误差。

## 9. 截断与重试（P1-12）

### 9.1 三个缺陷

| 缺陷 | 现状 |
|---|---|
| 截断误报 | `truncated = len(rows) >= row_limit`，结果恰好等于 limit 行时误报 |
| 同连接重试 | 传入 `connection` 时超时后用同一连接重试；PostgreSQL 超时后事务处于 aborted 状态，重试必然失败 |
| 字符串分类 | `_classify` 匹配 "timeout"、"canceling statement" 等消息文本，locale 或版本差异使分类失效 |

### 9.2 处置

- SQL 请求 `limit + 1` 行，客户端裁去多余一行并据此判断截断。
- 重试强制使用新连接，或先显式 rollback。
- 分类改用 SQLSTATE：`57014` 查询取消、`08xxx` 连接类、`53xxx` 资源不足、`40001` 序列化失败。
- 重试采用指数退避 + 抖动。

## 10. Verified Query 从 Plan 重编译（P0-04）

### 10.1 背景

S1 已关闭 VQ 直接执行 `fixed_sql` 的路径（列权限旁路）。本节恢复该能力，并从根消除旁路。

### 10.2 存储迁移

`verified_queries` 迁移为存 `canonical_plan`（JSONB）+ `semantic_revision_id`，不再存 `fixed_sql`。时间部分存 `TimeExpression` 而非已解析区间。

### 10.3 运行时流程

1. 精确文本命中只作为**高精度候选**，不是直接答案。
2. 相对时间问题按当前 anchor 重新解析：plan 中的时间部分以当前时钟重算区间。
3. 校验 `semantic_revision_id` 仍为当前版本，不一致则不复用。
4. 从 plan 重走 `compile_policy()` → 权限编译 → 方言编译，与普通查询同一入口。
5. 响应标记所用 trusted asset 及其验证时间。

列权限旁路由此消失：VQ 与普通查询共用同一权限编译入口，`required_field_lineage` 同样受检。

## 11. 测试

| 领域 | 用例 |
|---|---|
| 计划确定性 | 同一 plan 同一 revision 产出字节相同的 SQL |
| 多指标 | 异 time basis 触发澄清；显式分别统计时 SQL 含两个子查询；逐指标引证 |
| 比较查询 | 仅存在于基期的维度值正确显示；状态列正确；all-null 只看指标列 |
| Freshness | 受 RLS 约束；按当前指标 time basis |
| Decimal | 同比与格式化正确；财务值不走浮点比较 |
| 成本护栏 | 大表扫描小聚合被 REJECT（真实 EXPLAIN）；warehouse profile 阈值生效 |
| 截断重试 | 恰好 limit 行不误报；limit+1 裁剪正确；超时后用新连接重试；各 SQLSTATE 分类 |
| VQ | 命中被 DENY 列时拒绝；相对时间按当前 anchor 重算；revision 变更后不复用 |

## 12. 验收标准

1. 时间边界集、指标 DAG 前置校验、类型过滤、多指标口径集全部通过。
2. 相同 Canonical Plan 在同一 semantic revision 上生成稳定 AST（字节一致）。
3. Trusted Asset 不再保存可绕过当前语义与权限的物理 SQL。
4. 成本护栏对"大扫描小聚合"正确拒绝，不再误 PASS。
5. 截断判定精确；超时重试不复用 aborted 连接。
6. 引证逐指标展示时间字段与版本。
7. VQ 恢复可用，且与普通查询共用同一权限编译入口。

## 13. 风险

| 风险 | 处置 |
|---|---|
| 三段式流水线重构面广 | 分两步提交：先引入 plan 契约并让现有路径产出它，再切换执行路径 |
| `verified_queries` 数据迁移丢失历史 VQ | 旧 `fixed_sql` 只读归档保留，不参与运行时；无法转换为 plan 的条目标记为 inactive 并记录原因 |
| EXPLAIN 递归判定可能引入误拒 | 阈值按 warehouse profile 可配；先以 warn 观察一轮估计误差再收紧 reject |
| Citation 结构变更影响答案模板与前端 | `MetricCitation` 变更同步 S7 前端；本轮保留单指标场景的兼容渲染 |
| 计划序列化稳定性依赖字段顺序 | 序列化显式排序并版本化；以哈希一致性测试锁住 |
