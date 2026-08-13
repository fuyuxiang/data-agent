# S4 语义层规模化与可信资产 设计文档

> 上游依据：`fangan.md` 第 5.2（P1-05/P1-06/P1-08）、5.3（P2-01/P2-02/P2-03/P2-04）、6.4（语义层建议）、6.5（Trusted Asset）节。
> 子项目定位：升级路线 S4，依赖 S1（角色与鉴权）、S2（typed literal）、S3（Canonical Plan 与 `required_field_lineage`）。
> 覆盖问题：P1-05、P1-06、P1-08、P2-01、P2-02、P2-03、P2-04。

## 1. 目标与非目标

### 目标

把语义层从"单张宽表 + 一个布尔发布位"升级为可治理、可版本化、可扩展到多表的注册中心：

- 指标依赖用单一 DAG 表达，权限与编译共用同一份 lineage。
- `fixed_filter` 从自由 SQL 变为结构化 Predicate DSL。
- 枚举别名冲突在发布期被拦，运行时歧义必须澄清。
- 语义模型有不可变 revision 与完整状态机，支持 diff、审批、回滚。
- 发布门禁校验真实物理契约。
- 支持已审核的静态 Join Graph，fanout 不产出放大数字。
- Trusted Asset 具备参数化、过期、失效追踪与结构化召回。

### 非目标

| 项 | 去向 |
|---|---|
| 多跳（>2）Join、桥表、多对多完整支持 | 后续子项目 |
| AI 辅助语义初始化 | 不在本轮路线 |
| drift scan 的调度实现 | S6（本轮定义检查逻辑与告警语义） |
| 结果缓存与预聚合物化 | S6 |
| 评测框架对 trusted asset 的隔离执行 | S5（本轮只提供 `eval_excluded` 标记） |
| 语义管理前端界面 | S7 |

## 2. 核心设计决策

### 2.1 lineage 只有一个实现

当前存在两套不一致的表达式解析：`compiler/metrics.py` 用 SQLGlot AST 找依赖，`security/columns.py` 的 `_metric_field_names` 用 `expression.replace("(", " ").split()` 字符串分词。**权限判断走字符串那套，编译走 AST 那套**——两者漂移即权限漏判。这是 P1-05 的实际形态。

### 2.2 revision 完全不可变

修改语义等于新建 draft，存整个数据集定义的快照而非增量。历史 Turn 的 `semantic_revision_id` 必须永远能解析出当时的定义，否则回放无意义。

### 2.3 LLM 不拼 Join

Join 路径只从预定义、已审核的 `relations` 中选取。多条路径且无默认路径时拒绝，要求语义管理员显式定义。

### 2.4 fanout 绝不静默放大

`one_to_many` join 后对左表指标求和会重复计数。检测到 grain 被放大时，优先用预聚合子查询；无法预聚合则拒绝。不存在"产出放大数字"这个选项。

### 2.5 drift scan 只告警不阻断

上游一次 DDL 不应打挂整个问数服务。发布门禁阻断，线上巡检只告警。

### 2.6 Trusted Asset 必须过期

"半年前验证过的答案"不构成可信。`expires_at` 到期即停止召回，需重新验证。

## 3. 指标 DAG 与统一 lineage（P1-05）

### 3.1 单一解析入口

新增 `app/semantic/lineage.py`：

```
def metric_dag(dataset) -> MetricDag                      # 全数据集指标依赖图
def field_lineage(dataset, metric_name) -> frozenset[str] # 穿透到物理列
```

`security/columns.py` 的 `_metric_field_names` 删除，改调 `field_lineage`。编译器的 `resolve_metric_dependencies` 同样消费该 DAG。同一份 lineage 同时服务权限与编译。

`MetricDag` 在发布时构建并缓存于 semantic revision，运行时不重复解析。

### 3.2 发布门禁新增检测

| 检查 | 说明 |
|---|---|
| 循环依赖 | 现有 `resolve_metric_dependencies` 只排除自引用，间接循环（A→B→A）会无限递归 |
| 单位运算合法性 | 金额 ÷ 金额 = 无单位比率；金额 + 数量 = 错误 |
| 可加性 | `recalculate` 指标不得被 SUM 包裹。现有检查只覆盖直接聚合，未覆盖复合指标内的嵌套 |
| 时间字段 | 存在且可用 |
| 引用范围 | 表达式只引用同数据集内已知指标/字段 |

## 4. Predicate DSL（P1-06）

### 4.1 问题

`fixed_filter: str` 是自由 SQL 文本，经 `sqlglot.parse_one` 直接塞入 `exp.Filter`：方言绑定、可写任意函数、且 filter 中引用的列不进入任何 lineage 检查——列权限完全绕过。

### 4.2 处置

改为结构化 `Predicate` DSL：字段 + 操作符 + 类型化值 + and/or 组合。禁子查询，禁非白名单函数（复用 S1 的函数白名单）。

DSL 中的字段自动进入 `required_field_lineage`，因此受列权限检查。

### 4.3 迁移

旧文本做只读迁移：发布前解析并规范化为 DSL；无法转换的标 ERROR 阻止发布。

DSL 表达力弱于自由 SQL（如复杂 CASE 无法表达），这是有意的取舍——当前形态是一个完整的列权限旁路。

## 5. 枚举别名冲突（P1-08）

### 5.1 问题

`resolve_enum` 遍历时第一个匹配即返回。两个枚举值持有相同别名时静默选择前者，可能映射到错误实体。

### 5.2 处置

- 发布 lint 检测归一化（NFKC + casefold + 去空白）后的别名冲突，冲突为 ERROR。
- `resolve_enum` 改为返回候选列表：0 个 → 澄清；1 个 → 使用；N 个 → 必须澄清。
- 大字典场景引入受权限约束的检索索引，不把全部枚举值塞入 prompt。

## 6. Semantic Revision 状态机（P2-02）

### 6.1 问题

`DatasetRow.is_published` 是一个布尔，发布即 `row.is_published = True`。无版本、无历史、无法回滚、无法 diff、无审批人记录。而 S3 的 Canonical Plan 已引用 `semantic_revision_id`——该实体必须真实存在。

### 6.2 状态机

`semantic_revisions` 表存整个数据集定义的快照（JSONB）：

```
draft → linted → approved → published → retired
```

| 状态 | 含义 |
|---|---|
| `draft` | 可编辑，不可用于问答 |
| `linted` | 通过语义体检，记录 lint 报告 |
| `approved` | 由 `semantic_approver` 审批，记录审批人与时间 |
| `published` | 当前生效版本，同一 dataset 只能有一个 |
| `retired` | 被新版本取代，保留用于回放历史 Turn |

修改语义 = 从当前 published 复制出 draft → 走流程 → 发布时原 published 转 retired。

`DatasetRow` 保留为当前指针，增加 `published_revision_id`。运行时语义模型按 revision 加载，而非按 `DatasetRow` 拼装。

### 6.3 diff 与兼容性

发布前对比新旧 revision，分类变更：

| 类别 | 变更 | 处置 |
|---|---|---|
| 兼容 | 新增字段/指标、补充同义词 | 可直接发布 |
| 破坏性 | 删除指标、改指标口径、改 time basis、改物理列映射 | 需额外确认，并列出受影响的 trusted assets 与 Golden Case |

破坏性变更是重点：改了指标口径而不重跑 VQ 验证，等于让已验证答案静默变错。

### 6.4 回滚

把某个 retired revision 重新置为 published：新建一条指向它的发布记录，不修改历史行。目标 15 分钟内完成（对应 `fangan.md` 9.2 的 SLO）。

## 7. 物理契约校验（P2-03）

### 7.1 发布门禁

连接 `information_schema` 校验：表存在、列存在、类型匹配、当前 DB role 具备 SELECT 权限。

### 7.2 Drift scan

独立巡检任务（调度在 S6）。漂移作为告警，不阻断线上查询。

### 7.3 数据健康

进入 revision：freshness（配合 S3 的摄取水位）、唯一性、非空率、基数。

### 7.4 完整发布门禁清单

物理表/列与类型、指标 DAG 循环、单位运算、聚合合法性、时间字段、枚举冲突、join fanout、策略引用、**VQ 重编译**、Golden smoke set、兼容性 diff。

其中 VQ 重编译：发布前用新 revision 重编译所有关联 trusted asset，编译失败或 lineage 变化的必须处理，否则阻止发布。

## 8. 关系与 Join Graph（P2-01）

### 8.1 `relations` 表

```
left_dataset / right_dataset
join_keys[]              # 左右列对
cardinality              # one_to_one | one_to_many | many_to_one | many_to_many
optionality              # inner | left
fanout_risk              # none | measure_duplication
allowed_directions[]     # 仅已审核方向
is_default_path          # 同一对实体多条路径时的默认选择
```

### 8.2 路径选择

编译器按 `measures` 与 `group_by` 涉及的数据集，在静态 Join Graph 上寻找唯一路径。多条路径且无默认路径时拒绝，要求语义管理员显式定义——不猜。

本轮范围：已审核的静态 Join Graph，最多两跳。

### 8.3 fanout 处置

指标声明所属 grain。编译时检测 join 后 grain 是否被放大：

1. 可预聚合：先在原 grain 聚合为子查询，再 join。
2. 不可预聚合：拒绝。

`required_field_lineage` 扩展为带数据集限定的列（`dataset.column`），权限检查覆盖 join key——join key 本身也是列，也可能敏感。

## 9. Trusted Assets 治理（P2-04）

### 9.1 完整字段

```
id / name / domain
trigger_questions[]
canonical_plan            # S3 已迁移
parameter_schema
semantic_revision_id
verified_by / verified_at / review_status
last_validated_at / last_validation_result
expires_at / is_active
usage_count / failure_count
eval_excluded
```

### 9.2 新增能力

| 能力 | 说明 |
|---|---|
| 参数化 | "华东本月销售额"与"华南上月销售额"共用一个 asset + 参数，而非两条记录 |
| 过期 | `expires_at` 到期后停止召回，需重新验证 |
| 失效追踪 | `failure_count` 累积，连续失败自动置 inactive 并告警 |
| 召回升级 | 精确文本 → canonical signature（结构化匹配）→ 可选 embedding 仅作候选生成；最终必须按结构化兼容性 + semantic revision + 权限过滤；记录 `match_reason` 与 `confidence` |

### 9.3 评测隔离

被测 trusted asset 必须从运行时召回集合中移除，否则等于"拿答案做提示再证明自己答对"。该约束由 S5 评测框架强制执行，本轮在 asset 表提供 `eval_excluded` 标记支持。

## 10. 测试

| 领域 | 用例 |
|---|---|
| 指标 DAG | 间接循环被拒；单位运算错误被拒；recalculate 嵌套 SUM 被拒；权限与编译对同一复合指标得到相同 lineage |
| Predicate DSL | fixed_filter 的列进入 lineage 并受权限检查；旧文本迁移；无法转换的标 ERROR |
| 枚举 | 别名冲突为 ERROR；N 个候选触发澄清 |
| Revision | 不可变（修改已发布 revision 失败）；非法状态转换被拒；同 dataset 仅一个 published；历史 Turn 按旧 revision 回放；破坏性变更被识别；回滚后查询走旧定义 |
| 物理契约 | 列类型不匹配阻止发布；drift scan 告警不阻断查询；VQ 重编译失败阻止发布 |
| Join | 路径唯一时正确编译；多路径无默认时拒绝；one_to_many fanout 被检测；fanout 时预聚合且结果不放大；join key 受列权限约束；两跳 join 正确 |
| Trusted Asset | 参数化 asset 命中不同参数；过期 asset 不召回；连续失败自动 inactive；召回记录 match_reason；eval 模式下被测 asset 不参与召回 |

## 11. 验收标准

1. 权限与编译使用同一 lineage 实现，字符串分词版本已删除。
2. `fixed_filter` 全部迁移为 DSL，其引用列受列权限约束。
3. 枚举别名冲突无法通过发布；运行时多候选必澄清。
4. 语义 revision 不可变，状态机完整，可 diff、可审批、可 15 分钟内回滚。
5. 发布门禁 11 项全部生效，含 VQ 重编译与物理契约校验。
6. 两跳静态 Join 可用，fanout 场景不产出放大数字。
7. Trusted Asset 具备参数化、过期与失效追踪，召回记录匹配理由。

## 12. 风险

| 风险 | 处置 |
|---|---|
| `fixed_filter` DSL 表达力不足，个别配置无法迁移 | 发布期标 ERROR 并人工处理；必要时以派生指标替代复杂 CASE |
| revision 快照存储体积增长 | JSONB 压缩存储；retired revision 按保留期归档，但保留被历史 Turn 引用的版本 |
| 运行时改为按 revision 加载，影响所有语义读取路径 | 保留 `DatasetRow` 指针，加载层接口不变，内部切换来源 |
| Join Graph 引入新 SQL 形状，安全面扩大 | 新形状同样过 S1 函数白名单与 AST 守卫；join key 纳入 lineage 权限检查 |
| fanout 检测漏判导致数字放大 | grain 声明为必填；无法判定 grain 时按拒绝处理，不按放行 |
| 发布门禁变重，语义迭代变慢 | Golden smoke set 控制在秒级；重型检查（drift、全量 VQ 重编译）可异步但发布前必须完成 |
