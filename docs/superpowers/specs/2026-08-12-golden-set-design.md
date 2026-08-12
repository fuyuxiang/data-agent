# Golden Set 与真实 LLM 意图测试 设计文档

> 子项目范围：Data Agent「可信问数闭环」补丁计划 06。
> 上游范围基线：`docs/superpowers/specs/2026-08-12-trusted-query-loop-design.md` §8（测试策略）。

## 1. 定位与目标

### 定位

补丁计划。叠加在既有 5 份计划之上，**不动任何既有 Task、不变更任何 API/接口**。补两件事：

1. **端到端 Golden Set**——spec §8 第四条要求的、覆盖 7 类问题的真实样本库跑测问题集。当前只有零散编排器测试替代，缺 TopN 端到端、缺澄清回填后的第二轮。
2. **真实 LLM 意图测试**——spec §8 第三条要求的固定问题集跑真实模型。当前 `OpenAiCompatClient` 零集成测试。

同时**就地修复**子代理审计出的 7 处跨计划引用硬伤（详见 §7），不写新 spec 主体。

### 目标

- 在 stub 模式下 26 项用例全部通过（CI 合并门禁）。
- 在 real 模式下无关键 intent 漂移（XFAIL 报告可见，CI 不红）。
- 既有 5 份计划 419 项测试在 Golden Set 落地后继续全绿。
- `app/core/clock.py` 成为唯一 wall clock 出口；前序计划如有用 `datetime.now()` 的地方改为 `app.core.clock.now()`。
- 7 处引用硬伤全部修复，下一位实施者照计划实施不会被卡住。

### 非目标（本计划明确不做）

| 项 | 理由 |
|---|---|
| 评测中心（M-35/M-36/M-37） | spec §1 已列为下一轮。本计划产出物的升级路径在 spec §8 末段已写明 |
| 重写任何既有 Task | 本计划是叠加层，非重写层 |
| 修改 spec §1 的目标范围 | spec 已包含这两层，只缺计划承载 |
| HTTP 层端到端测试 | 计划 04 `test_chat_api.py` 已覆盖 17 项 |
| 真实模式自动化触发 CI | 默认 stub 模式为门禁；real 模式由开发者本地或定期 batch 触发 |
| 评测报告可视化 | 终端 summary 即可定位；HTML 报告留待评测中心 |

## 2. 核心设计决策

### 决策 1：YAML 数据文件 + 通用 runner

spec §8 末段要求「端到端问题集按 Golden Set 格式编写，下一轮可直接升级为评测集，无需重写」。这是对问题集载体形式的强约束。

**采用方案**：YAML 文件（按类别分目录） + 一个 Python runner 通过 `pytest.mark.parametrize` 遍历。runner 是唯一 Python 代码，问题集本身是数据。

**理由**：
- 下一轮评测中心可以直接读同一份 YAML 文件或批量导入数据库，零重写。
- YAML 比 Python dataclass 更易被非工程师维护（运营/分析可读可改）。
- 比每条用例一个独立 `test_` 函数更易管理；后者把问题与断言缠在一起，不利于评测化。
- 代价（断言表达力上限）由 §3 的 Pydantic 校验 + `CitationExpectation` 子结构弥补。

### 决策 2：双模式跑同一份 YAML

YAML 里的 `expect.intent` **一份字段两种语义**：

| 模式 | `expect.intent` 的角色 |
|---|---|
| stub | **LLM 的固定输出**——喂给 `StubClient`，意图识别阶段不再调真模型 |
| real | **期望值**——对照真实 `OpenAiCompatClient` 产出，差异按 `expect.intent_diff` 处理 |

**理由**：
- 一份 YAML 同时覆盖 spec §8 第三层（意图识别跑真实 LLM）与第四层（端到端 Golden Set）。
- stub 模式无 API key 依赖、确定性、CI 必跑；real 模式用于 prompt 退化检测。
- 失败语义分明：stub 失败 = 代码 bug，real 失败（默认 XFAIL）= 模型行为偏移。

### 决策 3：锁定「今天」= 2026-08-12

样本数据硬编码为 2026-07 与 2026-08 两个月份。但意图识别阶段让 LLM 把「本月/上月/本周」解析成绝对日期——若不锁定参考日期，所有相对时间词的用例一旦跑过 2026-09 都会自然漂移。

**采用方案**：

- 新增 `app/core/clock.py`，作为唯一 wall clock 出口。
- `tests/golden/conftest.py` 的 `frozen_clock` fixture（autouse）将 `clock.today()` 锁定为 2026-08-12。
- Runner 在拼 LLM system prompt 时插一行后缀：`今天的参考日期是 {today().isoformat()}。凡是「本月/上月/本周」都以此为准。`
- 期望值以 2026-08-12 为准写入 YAML。

**不采用动态样本方案**：理由是两份样本（一份冻结、一份随时间漂移）难维护；锁日期方案让样本唯一、期望唯一，YAML 与样本一一对应。

### 决策 4：约 25 条用例覆盖 7 类

spec §8 列的 7 类问题（简单问数 / 同比环比 / TopN / 多维 / 模糊触发澄清 / 无权限拒答 / 超范围拒答），每类 3–5 条边界。总计约 25 条 + 1 条澄清回填 = **26 项**。

**不采用饱和覆盖方案**（60~80 条）的理由：本次交付量明显重；YAML 手写成本与 LLM 调用成本同步上涨；下一轮评测中心启动时再补全到 60~80 条更合理。

**不采用 1 类 1 条方案**（7 条）的理由：同一类里的边界条件（如 TopN 的 N=1 / N=5 / N 超大）若不覆盖，回归进不来。

## 3. 数据契约

### `app/core/clock.py`（新增，~20 行）

唯一 wall clock 出口。所有需要「今天」的代码（意图识别 prompt 注入、Trace 时间戳、引证 `data_updated_at`、日志）都从这里取，不直接 `date.today()` 或 `datetime.now()`。

```python
"""Single source of truth for wall clock. Tests freeze it via monkeypatch."""

from datetime import date, datetime
from threading import Lock

_lock = Lock()
_frozen: date | None = None


def today() -> date:
    """Return the process's notion of today. Frozen by frozen_clock fixture."""
    with _lock:
        if _frozen is not None:
            return _frozen
    return date.today()


def now() -> datetime:
    """Wall clock as datetime. Frozen to today's 09:00 to match the sample data_updated_at."""
    d = today()
    return datetime(d.year, d.month, d.day, 9, 0)


def freeze(d: date) -> None:
    """Set today's date for the process. Used by frozen_clock fixture."""
    global _frozen
    with _lock:
        _frozen = d


def unfreeze() -> None:
    global _frozen
    with _lock:
        _frozen = None
```

**为什么是进程级而不是 fixture 级**：意图识别 prompt 里要拼 `今天的参考日期是 {today().isoformat()}`，这个字符串一旦在 LLM 调用中漏掉就要重跑整个调用。monkeypatch 在 teardown 自动复位，进程级 + monkeypatch 是最稳的组合。

**对前序计划的回填**（一处，预防性提示）：计划 04 Task 6 的编排器在写 Trace 时如果用了 `datetime.now()`，改用 `app.core.clock.now()`。计划 04 没明确写「使用哪个 now」，子代理审计也没发现硬依赖；实施时若现有实现仍用 `datetime.now()` 就换，无则跳过。

### `GoldenCase`（`tests/golden/loader.py`）

```python
@dataclass(frozen=True)
class GoldenCase:
    id: str                              # G-002
    question: str                        # "上月销售额"
    as_of: date                          # 2026-08-12（默认来自 fixture，可显式覆写）
    as_user: str                         # "admin" / "analyst_east"
    mode_required: Literal["any", "real_only", "stub_only"] = "any"
    expect: Expectation
    policies: tuple[PolicySpec, ...] = ()  # 临时插入的策略
    followup: FollowupSpec | None = None   # 澄清回填用例的第二轮
    notes: str = ""


@dataclass(frozen=True)
class Expectation:
    status: Literal["ANSWERED", "CLARIFYING", "REFUSED", "FAILED"]
    intent: IntentExpectation | None    # None 表示该用例不锁 intent 形状
    rows: int | None                     # 期望行数
    first_row: dict[str, Any] | None    # 期望首行（按列名匹配）
    citation_has: tuple[CitationExpectation, ...]  # 引证里至少包含这些 kind
    refused_leaks: tuple[str, ...] = ()  # 拒答响应中禁止出现的子串
    intent_diff: Literal["xfail", "fail"] = "xfail"  # 真实模式槽位差异的处理


@dataclass(frozen=True)
class IntentExpectation:
    metric: str | None
    time: TimeRange | None               # 期望绝对日期
    dimension: tuple[str, ...] = ()
    filters: tuple[FilterCondition, ...] = ()
    comparison: ComparisonKind | None = None
    top_n: int | None = None
```

**字段缺省语义**：`None` / `()` / 缺省 = 「不锁这一项」。例如引证断言里没写 `citation_has`，就只比对 `status` 与 `rows`。

**YAML 加载**：`pyyaml.safe_load` 解析 `tests/golden/cases/**/*.yaml`，Pydantic v2 强校验（`GoldenCase.model_validate(dict)`），schema 不合法直接让 pytest 在 collection 阶段红——早失败。

### `PolicySpec` 与 `FollowupSpec`

```python
@dataclass(frozen=True)
class PolicySpec:
    kind: Literal["row_policy", "column_policy"]
    field: str
    allowed_values: tuple[str, ...] = ()
    masked: bool = False


@dataclass(frozen=True)
class FollowupSpec:
    as_user: str
    select_option_index: int
    expect: Expectation
```

## 4. 架构与组件

### 文件结构

```
backend/
  app/
    core/
      clock.py                   # NEW：唯一 wall clock 出口
    semantic/
      (不动)
    pipeline/
      (不动)
    security/
      (不动)
  tests/
    golden/
      __init__.py
      conftest.py                # NEW：frozen_clock + ephemeral_policy fixture
      loader.py                  # NEW：load_cases()、diff_in()
      runner.py                  # NEW：run_case(case, mode) -> CaseReport
      test_golden_set.py         # NEW：parametrize + 报告生成
      cases/
        _index.yaml              # 用例索引（可选，列出全部 ID 与路径）
        simple/  g001.yaml g002.yaml g003.yaml
        mom/     g010.yaml g011.yaml g012.yaml
        yoy/     g013.yaml g014.yaml
        topn/    g020.yaml g021.yaml g022.yaml
        multidim/ g030.yaml g031.yaml
        clarify/ g040.yaml g041.yaml g042.yaml g043_followup.yaml
        permission/ g050.yaml g051.yaml
        out_of_scope/ g060.yaml g061.yaml
```

### 数据流（一次用例执行）

```text
用例 G-002.yaml
  ├─ case = load_cases()[id=G-002]
  │     question = "上月销售额"
  │     as_of    = "2026-08-12"          # 默认来自 fixture
  │     as_user  = "admin"
  │     expect   = {status: ANSWERED, intent: {metric: ..., time: ...}, rows: ..., first_row: {...}, citation_has: [...]}
  │
  ├─ frozen_clock fixture：app.core.clock.today() = 2026-08-12
  │   ephemeral_policy fixture（若有 as_user / policies）：为该用户注入临时策略，测试结束清理
  │
  ├─ 模式选择（由环境变量 DATA_AGENT_GOLDEN_MODE 控制，默认 stub）：
  │     stub 模式 → StubClient 注入 case.expect.intent 作 LLM 输出 → 跳过 INT 阶段断言
  │     real 模式 → OpenAiCompatClient 真实调用 → INT 阶段对照 expect.intent，差异 xfail
  │
  ├─ 跑一遍完整七阶段 Pipeline（复用计划 04 Task 6 的编排器）
  │     阶段 1（Verified Query）：按语义命中表查 case.id；未命中继续
  │     阶段 2（INTENT）：见模式选择
  │     阶段 3（SEMANTIC_RESOLVE）：值别名映射、置信度判定、澄清
  │     阶段 4（COMPILE）：意图 + 语义模型 → SQL AST
  │     阶段 5（SECURE）：RLS 注入 + 脱敏 + 白名单
  │     阶段 6（EXECUTE）：PostgreSQL 真实样本库
  │     阶段 7（ANSWER）：数字 + 引证
  │
  ├─ 断言（runner.assert_case(outcome, case.expect, mode)）：
  │     status            → outcome.status
  │     intent            → 模式相关（stub 跳过 / real diff xfail）
  │     rows / first_row  → outcome.rows 的结构与值
  │     citation_has      → 引证行 kind 列表包含期望项
  │     refused_leaks     → 拒答响应里禁止出现数据集名/字段名/表名
  │
  └─ 写一份 CaseReport（pass/xfail/refused/error）进 pytest 的报告 phase
```

### 与既有组件的耦合边界

- **复用 `app.pipeline.orchestrator.run()`**——不写新编排器，runner 只调既有入口。
- **复用 `app.core.config.Settings`**——不新增任何 LLM 客户端开关；只通过 `clock.frozen_date` 间接影响（计划 01 已定义 `Settings`，无改动）。
- **复用 `app.security` 的策略模型**——`ephemeral_policy` 走计划 03 Task 1 的策略插入/删除接口；具体函数名待实施时确定，本计划不强约束。
- **复用 `tests/semantic/factories.build_orders_dataset`**——不另起数据集构造。
- **不动**计划 04 的 `env` fixture——runner 只额外叠加 `frozen_clock` 与 `ephemeral_policy`。

**一个有意为之的简化**：Runner **不** 启动 FastAPI 客户端。所有断言走内存里的编排器入口，与计划 04 的 orchestrator 测试同一层级。HTTP 层断言交给计划 04 的 `test_chat_api.py` 17 项（已覆盖）。这样 Golden Set 跑得快、CI 稳。

## 5. Runner、错误处理与测试策略

### `runner.run_case(case, mode) -> CaseReport`

```python
@dataclass(frozen=True)
class CaseReport:
    id: str
    status: Literal["PASS", "XFAIL", "FAIL", "ERROR", "SKIPPED"]
    mode: Literal["stub", "real"]
    duration_ms: int
    message: str = ""
    diff: dict[str, tuple[Any, Any]] | None = None
```

**执行流**：

```text
run_case(case, mode)
  ├─ frozen_clock 已就绪 → as_of 校验（若 case.as_of 与 frozen 不同，记录 warning 但不阻断）
  ├─ ephemeral_policy.install(case.as_user, case.policies)  # 若有
  │
  ├─ if case.mode_required == "real_only" and mode == "stub"  → return SKIPPED
  ├─ if case.mode_required == "stub_only" and mode == "real"  → return SKIPPED
  │
  ├─ outcome = orchestrator.run(
  │     question=case.question,
  │     user_id=resolve_user(case.as_user),
  │     client=StubClient(case.expect.intent) if mode == "stub"
  │            else OpenAiCompatClient(settings),
  │     llm_mode=mode,
  │   )
  │
  ├─ if mode == "real" and case.expect.intent is not None:
  │     intent_diff = diff_in(outcome.intent, case.expect.intent)
  │     if intent_diff:
  │         if case.expect.intent_diff == "xfail":
  │             return CaseReport(XFAIL, diff=intent_diff)
  │         return CaseReport(FAIL, diff=intent_diff)
  │
  ├─ 后续阶段断言（status / rows / first_row / citation_has / refused_leaks）
  │     任一失败 → FAIL，带具体哪一项错
  │
  ├─ ephemeral_policy 自动清理（fixture teardown）
  │
  └─ return CaseReport(PASS)
```

### 错误处理：每类失败映射到断言产物

| 失败类型 | 触发条件 | 报告产物 | CI 影响 |
|---|---|---|---|
| **用例 YAML 不合法** | collection 时 schema 校验失败 | pytest 报错 | 红 |
| **`as_of` 与 frozen 不一致** | YAML 写 2026-08-11，fixture 锁 2026-08-12 | warning，case 仍跑 | 不红 |
| **`status` 不匹配** | outcome.status ≠ expect.status | FAIL | 红 |
| **`rows` 不匹配** | 期望 5 行实得 3 行 | FAIL | 红 |
| **`first_row` 不匹配** | 列值偏差 | FAIL | 红 |
| **`citation_has` 缺失** | 引证没有 `metric`/`permission` 任一 | FAIL | 红 |
| **`refused_leaks` 命中** | 拒答响应里出现 `sample.orders` 等 | FAIL | 红 |
| **真实模式 intent 漂移** | slot 与 expect 差 | XFAIL（默认）或 FAIL（显式声明） | 不红 / 红 |
| **编排器抛异常** | 未预期的 Exception | ERROR（区别于 FAIL——表示系统 bug） | 红 |
| **真实模式 API 失败** | 网络、超时、4xx | ERROR | 红 |
| **模式不匹配** | `mode_required="real_only"` 但当前跑 stub | SKIPPED | 不红 |

### `refused_leaks` 的实现细节

拒答类用例断言两件事：

1. `status == REFUSED`
2. 响应里**不能**包含任何元数据字符串——YAML 里写出来（`sample.orders` / 字段名 / 数据集名）

```python
def assert_no_leak(response_text: str, forbidden: tuple[str, ...]) -> None:
    for token in forbidden:
        if token in response_text:
            raise AssertionError(f"refusal leaks metadata: {token!r}")
```

**为什么把 `refused_leaks` 放在 YAML 里而不是 hardcode**：

- spec §5.6 列了 4 类元数据（表名、字段名、值别名、数据集名），具体哪类对哪条用例要禁，**因用例而异**。
- 让 YAML 自己写清楚，runner 只做通用检查——避免在 runner 里写死一份禁止列表导致 spec 演进时不同步。

### 澄清回填用例（g043）

spec §8 提到的「澄清回填后的第二轮」端到端用例，是 **唯一一个** 不走单轮 `run_case()` 的用例类型。

```yaml
# tests/golden/cases/clarify/g043_followup.yaml
- id: G-043
  question: 财务确认收入是多少            # 第一轮：意图识别低置信度 → 澄清
  expect_first:
    status: CLARIFYING
    clarify_kind: METRIC
  followup:
    as_user: admin
    select_option_index: 0               # 选「含税订单金额」
  expect_second:
    status: ANSWERED
    metric: sales_revenue
```

Runner 在 `run_case()` 之外暴露 `run_clarify_followup()`：

1. 先跑第一轮，断言 `expect_first`，拿到 `ClarifyRequest.options`。
2. 按 `select_option_index` 构造第二轮输入（复用计划 04 Task 7 已实现的 `slot_state` 回写机制）。
3. 再跑第二轮，断言 `expect_second`。

### 三层测试的覆盖关系

| 层 | 跑什么 | 数量 | 谁挡什么 |
|---|---|---|---|
| **编译器纯函数**（计划 02） | intent → SQL | ~55 项 | 任何编译逻辑回归 |
| **编排器+stub**（计划 04） | 7 阶段全链路，stub 喂 payload | ~129 项 | 编排逻辑、安全改写、结果校验 |
| **Golden Set stub 模式**（本计划） | 真实样本库 + YAML 期望 | 26 项 | 端到端业务结果是否对得上 |
| **Golden Set real 模式**（本计划） | 真实 LLM + 真实样本库 | 同 26 项 | prompt 退化、模型升级 |

**互补关系**：

- 编排器测试**不**用真实样本数据全量（部分用 stub 数据集），所以不验真业务口径。
- Golden Set **不**覆盖编译器的边界条件（同比闰日之类）——那是编译器纯函数测试的领地。
- 真实模式是**唯一**覆盖 prompt→intent 这一段的层；编排器测试里 INT 阶段是 stub。

### 测试规模与执行

- **stub 模式**：25 项 + 1 项澄清回填（g043）= **26 项**。CI 必跑，失败即红，作为合并门禁。
- **真实模式**：26 项（同一份 YAML）。不在默认 CI 跑；通过环境变量 `DATA_AGENT_GOLDEN_MODE=real pytest tests/golden/` 触发。开发者本地 + 定期 batch 跑。
- **YAML schema 校验**：pytest collection 阶段跑，不计入测试数量但失败即红。

### 报告产出

每个 pytest 报告里附一段额外 summary（`pytest_terminal_summary` hook）：

```text
========== Golden Set Summary ==========
Mode: stub  | Cases: 26 | Pass: 26 | Xfail: 0 | Fail: 0 | Error: 0
Mode: real  | Cases: 0   | Pass: 0  | Xfail: 0 | Fail: 0 | Error: 0  (skipped by default)
========================================
```

真实模式跑过后：

```text
========== Golden Set Summary ==========
Mode: stub  | Cases: 26 | Pass: 26 | Xfail: 0 | Fail: 0 | Error: 0
Mode: real  | Cases: 26 | Pass: 24 | Xfail: 2 | Fail: 0 | Error: 0
XFAIL:
  G-013.yoy.time.start   期望 2025-08-01, 实际 2025-08-04（闰日处理边界）
  G-021.topn.top_n       期望 5, 实际 3（模型对 N 的解析偏好小值）
========================================
```

**为什么不写 HTML 报告**：升级到评测中心时再写。当前阶段让 diff 直接落在 terminal summary 里足够定位。

### 一个边界：跑得动

跑通要求：

- stub 模式：环境无 LLM API key 也能跑。
- real 模式：环境有 `OPENAI_API_KEY` 或对应兼容端点（计划 04 已支持 `llm_api_key`/`llm_base_url`/`llm_model` 配置）。

**默认行为**：`DATA_AGENT_GOLDEN_MODE` 未设 → stub；设为 `real` 但缺 API key → 在 collection 阶段报 `pytest.skip` 而不是失败，给出明确信息：「real mode requires LLM_API_KEY」。

## 6. 代码起点

工作区当前处于「仅有文档」状态（计划 01~05 待实施，无任何 Python 代码）。

本计划**待计划 01~05 全部实施完成后**才能实施——具体依赖：

| 依赖项 | 来源 |
|---|---|
| `meta_session` / `sample_conn` / `prepared_database` fixture | 计划 01 Task 1 |
| `build_orders_dataset()` 工厂 | 计划 01 Task 2 |
| `QueryIntent` / `TimeRange` / `ComparisonKind` / `FilterCondition` Schema | 计划 02 Task 1 |
| `ComparisonKind.MOM`/`YOY` 在编译器内的时间计算 | 计划 02 Task 2 |
| `resolve_intent()` + `ResolveOutcome` | 计划 04 Task 3 |
| `orchestrator.run(question, user_id, client, llm_mode)` 入口 | 计划 04 Task 6 |
| `StubClient` 与 `OpenAiCompatClient` 抽象 | 计划 04 Task 2 |
| 策略插入/删除函数 | 计划 03 Task 1 |
| `ClarifyRequest.options` | 计划 04 Task 3 |

实施顺序：本计划排在计划 01~05 之后，作为计划 06。

## 7. 跨计划修复点（顺手修 7 处引用硬伤）

不写在本设计文档主体里，直接就地改在原计划。逐条列出定位与改法。

### 修复 1：计划 05 自查表「计划 01 Task 8 → /api/datasets」

**位置**：`docs/superpowers/plans/2026-08-12-05-frontend-workbench.md:4528`

**改法**：把"计划 01 Task 8"改成"计划 01 Task 6"，把 `/api/datasets`、`/api/datasets/{name}`、`/lint`、`/publish` 全部补前缀 `/semantic`——与计划 01 Task 6 实际定义一致。

### 修复 2：计划 04 回填表「get_sample_connection → 计划 01 Task 7」

**位置**：`docs/superpowers/plans/2026-08-12-04-pipeline-observability.md:4293`

**改法**：删掉「计划 01 的 `app/core/db.py`（Task 7 Step 4）」，改为「`app/core/db.py`（计划 04 Task 7 Step 4 自身新增）」。计划 01 的 `db.py` 不承担 `get_sample_connection`。

### 修复 3：计划 04 回填表「LLM 配置字段对计划 01 回填」

**位置**：`docs/superpowers/plans/2026-08-12-04-pipeline-observability.md:4291`

**改法**：把整行改为只回填 `llm_timeout_seconds`（计划 01 已定义其余三项）；回填说明从"计划 01 的配置"改为"在计划 01 Task 1 Step 4 的 Settings 上补 `llm_timeout_seconds: float = 30.0`"。

### 修复 4：计划 04 回填表「澄清阈值对计划 01 回填」

**位置**：`docs/superpowers/plans/2026-08-12-04-pipeline-observability.md:4292`

**改法**：删除整行——`clarify_confidence_threshold` 与 `clarify_max_rounds` 已在计划 01 Task 1 Step 4 定义，无需回填。

### 修复 5：计划 03 回填表「DatasetDef.has_metric 对计划 02」

**位置**：`docs/superpowers/plans/2026-08-12-03-security-execution.md:2574`

**改法**：把"对计划 02 的两处回填"改为"对计划 01 的回填"，并附注「`DatasetDef` 位于计划 01 的 `app/semantic/model.py`，本回填在计划 01 那一侧添加 `has_metric(name: str) -> bool` 方法」。

### 修复 6 & 7：计划 03 Task 1 引用 `app.core.db.MetaBase` / `META_SCHEMA`

**位置**：`docs/superpowers/plans/2026-08-12-03-security-execution.md:163`

**改法（双重保障）**：

1. **在计划 01 Task 1 Step 2 的 `app/core/db.py` 末尾增加**两行 re-export：
   ```python
   from app.semantic.orm import Base as MetaBase, META_SCHEMA  # noqa: F401
   ```
   不破坏既有 5 份计划的导入约定：所有引用 `app.semantic.orm.META_SCHEMA` 的代码照旧工作，新增的 `app.core.db.META_SCHEMA` 是同一对象。

2. **同步在计划 03 Task 1 的「Interfaces → Produces」列表里**补充一行："回填计划 01 Task 1 Step 2：`app/core/db.py` re-export `MetaBase` 与 `META_SCHEMA`"——让回填责任显式归属于计划 01。

### 一处不修

子代理审计里提到的「计划 04 与计划 05 自查表宜统一为逐 Task 累计」——这是格式建议，不是阻断错误，本计划**不修**，留到下一次计划体检。

## 8. 交付物

完成本计划后：

- 跑 `pytest tests/golden/`：stub 模式 26 项全绿（CI 门禁）。
- 跑 `DATA_AGENT_GOLDEN_MODE=real pytest tests/golden/`：在有 API key 的环境下看到 XFAIL 报告。
- 跑 `pytest`：全仓库原有测试不变全绿（计划 01~05 的所有断言继续通过）。
- 7 处跨计划引用硬伤已就地修复，下一位实施者照计划实施不会被卡住。
- `app/core/clock.py` 是唯一的 wall clock 出口；前序计划如有用 `datetime.now()` 的地方（计划 04 编排器、计划 04 引证）改成 `app.core.clock.now()`。

下一轮评测中心子项目启动时，直接读 `tests/golden/cases/**/*.yaml`，把它加载进评测数据库、加评分卡、加 HTML 报告——YAML 本身零改动。
