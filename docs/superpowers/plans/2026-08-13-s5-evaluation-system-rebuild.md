# S5 评测体系重建 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Golden Set 从"能跑出 22 个绿灯"改造成真正能拦住回归的门禁：修完八项空洞、按用途分层、七层独立打分、Eval Run 归档与回归检测、CI 分级门禁、差评反馈通道。

**Architecture:** 当前的问题不是覆盖不足，而是**绿灯与正确性脱钩**。`test_golden_set.py:140` 把 `"intent": None` 写死，`loader.py:54` 让 `intent_diff` 默认 `"xfail"`，`conftest.py:36-41` 的 `ephemeral_policy` 是个 `return None`——三处叠加的结果是：Intent 层断言从未执行、真实模型任何差异都被降级、case YAML 的 `policies:` 字段从未生效。修复顺序刻意是"先让绿灯可信，再扩数据集"：先扩数据集只会得到更多不可信的绿灯。

**Tech Stack:** Python 3.13、pytest、PyYAML、PostgreSQL savepoint

## Global Constraints

约束来自 `docs/superpowers/specs/2026-08-13-s5-evaluation-system-rebuild-design.md`：

- **一个断言字段写死 `None` 比没有这个字段更危险**。无法判定的层显式产出 `SKIPPED` 并计入统计，绝不伪装成 `PASS`。
- **XFAIL 不是差异兜底**。只能由 case 显式 `known_issue: <编号>` 产生；关键槽位（指标、时间、权限决策、status）差异一律 FAIL。
- **门禁看回归，不看总分**。总分能掩盖"修好 3 个、弄坏 3 个"。第一判据是 `regressed` 为空。
- **holdout 污染不可逆**。防污染必须是构建期自动检查，不能依赖人工纪律。
- **holdout 标准答案人工确认**，不自动生成。
- 迁移现有 case 时**只移动文件与补元数据，不改断言内容**——避免"迁移顺手改期望"掩盖问题。
- `patch_slot` **不调用 LLM**。澄清回填是确定性操作。
- 代码注释与标识符用英文；文档与提交信息用中文。

## 前置

**硬依赖 S2**（逐 case 时钟注入依赖 DI Clock）、S1（PrincipalContext 与对象级鉴权）、S3（CanonicalQueryPlan）、S4（semantic revision、`eval_excluded`）。

本轮 Eval Run **先落文件归档**（`artifacts/eval/<run_id>.json`），迁入元数据库表在 S6，避免被 Alembic 迁移基线阻塞。归档 JSON 结构按可直接映射为表来设计。

结构化澄清的 HTTP API 与前端接入在 S7，本轮只做编排器层能力。

预期修复空洞后**大量 case 变红——这是预期结果，不是回归**。变红 case 逐个定级：真实缺陷进问题清单，暂不修的登记 `known_issue`。

---

### Task 1: 让 Intent 层断言真正执行

**Files:**
- Modify: `backend/tests/golden/test_golden_set.py`
- Modify: `backend/app/orchestration/outcome.py`（`TurnOutcome` 所在模块）
- Modify: `backend/tests/golden/runner.py`
- Create: `backend/tests/golden/test_framework_semantics.py`

**Interfaces:**
- Changed: `TurnOutcome` 暴露 intent 快照
- Removed: `_to_outcome()` 中写死的 `"intent": None`

- [ ] **Step 1: 写失败的框架语义测试**

`test_framework_semantics.py`（本任务起持续扩充，覆盖 spec §10）：构造一个 intent 指标错误的 case，断言结果为 **FAIL**。当前代码下这条测试必然不过——`"intent": None` 让 `diff_in` 永远拿到 `None`。

- [ ] **Step 2: `TurnOutcome` 暴露 intent 快照**

- [ ] **Step 3: `_to_outcome` 取真实 intent 值**

删除 `test_golden_set.py:140` 的 `"intent": None,`。

- [ ] **Step 4: 改为按 `user_id` 驱动**

`runner.py:92` 解析出 `user_id` 后，`_orchestrator.run()` 只读 `kwargs["username"]`（`test_golden_set.py:156`），解析结果被丢弃——与 S1「user_id 为唯一权限键」相悖。改为 `user_id` 驱动，`username` 仅用于取 principal。

---

### Task 2: XFAIL 分级与字段 tolerance

**Files:**
- Modify: `backend/tests/golden/loader.py`
- Modify: `backend/tests/golden/runner.py`
- Create: `backend/tests/golden/tolerance.py`
- Modify: `backend/tests/golden/test_framework_semantics.py`

**Interfaces:**
- Removed: `Expectation.intent_diff: str = "xfail"`（`loader.py:54` 的一刀切开关）
- Produces: 字段类别驱动的 tolerance 判定

- [ ] **Step 1: 写失败的 tolerance 测试**

| 字段类别 | 断言 |
|---|---|
| 指标、时间区间、权限决策、status | 差异 → **FAIL**，不容忍 |
| 维度、过滤 | 按**语义等价类**比对，不比字符串 |
| headline 措辞、assumptions 文案 | 只记 diff，不判失败 |
| confidence 数值 | 容差区间内视为通过 |

外加：**`known_issue` 缺失时不允许产出 XFAIL**。

- [ ] **Step 2: 实现 tolerance 并移除 `intent_diff`**

删除 `runner.py:110` 的 `if case.expect.intent_diff == "xfail"` 逃逸分支。

- [ ] **Step 3: XFAIL 改为显式登记**

只能由 case 声明 `known_issue: <问题编号>` 产生。CI 统计 XFAIL 总数，**上升即告警**。

---

### Task 3: 澄清链路闭环

**Files:**
- Modify: `backend/app/orchestration/orchestrator.py`
- Create: `backend/tests/orchestration/test_patch_slot.py`
- Modify: `backend/tests/golden/runner.py`（`run_clarify_followup`）
- Modify: `backend/tests/golden/test_golden_set.py`

**Interfaces:**
- Produces: `patch_slot(turn_id, target, selected_value) -> TurnOutcome`

`runner.py:157` 的 followup 用 `question=case.question` **重发原问题**，`select_option_index` 只写进 `notes` 字符串（`runner.py:164`）——澄清回填从未被测过。

- [ ] **Step 1: 写失败的 `patch_slot` 测试**

`test_patch_slot.py`：`target` 取 metric / dimension / time / filter_value；语义是取该 turn 的 intent 快照 → 替换指定槽位 → 走确定性解析与编译 → 产出新 turn。

两条关键断言：
- **非本人或不存在的 turn 均返回 404**（复用 S1 对象级鉴权）
- **不调用 LLM**——断言 stub client 调用次数为 0。重新问模型会引入新的不确定性，也让澄清测不出东西

- [ ] **Step 2: 实现 `patch_slot`**

- [ ] **Step 3: 改造 `run_clarify_followup`**

1. 跑第一轮，断言 `status == CLARIFYING` 与 `clarify_kind`
2. 从第一轮 outcome 读 `options`，按 `select_option_index` 取值——**索引越界 → ERROR，不静默跳过**
3. 调 `patch_slot`
4. 断言第二轮期望

- [ ] **Step 4: 两轮都参与断言**

`test_golden_set.py:234` 只 `assert first.status`，`second_report` 算完丢弃——澄清第二轮结论不影响测试结果。改为两个报告都参与最终断言。

---

### Task 4: 逐 case 时钟与 case 级策略

**Files:**
- Modify: `backend/tests/golden/conftest.py`
- Modify: `backend/tests/golden/runner.py`
- Modify: `backend/tests/golden/test_framework_semantics.py`

**Interfaces:**
- Removed: `conftest.py:19-25` autouse 全局 `clock.freeze(date(2026,8,12))`
- Changed: `ephemeral_policy` 从 no-op 改为真实实现

- [ ] **Step 1: 写失败的时钟隔离测试**

断言：不同 case 的 `as_of` 互不影响；**无全局时钟冻结残留**。当前 `case.as_of` 只在 `GoldenCase` 里传递，到不了运行时时钟，导致 `temporal/` 分层不可能建立。

- [ ] **Step 2: 删除全局冻结，改为逐 case 注入 `FixedClock(case.as_of)`**

依赖 S2 的 DI Clock。

- [ ] **Step 3: 写失败的 case 级策略测试**

断言 case YAML 的 `policies:` 字段真实生效，且 **case 结束后不残留**。

`conftest.py:36-41` 的 `ephemeral_policy` 是彻底的 no-op（`return None`），注释称"测试工厂已装好策略矩阵"。`fangan.md` 写「必须实际创建 case 级策略并在事务中回滚」，暗示已有实现但缺回滚——实际是 no-op，**case 级策略能力不存在，需从零实现**。

- [ ] **Step 4: 实现 case 级策略安装 + savepoint 回滚**

savepoint 在某些测试库配置下不可靠，因此 Step 3 的残留检测测试是必须的——**失败即暴露而非静默失效**。

---

### Task 5: 结构化 CaseReport 与七层打分

**Files:**
- Modify: `backend/tests/golden/runner.py`（`CaseReport`）
- Create: `backend/tests/golden/scoring.py`
- Create: `backend/tests/golden/test_scoring.py`

**Interfaces:**
- Changed: `CaseReport` 从 `message: str`（`runner.py:18`）改为结构化，含 L1~L7 逐层判定与 diff

- [ ] **Step 1: 写失败的七层打分测试**

| 层 | 判定对象 | 判定方式 |
|---|---|---|
| L1 Intent | IntentV2 槽位 | 指标/时间/维度/过滤逐槽比对，按等价类 |
| L2 Resolve | 语义解析结果 | 指标解析、口径版本、时间锚点落值、假设与澄清 |
| L3 Plan | CanonicalQueryPlan | **结构等价（非字符串）**，含 `required_field_lineage` |
| L4 SQL | 生成的 SQL | AST 等价 + 护栏结论（是否拦、是否限流、方言） |
| L5 Result | 结果集 | 行数、首行、关键单元格（Decimal 容差）、排序、当前/基期 |
| L6 Answer | 自然语言回答 | 引用三要素完整（指标/时间/过滤）、无泄漏、单位、警告 |
| L7 非功能 | 延迟、token、重试次数、扫描行数 | 阈值区间 |

核心断言：**无法判定的层产出 `SKIPPED` 而非 `PASS`**。

- [ ] **Step 2: 实现结构化 CaseReport 与逐层判定**

**总分只用于趋势观测，不用于门禁**——打分的目的是定位失败层。

- [ ] **Step 3: 拒答与澄清指标独立统计**

precision / recall 分开统计，**不混入总准确率**。

---

### Task 6: 数据集分层与防污染

**Files:**
- Move: `backend/tests/golden/cases/*` → 六个分层目录
- Modify: `backend/tests/golden/loader.py`（case 元数据）
- Create: `backend/tests/golden/contamination.py`
- Create: `backend/tests/golden/test_contamination.py`
- Create: `backend/tests/golden/cases/holdout/`、`backend/tests/golden/cases/temporal/`

**Interfaces:**
- Produces: 六个用途分层
- Produces: case 元数据 `layer`/`tags[]`/`eval_excluded`/`owner`/`created_at`/可选 `known_issue`

当前 22 个 case 按"问题类型"分目录（`simple/mom/yoy/topn/multidim/clarify/permission/out_of_scope`），这个维度不区分**用途**，无法区分「可进提示词的示例」与「必须盲测的评估集」。

- [ ] **Step 1: 建立六个分层目录**

| 分层 | 用途 | 规模目标 | CI 门禁 | 可用于 few-shot |
|---|---|---|---|---|
| `runtime_examples/` | 提示词示例、开发自测 | 20~30 | 否 | **是** |
| `holdout/` | 真实模型准确率评估 | 60（首批 20 建基线） | 是（阈值门禁） | **严格禁止** |
| `security/` | 权限、脱敏、越权、枚举探测、注入、跨租户 | 30+ | 是（**零容忍**） | 否 |
| `multiturn/` | 澄清回填、上下文继承、指代、会话所有权 | 20+ | 是 | 否 |
| `robustness/` | 错别字、口语、超长、空问题、冲突指令 | 20+ | 是（降级阈值） | 否 |
| `temporal/` | 财年、跨年、闰年、月末、周边界、时区、相对时间 | 20+ | 是 | 否 |

- [ ] **Step 2: 迁移现有 22 个 case**

`simple`/`mom`/`yoy`/`topn`/`multidim` → `runtime_examples/`；`clarify` → `multiturn/`；`permission` → `security/`；`out_of_scope` → `robustness/`。

**只移动文件与补元数据，不改断言内容。**

- [ ] **Step 3: case 元数据与一致性校验**

`layer` 与所在目录不一致时**加载期报错**。

- [ ] **Step 4: 写失败的防污染测试**

断言：任一 `holdout/` case 的问题文本出现在提示词模板、few-shot 示例或 `runtime_examples/` 中 → **构建失败**。比对用归一化文本（去空格与标点）以防轻微改写绕过。

- [ ] **Step 5: 实现构建期防污染检查**

误报（正常业务词汇重合）按整条问题文本归一化比对来规避，不按关键词；确有误报时可显式豁免并记录理由。

- [ ] **Step 6: holdout 首批 20 条与 temporal 建设**

标准答案**人工确认**。holdout 剩余 40 条分批交付——这是本子项目最大人力项。

---

### Task 7: Eval Run 归档与回归检测

**Files:**
- Create: `backend/app/eval/run_archive.py`
- Create: `backend/app/eval/regression.py`
- Create: `backend/tests/eval/test_run_archive.py`
- Create: `backend/tests/eval/test_regression.py`
- Create: `artifacts/eval/.gitignore`

**Interfaces:**
- Produces: `artifacts/eval/<run_id>.json`
- Produces: 回归检测输出 `regressed` / `fixed` / `flaky`

- [ ] **Step 1: 写失败的归档测试**

run 级字段：`run_id`/`started_at`/`mode`/`layers[]`/`model`/`prompt_version`/`semantic_revision`/`git_commit`/按层聚合 `totals`/`baseline_run_id`。
case 级：`case_id`/`layer`/`per_layer`(L1~L7 判定与 diff)/`duration_ms`/`tokens`/`trace_id`/`status`/`known_issue`。

`trace_id` 让失败 case 能直接跳到产品 Trace 看完整执行链——当前调试最缺的一环。

关键断言：**可复现三要素（`model` + `prompt_version` + `semantic_revision`）任一缺失 → 拒绝生成 run 记录**，不产出无法解释的数据。

- [ ] **Step 2: 实现归档**

JSON 结构按**可直接映射为 `eval_run` / `eval_case_result` 表**来设计（S6 迁库）。

- [ ] **Step 3: 写失败的回归检测测试**

`regressed`（上次过、这次不过，**门禁第一判据**）/ `fixed` / `flaky`（同一 run 内重复结果不一致）三类正确区分。

- [ ] **Step 4: 实现回归检测**

---

### Task 8: CI 分级门禁

**Files:**
- Create: `backend/tests/golden/baselines/`（版本化基线文件）
- Modify: `backend/pytest.ini`（分层 marker）
- Create: `.github/workflows/eval.yml`

- [ ] **Step 1: 按层配置门禁**

| 层 | 判据 | 频率 |
|---|---|---|
| security | 任一失败 → 红（零容忍） | 每次 PR |
| multiturn / temporal | regressed 非空 → 红 | 每次 PR |
| holdout | L1/L3 准确率低于基线−容差 → 红 | 每日 + 发布前 |
| robustness | 降级率超阈值 → 红 | 每日 |
| 非功能 | P95 延迟回退超阈值 → 黄（告警不阻塞） | 每日 |

- [ ] **Step 2: 版本化基线文件**

用**相对上次基线的容差 + 版本化基线文件**，不用绝对阈值——真实模型输出有天然波动，绝对阈值要么太松要么天天误报。基线文件变更**必须显式提交**，防止悄悄调低门槛。

结果正确率门槛按域设置，首个销售域 ≥98%。

- [ ] **Step 3: 成本控制**

holdout 分层跑，非功能层与 robustness 可降频。成本预算在 S6 的成本可观测里统一处理。

---

### Task 9: 差评反馈通道

**Files:**
- Create: `backend/app/eval/feedback_export.py`
- Create: `backend/tests/eval/test_feedback_export.py`

`feedback` 表已存在（`app/observability/orm.py:89`），但**只写不读**。

- [ ] **Step 1: 写失败的导出测试**

差评 turn 导出为草稿 case，带 question / intent / plan / 实际结果 / trace_id；入库前跑防污染检查。

- [ ] **Step 2: 实现离线导出命令**

人工补标准答案（不自动生成），定级入 `holdout/`（新问法、真实分布）或 `runtime_examples/`（值得进提示词的范例）。不做界面。

---

## 验收

1. Intent 层断言真实执行，`"intent": None` 已删除。
2. 按 `user_id` 驱动，`user_id_resolver` 结果不再被丢弃。
3. `intent_diff` 一刀切开关移除；关键槽位差异产生 FAIL。
4. XFAIL 仅由显式 `known_issue` 产生，CI 统计其数量。
5. 澄清 followup 通过 `patch_slot` 提交选定选项，两轮均参与断言；`patch_slot` 不调用 LLM 且对非本人 turn 返回 404。
6. 全局 autouse 时钟冻结移除，`case.as_of` 逐 case 生效。
7. `ephemeral_policy` 实际安装 case 级策略并在 case 后回滚。
8. 六个分层目录建立，现有 22 个 case 完成迁移且断言内容未变。
9. holdout 首批 20 条入库并建立基线；防污染检查在构建期生效。
10. 七层打分落地，无法判定的层产出 SKIPPED。
11. 每次跑产出 `artifacts/eval/<run_id>.json`，含可复现三要素与 trace_id。
12. 回归检测输出 regressed / fixed / flaky，门禁以 regressed 为第一判据。
13. CI 按 Task 8 表格分级配置。
14. 差评导出草稿 case 的离线命令可用。
15. 后端测试基线不回退，新增测试全绿。
