# S6 可观测性、缓存与异步 设计文档

> 上游依据：`fangan.md` 第 9 章（9.1 Trace 模型升级、9.2 SLO、9.3 缓存与成本）、5.3（P2-05/P2-06/P2-07）、5.2（P1-09 结果集与分页）。
> 子项目定位：升级路线 S6，依赖 S1（Trace 分级与列拆分、事务边界）、S2（Clock 依赖注入、prompt 版本号）、S3（CanonicalQueryPlan 与 plan hash）、S4（semantic revision、drift scan 检查逻辑）、S5（Eval Run 归档结构）。
> 与 S5 的依赖方向：单向。S5 不依赖本轮任何产出即可完整交付（Eval Run 先落文件归档），本轮承接其迁库与调度。
> 覆盖问题：P2-05、P2-06、P2-07，以及 S4/S5 委派到本轮的调度与落库项。

## 1. 目标与非目标

### 目标

当前项目零缓存（仅 `functools.lru_cache` 装饰配置读取）、零异步任务、零 OpenTelemetry；`TraceStageRow` 只有 stage/payload/token/elapsed 七个字段，`fangan.md` §9.1 要求的标识几乎全缺。本子项目建立生产可运维的观测与承载能力：

- Trace 模型补齐全部标识与度量，业务 Trace 与 OpenTelemetry 双写。
- 建立 SLO 与对应指标，可用性、延迟、权限漏施加、Trace 完整率可测。
- 三层缓存（语义 revision、权限可见视图、结果），键必含 `policy_hash` 与 source watermark。
- Job/Run 状态机承载长任务，支持取消、超时、预算、部分进度。
- 承接 S4 的 drift scan 调度、S5 的 Eval Run 落库、Trace 留存清理。

### 非目标

| 项 | 去向 |
|---|---|
| LangGraph / Temporal 等编排框架接入 | 不在本轮。先用自建状态机，`fangan.md` P2-06 也主张"到这一步再选" |
| 多步深度分析的业务逻辑 | 本轮只建承载框架，具体分析能力后续 |
| 预聚合物化视图的物理建设 | 本轮定义识别与选择逻辑，物理建设需业务侧决策 |
| 观测平台的部署与看板搭建 | 本轮只保证埋点与指标导出正确 |
| API v2 与 SSE 进度推送 | S7（本轮提供 Job 状态查询的服务层能力） |
| 成本计费与租户账单 | 本轮只做成本可观测与预算护栏，不做计费 |

## 2. 核心设计决策

### 2.1 OpenTelemetry 不替代业务 Trace

业务回放需要稳定、版本化、可查询的结构，OTel 的采样与保留策略不满足这个需求。两者并行：Trace 落库供产品回看与评测定位，OTel span 供运维告警与聚合分析。共享同一个 `trace_id` 以便互跳。

### 2.2 失败阶段必须落库，这个保证不能被破坏

现有 `trace.py` 的 `stage_timer` 在异常路径先 `record` 再 `raise`，失败阶段一定写入。升级 Trace 模型与接入 OTel 时必须保留这个语义——失败恰恰是 Trace 存在的理由。同时注意与 S1 的事务边界配合：Trace 写入不能因主流程回滚而丢失，需独立提交路径。

### 2.3 缓存键错一次等于越权

结果缓存若不含 `policy_hash`，A 用户的结果会被返给 B 用户。因此缓存键构造是安全边界而非性能优化：键缺任一必要成分 → 拒绝缓存，不降级为"不带该成分的键"。

### 2.4 数据新鲜度进缓存键，不靠 TTL 赌

TTL 只能限制陈旧程度上界，不能保证正确性。结果缓存键含 source watermark（数据集的 `data_updated_at`），上游数据更新即自然失效。TTL 是兜底而非主手段。

### 2.5 同步路径不动

基础问数保留同步。异步只承接明确的长任务，不做"全部改异步"的重构——那会把 P95 延迟问题变成排队问题，且让 Trace 链路复杂化。

### 2.6 部分进度必须可见

长任务失败时"什么都没有"和"完成了 7 步中的 5 步"对用户价值完全不同。Job 支持 checkpoint 与部分结果，取消与超时时保留已完成部分。

## 3. Trace 模型升级（P2-07）

### 3.1 当前缺口

`TraceStageRow`（`app/observability/orm.py:62-83`）现有字段：`turn_id`、`stage`、`sequence`、`input_payload`、`output_payload`、`model`、`prompt_tokens`、`completion_tokens`、`elapsed_ms`、`error`、`created_at`。

`fangan.md` §9.1 要求的以下内容全部缺失：`request_id` / `trace_id` / `run_id`、`principal_hash` / `tenant_id` / `domain`、`policy_revision` / `prompt_version` / `model_snapshot`、payload schema 版本、cache / reasoning token、retry 次数、plan hash / secured SQL hash / `trusted_asset_id`、EXPLAIN cost 与递归行数、扫描关系、仓库排队与执行时间、`truncated`、validator issues、`error_code` / 取消 / 幂等结果。

### 3.2 字段分组

按归属拆分，避免把所有东西堆进 stage 行：

| 归属 | 字段 |
|---|---|
| Turn 级（`TurnRow` 扩展） | `request_id`、`trace_id`、`run_id`、`principal_hash`、`tenant_id`、`domain`、`semantic_revision`、`policy_revision`、`prompt_version`、`model_snapshot`、`status`、`error_code`、`cancellation`、`idempotency_outcome` |
| Stage 级（`TraceStageRow` 扩展） | `payload_schema_version`、`cache_tokens`、`reasoning_tokens`、`retry_count`、`error_code`、`cache_outcome` |
| 编译/执行专项 | `plan_hash`、`secured_sql_hash`、`trusted_asset_id`、`explain_total_cost`、`explain_max_plan_rows`、`scan_relations`、`warehouse_queue_ms`、`warehouse_exec_ms`、`result_rows`、`truncated`、`validator_issues` |

`principal_hash` 而非 `user_id`：Trace 的运维视角不需要可反查的用户身份，哈希足以做聚合与关联，同时降低泄漏面。产品回看侧的归属判定仍用 S1 的 `user_id` 对象级鉴权。

### 3.3 与 S1 的 payload 分级衔接

S1 已确定 Trace 四级可见性（public / user / sensitive / admin）且 **payload 按级别存入独立列**，不在读取时过滤。本轮新增字段沿用该分级归属：

| 级别 | 归属字段举例 |
|---|---|
| public | stage、sequence、elapsed_ms、status |
| user | 引用要素、假设、警告、result_rows、truncated |
| sensitive | secured SQL、plan 细节、validator issues、scan_relations |
| admin | 完整 LLM 输入输出、principal_hash、policy_revision、EXPLAIN 明细 |

### 3.4 错误分类

`error: str | None` 保留供人读，新增 `error_code` 供机器判定。分类来源：S3 定义的 SQLSTATE 映射（`57014` 超时、`08xxx` 连接、`53xxx` 资源、`40001` 序列化失败）加上应用层类别（LLM 五类失败、权限拒绝、护栏拦截、编译失败）。SLO 与告警只看 `error_code`，不做字符串匹配。

### 3.5 OpenTelemetry 双写

每个 stage 同时产出一个 OTel span，属性取 §3.2 中 public/user 级字段与 `error_code`；sensitive/admin 级内容**不进 OTel**（观测平台的访问控制弱于业务库）。span 层级：turn 为 root span，stage 为子 span，仓库执行为孙 span。

`trace_id` 由入口生成并同时写入业务 Trace 与 OTel，两侧可互跳。

## 4. SLO 与指标

### 4.1 SLO

按 `fangan.md` §9.2：

| SLO | 目标 |
|---|---|
| API 可用性 | 99.9% |
| 基础问数成功链路 P95 / P99 | < 5s / < 10s（不含明确异步任务） |
| 权限策略漏施加 | 0 |
| 元数据写入持久化成功率 | 99.99% |
| Trace 完整率 | > 99.9%（任何 answered turn 必须有 resolve/plan/security/execute/answer） |
| 安全 / 关键 Golden 回归 | 0 |
| 语义发布回滚时间 | < 15 分钟 |

### 4.2 指标

latency（分阶段与端到端）、token（prompt/completion/cache/reasoning）、cost、refusal rate、clarify rate、eval regression（来自 S5 的 Eval Run）、warehouse scan rows 与排队时间、cache hit rate、Trace 完整率、`error_code` 分布。

「权限策略漏施加 = 0」的可测化：每个 answered turn 的 security stage 必须记录已施加的策略清单与 `policy_hash`；缺失即计为违约事件并告警。这让该 SLO 从口号变成可验证约束。

「元数据写入持久化成功率」直接对应 S1 修复的 P0-05（`get_meta_session` 无 commit）——本轮为其加上可观测的确认。

## 5. 缓存（P2-05）

三层，各自的键与失效条件不同：

| 层 | 键 | TTL | 失效条件 |
|---|---|---|---|
| 语义定义 | `semantic_revision_id` | 长 | revision 不可变（S4），故永不脏 |
| 权限可见视图 | `(principal_policy_hash, semantic_revision_id)` | 短 | 策略或 revision 变更 |
| 结果 | `(canonical_plan_hash, semantic_revision_id, policy_hash, source_watermark, dialect)` | 短 | watermark 推进或任一成分变更 |

### 5.1 结果缓存约束

- 键缺任一成分 → **不缓存**，不降级（§2.3）
- 含被脱敏列的结果、`sensitive` 级结果 → 可配置为完全禁用共享缓存
- `truncated=true` 的结果不缓存（分页语义下缓存部分结果会误导）
- 缓存命中必须记入 Trace 的 `cache_outcome`，否则 P95 数据会失真且无法解释

`canonical_plan_hash` 来自 S3 的 CanonicalQueryPlan——绝不按自然语言或 SQL 字符串缓存（`fangan.md` P2-05 明确要求）。

### 5.2 Prompt 缓存

按 provider 官方能力做固定前缀缓存，记录 cache hit / write token。**用户与权限上下文不得放入可共享前缀**——那是跨用户泄漏路径。前缀只含指令与语义 schema 描述。

### 5.3 预聚合

本轮定义识别逻辑：高频 `(metric, grain, dimension)` 组合从 Eval Run 与真实 Trace 统计中识别，产出候选清单与预估收益。物理建设需业务侧决策，不在本轮自动执行。

与 S4 的 fanout 预聚合区分：S4 是**正确性**手段（防重复计数），本节是**性能**手段，两者实现可复用但触发条件无关。

## 6. Job / Run 状态机（P2-06）

### 6.1 状态

```
queued → running → (succeeded | failed | cancelled | timed_out | budget_exceeded)
```

`running` 期间可写 checkpoint。终态均保留已完成的部分进度与已产出的部分结果。

### 6.2 Job 记录

`job_id`、`kind`、`principal`（user_id）、`state`、`created_at`、`started_at`、`finished_at`、`checkpoints[]`、`partial_result_ref`、`budget`（token / 时长 / 扫描行数）、`consumed`、`error_code`、`trace_id`、`cancel_requested_at`。

`trace_id` 与同步链路共享同一套 Trace，异步任务不另建观测体系。

### 6.3 取消与超时

取消是协作式的：置 `cancel_requested_at`，执行侧在 checkpoint 边界检查并退出，同时取消底层仓库查询（对应 SQLSTATE `57014`）。超时与预算超限走同一退出路径，区别只在终态与 `error_code`。

### 6.4 归属与鉴权

Job 的读取、取消一律走 S1 的对象级鉴权：非本人或不存在均 404。Job 承载的查询在提交时固化 `policy_hash`，执行时不重新解析权限——避免长任务期间策略变更导致的判定不一致（要么用提交时策略并记录，要么拒绝，不静默混用）。

本轮只提供服务层能力；HTTP API 与 SSE 进度推送在 S7。

## 7. 调度与留存

三项定时任务，承接上游子项目的委派：

| 任务 | 来源 | 语义 |
|---|---|---|
| drift scan | S4 委派（S4 定义检查逻辑，本轮做调度） | 定期校验语义定义与物理契约；**只告警不阻断**（S4 §2.5） |
| Eval Run 落库 | S5 委派 | 将 `artifacts/eval/<run_id>.json` 迁入 `eval_run` / `eval_case_result` 表，归档 JSON 结构已按可映射为表设计 |
| Trace 留存清理 | 本轮 | 按级别设不同保留期：public/user 长，sensitive/admin 短（含 SQL 与 LLM 原文，泄漏面最大）。清理按 turn 粒度，不留半截 Trace |

Eval / 批处理使用独立 warehouse 或 resource group，不影响在线问数（`fangan.md` §9.3）。

## 8. 成本护栏

同时使用四个维度（`fangan.md` §9.3）：时间范围、估计扫描行数、EXPLAIN Total Cost、并发与租户预算。

与 S3 的护栏关系：S3 的护栏是**单查询**准入（递归 EXPLAIN 分析、最大 Plan Rows），本节增加**跨查询**的并发与预算维度。两者串联，任一拒绝即拒绝。

预算超限的处置优先级：先建议收窄时间范围或加维度过滤（可执行的补救），无法收窄再拒绝。不静默截断结果——那会产出错误结论。

## 9. 测试

- 失败 stage 在异常路径仍落库（保护 §2.2 的既有保证不被升级破坏）
- Trace 写入不因主流程回滚而丢失
- answered turn 缺任一必需 stage 时 Trace 完整率指标计为违约
- security stage 缺 `policy_hash` 时计为「权限漏施加」违约事件
- 结果缓存键缺任一成分时不缓存，而非用残缺键缓存
- 相同 plan、不同 `policy_hash` 的两个 principal 不共享缓存结果（跨用户泄漏回归测试）
- source watermark 推进后缓存自然失效
- `truncated=true` 的结果不进缓存
- Prompt 共享前缀中不含用户或权限上下文
- Job 取消在 checkpoint 边界生效并保留部分结果
- 超时 / 预算超限产出各自 `error_code` 而非同一个
- Job 读取与取消对非本人返回 404
- Job 执行期间策略变更不改变已固化的 `policy_hash`
- sensitive / admin 级内容不出现在 OTel span 属性中
- `error_code` 由 SQLSTATE 映射得出，不依赖字符串匹配
- Trace 清理按 turn 粒度，不产生半截 Trace

## 10. 验收标准

1. Trace 模型补齐 §3.2 全部字段，按 turn / stage / 编译执行专项分组落位。
2. 新增字段纳入 S1 的四级可见性列拆分，级别归属明确。
3. `error_code` 落地，SLO 与告警不再依赖字符串匹配。
4. OTel 双写生效，与业务 Trace 共享 `trace_id`，sensitive/admin 内容不外泄。
5. §4.1 七项 SLO 均有对应可测指标；「权限漏施加」与「Trace 完整率」有明确违约判定。
6. 三层缓存落地，结果缓存键含 `policy_hash` 与 source watermark。
7. 跨用户缓存泄漏有回归测试守住。
8. 预聚合候选识别可产出清单，不自动执行物理建设。
9. Job/Run 状态机落地，支持 checkpoint、取消、超时、预算、部分进度。
10. Job 归属走对象级鉴权，`policy_hash` 提交时固化。
11. drift scan 定时执行且只告警。
12. Eval Run 从文件归档迁入元数据库表。
13. Trace 按级别分期清理，按 turn 粒度。
14. 成本护栏四维度生效，预算超限优先建议收窄。
15. 失败 stage 落库的既有保证未被破坏。
16. 后端测试基线不回退，新增测试全绿。

## 11. 风险

| 风险 | 处置 |
|---|---|
| Trace 字段大幅扩容影响写入性能 | 编译执行专项字段单表存放，避免 stage 行过宽；写入路径异步化留作后续优化项，本轮先测量 |
| Trace 独立提交路径与 S1 事务边界冲突 | 明确 Trace 用独立 session 提交，主流程回滚不影响；加测试守住 |
| OTel 引入依赖与运行时开销 | 可开关配置，默认开启但失败不影响主流程（观测不能拖垮业务） |
| 缓存引入后 bug 更难复现 | 缓存命中必记 `cache_outcome`；提供强制绕过缓存的开关用于排查 |
| 自建 Job 状态机长期可能不够用 | 接受。P2-06 主张先自建再选框架；状态机边界设计为可被外部编排器替换 |
| 预算护栏误杀正常大查询 | 阈值可配置，先观测期只告警不拒绝，取得分布后再收紧 |
| Trace 清理误删仍被引用的数据 | 清理前检查 Trusted Asset 与 Golden case 引用；被引用的 turn 不清理 |
| Eval Run 迁库需要 Alembic 基线（S7） | 本轮迁库任务排在 S7 迁移基线之后，或用本轮自建的迁移脚本；计划中标注依赖 |
