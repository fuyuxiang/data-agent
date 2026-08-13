# S5 评测体系重建 设计文档

> 上游依据：`fangan.md` 第 7 章（7.1 空洞修复、7.2 数据集分层、7.3 评分维度、7.4 CI 门禁）、5.3（P2-08）。
> 子项目定位：升级路线 S5，依赖 S1（PrincipalContext 与对象级鉴权）、S2（依赖注入 Clock、IntentV2、prompt 版本号）、S3（CanonicalQueryPlan）、S4（semantic revision、`eval_excluded`）。
> 覆盖问题：P2-08，以及 `fangan.md` 7.1 列出的全部空洞。
> 与既有文档关系：不废弃 `2026-08-12-golden-set-design.md` 与 `2026-08-12-06-golden-set.md`，本文是其下一阶段演进。

## 1. 目标与非目标

### 目标

当前 Golden Set 能跑、能出 22 个绿灯，但**绿灯不代表正确**：Intent 层断言从未执行，澄清链路从未真正闭环，真实模型的任何差异都被降级为 XFAIL。本子项目把它从"看起来在评测"改造成真正能拦住回归的门禁：

- 修复全部已确认空洞，绿灯与正确性重新挂钩。
- 数据集按**用途**分层，holdout 与运行时上下文物理隔离并有防污染检查。
- 七层独立打分，失败可定位到层。
- CI 分级门禁，第一判据是逐 case 回归而非总分。
- 每次评测产出可复现、可比较的 Eval Run 归档。
- 打通"用户差评 → 候选 case"通道，评测集随真实使用增长。

### 非目标

| 项 | 去向 |
|---|---|
| Eval Run 落元数据库表 | S6（本轮先文件归档，避免被 Alembic 迁移基线阻塞） |
| 结构化澄清的 HTTP API 与前端接入 | S7（本轮只做编排器层能力，见 §4） |
| OpenTelemetry 指标接入评测 | S6 |
| 前端 E2E（Playwright） | S7 |
| 评测结果可视化界面 | 不在本轮路线 |
| 自动生成标准答案 | 不做，holdout 标准答案必须人工确认 |

## 2. 核心设计决策

### 2.1 绿灯必须可信

一个断言字段写死 `None` 比没有这个字段更危险——它让报告看起来完整。本轮所有"占位式通过"一律移除，无法判定的层显式产出 `SKIPPED` 并计入统计，不伪装成 `PASS`。

### 2.2 XFAIL 不作为差异兜底

真实模型有波动，但波动不等于"任何字段都可以错"。XFAIL 收窄为「显式登记的已知问题」，且数量上升即告警。关键槽位（指标、时间、权限决策、status）差异一律 FAIL。

### 2.3 门禁看回归，不看总分

总分能掩盖"修好 3 个、弄坏 3 个"。门禁第一判据是与基线逐 case 比对后 `regressed` 为空；准确率阈值是第二判据。

### 2.4 holdout 污染是不可逆的

一旦 holdout 问题进入提示词示例或 few-shot，该 case 永久失去评估价值且无法察觉。因此需要构建期自动检查，而非依赖人工纪律。

### 2.5 评测集来自真实使用

人工凭空编题覆盖不到真实用户的表达方式。差评 turn 是最有价值的 case 来源，需要工程通道而不是靠人翻日志。

## 3. 已确认空洞与修复（fangan.md 7.1）

八项，均已对当前代码逐条核实。`fangan.md` 列六项，核查时新增两项（第 7、8 项），并修正第 5 项的性质。

### 3.1 空洞清单

| # | 现状（代码位置） | 后果 | 修复 |
|---|---|---|---|
| 1 | `test_golden_set.py:140` `_to_outcome()` 里 `"intent": None` 写死 | Intent 层断言完全没跑，`diff_in` 永远拿到 `None` | `TurnOutcome` 暴露 intent 快照，`_to_outcome` 取真实值 |
| 2 | `runner.py:110` `if case.expect.intent_diff == "xfail"`，默认值 `intent_diff="xfail"`（`loader.py:54`） | 真实模型任何 intent 差异都不算失败 | 改为字段级 tolerance，见 §3.2 |
| 3 | `runner.py:157` followup 用 `question=case.question` 重发原问题；`select_option_index` 只写进 `notes` 字符串（`runner.py:164`） | 澄清回填从未被测过 | 读第一轮 options → 提交选项，见 §4 |
| 4 | `conftest.py:19-25` autouse 全局 `clock.freeze(date(2026,8,12))`；`case.as_of` 只在 `GoldenCase` 里传递，到不了运行时时钟 | 无法测时间边界，`temporal/` 分层不可能建立 | 删除全局冻结，改为逐 case 注入 `FixedClock(case.as_of)` |
| 5 | `conftest.py:36-41` `ephemeral_policy` 是彻底的 no-op（`return None`），注释称"测试工厂已装好策略矩阵" | case YAML 的 `policies:` 字段从未生效，case 级策略能力**不存在** | 从零实现：case 级策略安装 + savepoint 回滚 |
| 6 | `CaseReport` 只有一个 `message: str`（`runner.py:18`） | 失败后无法定位、无法跨 run 比较 | 结构化报告，见 §6 |
| 7 | `test_golden_set.py:234` 只 `assert first.status`，`second_report` 算完丢弃 | 澄清第二轮结论不影响测试结果 | 两轮都断言 |
| 8 | `runner.py:92` 解析出 `user_id` 传给 orchestrator，但 `_orchestrator.run()` 只读 `kwargs["username"]`（`test_golden_set.py:156`） | `user_id_resolver` 结果被丢弃，与 S1「user_id 为唯一权限键」相悖 | 改为按 `user_id` 驱动，`username` 仅用于取 principal |

> 第 5 项与 `fangan.md` 表述的差异：原文写「必须实际创建 case 级策略并在事务中回滚」，暗示已有实现但缺回滚。实际是 no-op，需从零实现。

### 3.2 XFAIL 分级与字段 tolerance

`Expectation.intent_diff: str = "xfail"` 这个一刀切开关移除，改为字段类别驱动：

| 字段类别 | 真实模型差异处置 |
|---|---|
| 指标、时间区间、权限决策、status | **FAIL**，不容忍 |
| 维度、过滤 | 按语义等价类比对，不比字符串 |
| headline 措辞、assumptions 文案 | 只记 diff，不判失败 |
| confidence 数值 | 容差区间内视为通过 |

`XFAIL` 只能由 case 显式声明 `known_issue: <问题编号>` 产生。CI 统计 XFAIL 总数，上升即告警。

## 4. 结构化澄清能力（编排器层）

空洞 3 的修复需要一个「提交选定选项」的入口，当前不存在——`clarify` 只能靠重发问题碰运气。该能力的 HTTP API 属 S7，但评测现在就需要，因此**编排器层能力在 S5 实现**，S7 只做 API 与前端接入。

### 4.1 `patch_slot`

编排器新增：

```
patch_slot(turn_id, target, selected_value) -> TurnOutcome
```

- `turn_id`：待澄清的 turn，必须归属当前 principal（复用 S1 的对象级鉴权，非本人或不存在均 404）
- `target`：被澄清的槽位（metric / dimension / time / filter_value）
- `selected_value`：从第一轮 `options` 中选出的值

语义：取该 turn 的 intent 快照 → 替换指定槽位 → **不重新调用 LLM** → 走确定性解析与编译 → 产出新 turn。这一点很关键：澄清回填是确定性操作，重新问模型会引入新的不确定性，也让澄清测不出东西。

### 4.2 评测侧改造

`run_clarify_followup` 改为：

1. 跑第一轮，断言 `status == CLARIFYING` 与 `clarify_kind`
2. 从第一轮 outcome 读 `options`，按 `select_option_index` 取值（索引越界 → ERROR，不静默跳过）
3. 调 `patch_slot(turn_id, target, selected_value)`
4. 断言第二轮期望
5. **两个报告都参与最终断言**

## 5. 数据集分层

当前 22 个 case 按"问题类型"分目录（`simple/mom/yoy/topn/multidim/clarify/permission/out_of_scope`），这个维度不区分**用途**，导致无法区分「可进提示词的示例」与「必须盲测的评估集」。改为按用途分层：

| 分层 | 用途 | 规模目标 | CI 门禁 | 可用于 few-shot |
|---|---|---|---|---|
| `runtime_examples/` | 提示词示例、开发自测 | 20~30 | 否 | **是** |
| `holdout/` | 真实模型准确率评估 | 60（首批 20 建基线，后续补齐） | 是（阈值门禁） | **严格禁止** |
| `security/` | 权限、脱敏、越权、枚举探测、注入、跨租户 | 30+ | 是（**零容忍**） | 否 |
| `multiturn/` | 澄清回填、上下文继承、指代、会话所有权 | 20+ | 是 | 否 |
| `robustness/` | 错别字、口语、超长、空问题、冲突指令 | 20+ | 是（降级阈值） | 否 |
| `temporal/` | 财年、跨年、闰年、月末、周边界、时区、相对时间 | 20+ | 是 | 否 |

### 5.1 现有 case 迁移

| 现目录 | 去向 |
|---|---|
| `simple/`、`mom/`、`yoy/`、`topn/`、`multidim/` | `runtime_examples/` |
| `clarify/` | `multiturn/` |
| `permission/` | `security/` |
| `out_of_scope/` | `robustness/` |

`holdout/` 与 `temporal/` 从零建。迁移只移动文件与补元数据，不改断言内容，避免"迁移顺手改期望"掩盖问题。

### 5.2 防污染检查

构建期检查：任一 `holdout/` case 的问题文本出现在提示词模板、few-shot 示例或 `runtime_examples/` 中，则**构建失败**。比对用归一化文本（去空格与标点）以防轻微改写绕过。这是 §2.4 的落地手段。

### 5.3 case 元数据

每个 case 新增：`layer`、`tags[]`、`eval_excluded`（与 S4 Trusted Asset 同名字段语义一致）、`owner`、`created_at`、可选 `known_issue`。`layer` 与所在目录不一致时加载期报错。

## 6. 七层打分

当前 `CaseReport.status` 是二值的，失败时看不出是模型理解错、编译错还是数据错。改为逐层独立判定：

| 层 | 判定对象 | 判定方式 |
|---|---|---|
| L1 Intent | IntentV2 槽位 | 指标/时间/维度/过滤逐槽比对，按等价类 |
| L2 Resolve | 语义解析结果 | 指标解析、口径版本、时间锚点落值、假设与澄清 |
| L3 Plan | CanonicalQueryPlan | 结构等价（非字符串），含 `required_field_lineage` |
| L4 SQL | 生成的 SQL | AST 等价 + 护栏结论（是否拦、是否限流、方言） |
| L5 Result | 结果集 | 行数、首行、关键单元格（Decimal 容差）、排序、当前/基期 |
| L6 Answer | 自然语言回答 | 引用三要素完整（指标/时间/过滤）、无泄漏、单位、警告 |
| L7 非功能 | 延迟、token、重试次数、扫描行数 | 阈值区间 |

每层独立产出 `pass/fail/skipped` + diff。**总分只用于趋势观测，不用于门禁**——打分的目的是定位失败层，不是算一个漂亮数字。

拒答与澄清的 precision / recall 分开统计（`fangan.md` 7.4），不混入总准确率。

## 7. Eval Run 与可复现

### 7.1 归档结构

一次评测跑 = 一个 `artifacts/eval/<run_id>.json`：

- run 级：`run_id`、`started_at`、`mode`、`layers[]`、`model`、`prompt_version`、`semantic_revision`、`git_commit`、按层聚合的 `totals`、`baseline_run_id`
- case 级：`case_id`、`layer`、`per_layer`(L1~L7 判定与 diff)、`duration_ms`、`tokens`、`trace_id`、`status`、`known_issue`

`trace_id` 让失败 case 能直接跳到产品 Trace 看完整执行链，这是当前调试最缺的一环。

S6 将其迁入元数据库表（`eval_run` / `eval_case_result`），归档 JSON 结构按可直接映射为表来设计。

### 7.2 可复现三要素

`model` + `prompt_version` + `semantic_revision` 必须写入 run 记录，否则跨 run 比对无意义：

- `semantic_revision`：来自 S4 的不可变 revision（`published` 版本号）
- `prompt_version`：S2 给提示词模板加的显式版本号
- `model`：真实模式下的模型标识；stub 模式记 `stub`

三要素任一缺失 → 拒绝生成 run 记录，不产出无法解释的数据。

### 7.3 回归检测

新 run 与 `baseline_run_id` 逐 case 比对，输出：

- `regressed`：上次过、这次不过 —— **门禁第一判据**
- `fixed`：上次不过、这次过
- `flaky`：同一 run 内重跑不一致

### 7.4 报告产出

CI 输出 JSON 归档 + Markdown 摘要（贴 PR 评论）；本地跑输出终端摘要。现有 `_summary_hook.py` 从"打印 Pass 计数"升级为产出结构化摘要。

## 8. CI 门禁

真实模型跑在每个 PR 上不现实（慢、费钱、有波动），因此按分层与时机拆开：

| 门禁 | 规则 | 时机 |
|---|---|---|
| security 层 | 任一 case 失败 → 红 | 每次 PR |
| stub 全量 | 任一 case 失败 → 红 | 每次 PR |
| temporal 层 | 任一 case 失败 → 红（stub 可判定） | 每次 PR |
| multiturn 层 | 任一 case 失败 → 红 | 每次 PR |
| holdout 真实模型 | `regressed` 非空 → 红；L1~L3 准确率低于基线−容差 → 红 | 每日 + 发布前 |
| robustness | 降级率超阈值 → 红 | 每日 |
| 非功能 | P95 延迟回退超阈值 → 黄（告警不阻塞） | 每日 |

基线用**相对上次基线的容差 + 版本化基线文件**，不用绝对阈值——真实模型输出有天然波动，绝对阈值要么太松要么天天误报。基线文件变更必须显式提交，防止悄悄调低门槛。

结果正确率门槛按域设置，首个销售域 ≥98%（`fangan.md` 7.4）。

## 9. 反馈闭环

`feedback` 表已存在（`app/observability/orm.py:89`），但只写不读。设计一条从差评到候选 case 的通道：

1. 差评 turn 导出为草稿 case，带 question / intent / plan / 实际结果 / trace_id
2. 人工补标准答案（不自动生成，见 §1 非目标）
3. 定级入 `holdout/`（新问法、真实分布）或 `runtime_examples/`（值得进提示词的范例）
4. 入库前跑 §5.2 防污染检查

导出为离线命令，不做界面（界面属 S7 范围外）。

## 10. 测试

评测框架本身需要被测——当前 `test_runner.py` 存在但覆盖不到这些语义：

- intent 不匹配时必须 FAIL 而非 XFAIL
- `known_issue` 缺失时不允许产出 XFAIL
- 澄清 followup 真的提交了选项（断言 `patch_slot` 入参），且第二轮报告参与断言
- 不同 case 的 `as_of` 互不影响；无全局时钟冻结残留
- ephemeral policy 在 case 结束后不残留（savepoint 回滚生效）
- holdout 问题出现在 runtime_examples 或提示词中时构建失败
- 缺失可复现三要素时拒绝生成 run 记录
- 回归检测正确区分 regressed / fixed / flaky
- 七层报告中无法判定的层产出 SKIPPED 而非 PASS
- `patch_slot` 对非本人 turn 返回 404（复用 S1 鉴权）
- `patch_slot` 不调用 LLM（断言 stub client 调用次数为 0）

## 11. 验收标准

1. `fangan.md` 7.1 六项空洞 + 核查新增两项全部修复，共八项。
2. `_to_outcome()` 产出真实 intent；Intent 层断言在 stub 与 real 模式下都实际执行。
3. `intent_diff` 一刀切开关移除；关键槽位差异产生 FAIL。
4. XFAIL 仅由显式 `known_issue` 产生，CI 统计其数量。
5. 澄清 followup 通过 `patch_slot` 提交选定选项，两轮均参与断言。
6. 全局 autouse 时钟冻结移除，`case.as_of` 逐 case 生效。
7. `ephemeral_policy` 实际安装 case 级策略并在 case 后回滚。
8. 六个分层目录建立，现有 22 个 case 完成迁移且断言内容未变。
9. holdout 首批 20 条入库并建立基线；防污染检查在构建期生效。
10. 七层打分落地，失败可定位到层。
11. 每次跑产出 `artifacts/eval/<run_id>.json`，含可复现三要素与 trace_id。
12. 回归检测输出 regressed / fixed / flaky，门禁以 regressed 为第一判据。
13. CI 按 §8 表格分级配置。
14. 差评导出草稿 case 的离线命令可用。
15. 后端测试基线不回退，新增测试全绿。

## 12. 风险

| 风险 | 处置 |
|---|---|
| holdout 60 条标准答案人力成本高 | 分批交付，首批 20 条先建门禁基线；这是本子项目最大人力项 |
| 修复空洞后大量 case 变红 | 预期结果，不是回归。变红 case 逐个定级：真实缺陷进问题清单，暂不修的登记 `known_issue` |
| `patch_slot` 提前到 S5 实现，与 S7 API 设计可能不一致 | S5 只定编排器层签名与语义，S7 的 API 是薄封装；签名变更的成本可接受 |
| 逐 case 时钟注入依赖 S2 的 DI Clock | S5 必须在 S2 之后实施，计划中标注硬依赖 |
| case 级策略回滚依赖 savepoint，某些测试库配置下不可靠 | 加显式验证测试（§10），失败即暴露而非静默失效 |
| 真实模型每日跑成本 | holdout 分层跑，非功能层与 robustness 可降频；成本预算在 S6 的成本可观测里统一处理 |
| 防污染检查误报（正常业务词汇重合） | 按整条问题文本归一化比对，不按关键词；误报时可显式豁免并记录理由 |
