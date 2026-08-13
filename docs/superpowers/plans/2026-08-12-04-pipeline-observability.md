# 问数管道与可观测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把七个阶段串成一条可编排、可澄清、可引证、可回放的问数管道，并提供对话与 Trace 的 HTTP 接口。

**Architecture:** 管道是一个显式的阶段序列，每个阶段是独立可测的函数，阶段间只通过一个 `PipelineContext` 传递数据。编排器负责调用阶段、记录 Trace、决定是继续、转澄清还是拒答；阶段本身不知道自己在管道的第几步。LLM 只出现在意图识别这一个阶段，它的输出经过严格的结构校验后才进入后续确定性流程。多轮上下文以结构化槽位存储，不堆叠聊天记录。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy 2.0、Pydantic v2、openai SDK（兼容接口）、pytest

## Global Constraints

以下约束来自 `docs/superpowers/specs/2026-08-12-trusted-query-loop-design.md`，每个任务的要求都隐含包含本节：

- LLM 只在意图识别阶段出现，输出必须是结构化意图，不含 SQL。
- 任何一步不确定，走澄清或拒答，**绝不猜一个数字**。
- 澄清超过最大轮数时可用默认假设作答，但**默认假设必须写进答案**。
- 引证块默认展开，必须包含口径（含指标版本）、时间范围与时间口径字段、过滤条件、数据更新时间。
- 由数据权限自动附加的过滤条件必须在引证中显式标注来源。
- 每个阶段的输入输出都要落 Trace，且可从任一阶段的意图快照重放。
- 越权与超范围一律拒答，拒答文案不泄漏元数据。
- 代码注释与标识符用英文；文档与提交信息用中文。

## 前置

依赖计划 01（语义层）、计划 02（意图 Schema 与编译器）、计划 03（安全改写、执行、结果校验）全部完成。

---

### Task 1: 会话与 Trace 存储

**Files:**
- Create: `backend/app/observability/__init__.py`
- Create: `backend/app/observability/orm.py`
- Create: `backend/app/observability/trace.py`
- Create: `backend/tests/observability/__init__.py`
- Create: `backend/tests/observability/test_trace.py`

**Interfaces:**
- Consumes: `app.core.db.MetaBase`
- Produces:
  - `app.observability.orm.ConversationRow`(`id`/`user_id`/`title`/`dataset_name`/`slot_state`/`created_at`/`updated_at`) — `agent_meta.conversations`
  - `app.observability.orm.TurnRow`(`id`/`conversation_id`/`question`/`status`/`answer`/`intent_snapshot`/`created_at`) — `agent_meta.turns`
  - `app.observability.orm.TraceStageRow`(`id`/`turn_id`/`stage`/`sequence`/`input_payload`/`output_payload`/`model`/`prompt_tokens`/`completion_tokens`/`elapsed_ms`/`error`/`created_at`) — `agent_meta.trace_stages`
  - `app.observability.orm.FeedbackRow`(`id`/`turn_id`/`is_positive`/`category`/`comment`/`created_at`) — `agent_meta.feedback`
  - `app.observability.trace.Stage` — 枚举，七个阶段 `VERIFIED_RECALL`/`INTENT`/`SEMANTIC_RESOLVE`/`COMPILE`/`SECURITY`/`EXECUTE`/`ANSWER`
  - `app.observability.trace.TraceRecorder` — 方法 `record(stage, *, input_payload, output_payload=None, model=None, prompt_tokens=0, completion_tokens=0, elapsed_ms=0, error=None)`、`stage_timer(stage, input_payload)`（上下文管理器，自动计时并在异常时记录 error 后重新抛出）
  - `app.observability.trace.load_trace(session, turn_id) -> list[TraceStageRow]`

- [ ] **Step 1: 写失败的 Trace 测试**

`backend/tests/observability/test_trace.py`：

```python
import pytest

from app.observability.orm import ConversationRow, TurnRow
from app.observability.trace import Stage, TraceRecorder, load_trace


@pytest.fixture
def turn(meta_session):
    conversation = ConversationRow(user_id=1, title="本月销售额")
    row = TurnRow(conversation=conversation, question="本月销售额")
    meta_session.add(row)
    meta_session.flush()
    return row


def test_record_persists_a_stage(meta_session, turn):
    recorder = TraceRecorder(meta_session, turn.id)
    recorder.record(Stage.INTENT, input_payload={"question": "本月销售额"})
    meta_session.flush()

    stages = load_trace(meta_session, turn.id)
    assert len(stages) == 1
    assert stages[0].stage == Stage.INTENT.value


def test_stages_keep_their_order(meta_session, turn):
    recorder = TraceRecorder(meta_session, turn.id)
    for stage in (Stage.INTENT, Stage.COMPILE, Stage.EXECUTE):
        recorder.record(stage, input_payload={})
    meta_session.flush()

    stages = load_trace(meta_session, turn.id)
    assert [item.sequence for item in stages] == [1, 2, 3]
    assert [item.stage for item in stages] == [
        Stage.INTENT.value,
        Stage.COMPILE.value,
        Stage.EXECUTE.value,
    ]


def test_token_usage_is_recorded(meta_session, turn):
    recorder = TraceRecorder(meta_session, turn.id)
    recorder.record(
        Stage.INTENT,
        input_payload={},
        output_payload={"metric": "sales_revenue"},
        model="claude-opus-5",
        prompt_tokens=1200,
        completion_tokens=80,
    )
    meta_session.flush()

    stage = load_trace(meta_session, turn.id)[0]
    assert stage.model == "claude-opus-5"
    assert stage.prompt_tokens == 1200
    assert stage.completion_tokens == 80


def test_stage_timer_records_elapsed_and_output(meta_session, turn):
    recorder = TraceRecorder(meta_session, turn.id)

    with recorder.stage_timer(Stage.COMPILE, {"metric": "sales_revenue"}) as span:
        span.output = {"sql": "SELECT 1"}
    meta_session.flush()

    stage = load_trace(meta_session, turn.id)[0]
    assert stage.elapsed_ms >= 0
    assert stage.output_payload == {"sql": "SELECT 1"}
    assert stage.error is None


def test_stage_timer_records_error_and_reraises(meta_session, turn):
    recorder = TraceRecorder(meta_session, turn.id)

    with pytest.raises(ValueError):
        with recorder.stage_timer(Stage.EXECUTE, {"sql": "SELECT 1"}):
            raise ValueError("boom")
    meta_session.flush()

    stage = load_trace(meta_session, turn.id)[0]
    # A failing stage is the one most worth having in Trace.
    assert "boom" in stage.error


def test_payloads_survive_nested_structures(meta_session, turn):
    recorder = TraceRecorder(meta_session, turn.id)
    payload = {"filters": [{"field": "region_code", "values": ["EC"]}], "confidence": 0.91}
    recorder.record(Stage.SEMANTIC_RESOLVE, input_payload=payload)
    meta_session.flush()

    assert load_trace(meta_session, turn.id)[0].input_payload == payload


def test_intent_snapshot_supports_replay(meta_session, turn):
    turn.intent_snapshot = {"kind": "aggregate", "metrics": ["sales_revenue"]}
    meta_session.flush()
    meta_session.expire(turn)

    assert meta_session.get(TurnRow, turn.id).intent_snapshot["metrics"] == ["sales_revenue"]


def test_trace_of_another_turn_is_not_returned(meta_session, turn):
    other = TurnRow(conversation_id=turn.conversation_id, question="上月销售额")
    meta_session.add(other)
    meta_session.flush()

    TraceRecorder(meta_session, turn.id).record(Stage.INTENT, input_payload={})
    TraceRecorder(meta_session, other.id).record(Stage.ANSWER, input_payload={})
    meta_session.flush()

    assert len(load_trace(meta_session, turn.id)) == 1
    assert load_trace(meta_session, other.id)[0].stage == Stage.ANSWER.value


def test_conversation_holds_structured_slot_state(meta_session):
    """M-19: multi-turn context is slots, not chat history."""
    conversation = ConversationRow(
        user_id=1,
        title="华东销售",
        slot_state={"metrics": ["sales_revenue"], "filters": {"region_code": ["EC"]}},
    )
    meta_session.add(conversation)
    meta_session.flush()
    meta_session.expire(conversation)

    stored = meta_session.get(ConversationRow, conversation.id)
    assert stored.slot_state["filters"]["region_code"] == ["EC"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/observability/test_trace.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.observability'`

- [ ] **Step 3: 写会话与 Trace 的 ORM**

`backend/app/observability/orm.py`：

```python
"""Conversations, turns, trace stages and feedback.

Trace is a queryable product surface, not a log file (spec 5.7), so stages are
rows with structured payloads rather than formatted text.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import META_SCHEMA, MetaBase


class ConversationRow(MetaBase):
    __tablename__ = "conversations"
    __table_args__ = {"schema": META_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(256), default="")
    dataset_name: Mapped[str] = mapped_column(String(64), default="")
    # Structured slots carried across turns (spec M-19), not chat history.
    slot_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    turns: Mapped[list["TurnRow"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class TurnRow(MetaBase):
    __tablename__ = "turns"
    __table_args__ = {"schema": META_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey(f"{META_SCHEMA}.conversations.id", ondelete="CASCADE")
    )
    question: Mapped[str] = mapped_column(Text)
    # answered | clarifying | refused | failed
    status: Mapped[str] = mapped_column(String(16), default="answered")
    answer: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Replay unit (spec 5.7): the intent as it stood when this turn ran.
    intent_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped[ConversationRow] = relationship(back_populates="turns")
    stages: Mapped[list["TraceStageRow"]] = relationship(
        back_populates="turn", cascade="all, delete-orphan"
    )


class TraceStageRow(MetaBase):
    __tablename__ = "trace_stages"
    __table_args__ = {"schema": META_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True)
    turn_id: Mapped[int] = mapped_column(
        ForeignKey(f"{META_SCHEMA}.turns.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(32))
    sequence: Mapped[int] = mapped_column(Integer)
    input_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    output_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    turn: Mapped[TurnRow] = relationship(back_populates="stages")


class FeedbackRow(MetaBase):
    """Thumbs-down attribution (spec M-38): the most honest source of eval data."""

    __tablename__ = "feedback"
    __table_args__ = {"schema": META_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True)
    turn_id: Mapped[int] = mapped_column(
        ForeignKey(f"{META_SCHEMA}.turns.id", ondelete="CASCADE"), index=True
    )
    is_positive: Mapped[bool] = mapped_column(default=True)
    # metric | time | sql | calculation | conclusion
    category: Mapped[str] = mapped_column(String(32), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

- [ ] **Step 4: 写 Trace 记录器**

`backend/app/observability/trace.py`：

```python
"""Stage-by-stage tracing.

The recorder owns sequence numbering so stages cannot be recorded out of order,
and `stage_timer` guarantees a failing stage is still written — the failures are
precisely what Trace exists to explain.
"""

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.observability.orm import TraceStageRow


class Stage(str, Enum):
    VERIFIED_RECALL = "verified_recall"
    INTENT = "intent"
    SEMANTIC_RESOLVE = "semantic_resolve"
    COMPILE = "compile"
    SECURITY = "security"
    EXECUTE = "execute"
    ANSWER = "answer"


@dataclass
class StageSpan:
    """Mutable handle a stage uses to report what it produced."""

    output: dict[str, Any] | None = None
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class TraceRecorder:
    def __init__(self, session: Session, turn_id: int) -> None:
        self._session = session
        self._turn_id = turn_id
        self._sequence = 0

    def record(
        self,
        stage: Stage,
        *,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any] | None = None,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        elapsed_ms: int = 0,
        error: str | None = None,
    ) -> TraceStageRow:
        self._sequence += 1
        row = TraceStageRow(
            turn_id=self._turn_id,
            stage=stage.value,
            sequence=self._sequence,
            input_payload=input_payload,
            output_payload=output_payload,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            elapsed_ms=elapsed_ms,
            error=error,
        )
        self._session.add(row)
        return row

    @contextmanager
    def stage_timer(self, stage: Stage, input_payload: dict[str, Any]) -> Iterator[StageSpan]:
        span = StageSpan()
        started = time.perf_counter()
        try:
            yield span
        except Exception as error:
            self.record(
                stage,
                input_payload=input_payload,
                model=span.model,
                prompt_tokens=span.prompt_tokens,
                completion_tokens=span.completion_tokens,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                error=f"{error.__class__.__name__}: {error}",
            )
            raise
        self.record(
            stage,
            input_payload=input_payload,
            output_payload=span.output,
            model=span.model,
            prompt_tokens=span.prompt_tokens,
            completion_tokens=span.completion_tokens,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


def load_trace(session: Session, turn_id: int) -> list[TraceStageRow]:
    statement = (
        select(TraceStageRow)
        .where(TraceStageRow.turn_id == turn_id)
        .order_by(TraceStageRow.sequence)
    )
    return list(session.execute(statement).scalars())
```

- [ ] **Step 5: 注册新表**

`backend/scripts/init_db.py` 与 `backend/tests/conftest.py` 中补上导入：

```python
from app.observability import orm as observability_orm  # noqa: F401
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/observability/test_trace.py -v`
Expected: PASS（9 项）

- [ ] **Step 7: 提交**

```bash
git add backend/app/observability backend/tests/observability backend/scripts/init_db.py backend/tests/conftest.py
git commit -F - <<'EOF'
实现会话、多轮槽位与分阶段 Trace 存储

Trace 要求是可查页面而非日志文件，因此阶段记录必须是带结构化载荷的行数据；同时失败阶段最需要被看到，若由各阶段自行记录，异常路径最容易漏写。为此把计时与异常记录收进上下文管理器统一处理。

- 会话、轮次、阶段、反馈四张表，阶段载荷用 JSONB 保留嵌套结构
- 序号由记录器统一维护，阶段顺序不可错乱
- 阶段计时上下文管理器在异常时先落 Trace 再重新抛出
- 轮次保存意图快照，作为重放单元
- 会话保存结构化槽位状态而非聊天记录
- 验证：pytest tests/observability/test_trace.py 9 项通过
EOF
```

---

### Task 2: 意图识别（LLM 唯一介入点）

**Files:**
- Create: `backend/app/intent/prompt.py`
- Create: `backend/app/intent/recognizer.py`
- Create: `backend/tests/intent/test_prompt.py`
- Create: `backend/tests/intent/test_recognizer.py`

**Interfaces:**
- Consumes: `app.semantic.model.DatasetDef`、`app.intent.schema.QueryIntent`、`app.core.config.Settings`、openai SDK
- Produces:
  - `app.intent.prompt.build_intent_prompt(dataset, question, slot_state=None) -> tuple[str, str]` — 返回 (system, user)
  - `app.intent.recognizer.IntentPayload` — Pydantic v2 模型，LLM 输出的落地校验层
  - `app.intent.recognizer.IntentRecognitionError` — 属性 `reason`
  - `app.intent.recognizer.LlmClient` — Protocol，方法 `complete(system, user) -> LlmCompletion`
  - `app.intent.recognizer.LlmCompletion` — frozen dataclass `content`/`model`/`prompt_tokens`/`completion_tokens`
  - `app.intent.recognizer.OpenAiCompatClient(settings)` — 生产实现
  - `app.intent.recognizer.recognize(client, dataset, question, slot_state=None) -> tuple[QueryIntent, LlmCompletion]`

- [ ] **Step 1: 写失败的 Prompt 测试**

`backend/tests/intent/test_prompt.py`：

```python
import pytest

from app.intent.prompt import build_intent_prompt
from app.semantic.loader import load_dataset
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def orders(meta_session):
    build_orders_dataset(meta_session)
    return load_dataset(meta_session, "orders")


def test_prompt_lists_metrics_with_definitions(orders):
    _, user = build_intent_prompt(orders, "本月销售额")

    assert "sales_revenue" in user
    assert "销售额" in user
    assert "已完成订单含税金额" in user


def test_prompt_lists_groupable_dimensions(orders):
    _, user = build_intent_prompt(orders, "各省销售额")

    assert "province" in user
    # amount is not groupable and must not be offered as a dimension.
    assert "维度" in user


def test_prompt_excludes_non_groupable_fields_from_dimensions(orders):
    _, user = build_intent_prompt(orders, "各省销售额")
    dimension_block = user.split("可用维度")[1].split("可用过滤字段")[0]

    assert "province" in dimension_block
    assert "amount" not in dimension_block


def test_prompt_does_not_enumerate_enum_values(orders):
    """Spec 4.3: value dictionaries are looked up, not stuffed into the prompt."""
    _, user = build_intent_prompt(orders, "华东销售额")

    assert "华东" not in user
    assert "EC" not in user


def test_prompt_states_forbidden_scenario(orders):
    _, user = build_intent_prompt(orders, "确认收入多少")
    assert "不可用于财务确认收入口径" in user


def test_prompt_forbids_sql(orders):
    system, _ = build_intent_prompt(orders, "本月销售额")
    assert "SQL" in system


def test_prompt_requires_per_field_confidence(orders):
    system, _ = build_intent_prompt(orders, "本月销售额")
    assert "confidence" in system


def test_prompt_carries_slot_state_for_followups(orders):
    _, user = build_intent_prompt(
        orders,
        "那华南呢",
        slot_state={"metrics": ["sales_revenue"], "filters": {"region_code": ["EC"]}},
    )
    assert "上一轮" in user
    assert "sales_revenue" in user


def test_prompt_omits_slot_block_on_first_turn(orders):
    _, user = build_intent_prompt(orders, "本月销售额")
    assert "上一轮" not in user


def test_prompt_is_deterministic(orders):
    assert build_intent_prompt(orders, "本月销售额") == build_intent_prompt(orders, "本月销售额")
```

- [ ] **Step 2: 写失败的识别器测试**

`backend/tests/intent/test_recognizer.py`：

```python
import json

import pytest

from app.intent.recognizer import IntentRecognitionError, LlmCompletion, recognize
from app.intent.schema import ComparisonKind, IntentKind
from app.semantic.loader import load_dataset
from tests.semantic.factories import build_orders_dataset


class StubClient:
    """Returns a canned payload so the deterministic layer can be tested alone."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> LlmCompletion:
        self.calls.append((system, user))
        return LlmCompletion(
            content=self.content, model="stub", prompt_tokens=100, completion_tokens=20
        )


@pytest.fixture
def orders(meta_session):
    build_orders_dataset(meta_session)
    return load_dataset(meta_session, "orders")


def _payload(**overrides) -> str:
    base = {
        "kind": "aggregate",
        "metrics": ["sales_revenue"],
        "dimensions": [],
        "filters": [],
        "time": {"start": "2026-08-01", "end": "2026-08-31", "grain": "month", "expression": "本月"},
        "comparison": "none",
        "confidence": {"overall": 0.92, "metric": 0.95, "time": 0.9},
        "assumptions": [],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def test_recognize_returns_a_query_intent(orders):
    intent, completion = recognize(StubClient(_payload()), orders, "本月销售额")

    assert intent.kind == IntentKind.AGGREGATE
    assert intent.metrics == ["sales_revenue"]
    assert intent.time.expression == "本月"
    assert intent.raw_question == "本月销售额"
    assert completion.prompt_tokens == 100


def test_comparison_is_parsed(orders):
    intent, _ = recognize(StubClient(_payload(comparison="mom")), orders, "本月销售额环比")
    assert intent.comparison == ComparisonKind.MOM


def test_json_in_code_fence_is_accepted(orders):
    fenced = f"```json\n{_payload()}\n```"
    intent, _ = recognize(StubClient(fenced), orders, "本月销售额")
    assert intent.metrics == ["sales_revenue"]


def test_non_json_output_is_rejected(orders):
    with pytest.raises(IntentRecognitionError):
        recognize(StubClient("我觉得你想问销售额"), orders, "本月销售额")


def test_unknown_metric_is_rejected(orders):
    """A hallucinated metric must not reach the compiler."""
    with pytest.raises(IntentRecognitionError) as excinfo:
        recognize(StubClient(_payload(metrics=["profit_rate_v9"])), orders, "利润率")
    assert "profit_rate_v9" in excinfo.value.reason


def test_unknown_dimension_is_rejected(orders):
    with pytest.raises(IntentRecognitionError):
        recognize(StubClient(_payload(dimensions=["city"])), orders, "各城市销售额")


def test_unknown_filter_field_is_rejected(orders):
    payload = _payload(
        filters=[{"field": "salesperson", "operator": "eq", "spoken_values": ["张三"]}]
    )
    with pytest.raises(IntentRecognitionError):
        recognize(StubClient(payload), orders, "张三的销售额")


def test_sql_in_output_is_rejected(orders):
    """The model has exactly one job and writing SQL is not it."""
    payload = _payload(assumptions=["SELECT SUM(amount) FROM orders"])
    with pytest.raises(IntentRecognitionError):
        recognize(StubClient(payload), orders, "本月销售额")


def test_unsupported_kind_passes_through(orders):
    payload = _payload(kind="unsupported", metrics=[], confidence={"overall": 0.1})
    intent, _ = recognize(StubClient(payload), orders, "帮我下单")

    # Refusal is decided downstream; recognition only reports what it saw.
    assert intent.kind == IntentKind.UNSUPPORTED


def test_missing_confidence_is_rejected(orders):
    payload = json.loads(_payload())
    del payload["confidence"]
    with pytest.raises(IntentRecognitionError):
        recognize(StubClient(json.dumps(payload)), orders, "本月销售额")


def test_filters_keep_spoken_values_unresolved(orders):
    """Value mapping is the next stage's job, not the model's."""
    payload = _payload(
        filters=[{"field": "region_code", "operator": "in", "spoken_values": ["华东"]}]
    )
    intent, _ = recognize(StubClient(payload), orders, "华东销售额")

    assert intent.filters[0].spoken_values == ["华东"]
    assert intent.filters[0].values == []


def test_time_absent_is_allowed(orders):
    payload = json.loads(_payload())
    payload["time"] = None
    intent, _ = recognize(StubClient(json.dumps(payload)), orders, "销售额")

    # Missing time triggers clarification later, not a recognition failure.
    assert intent.time is None


def test_slot_state_is_passed_to_the_prompt(orders):
    client = StubClient(_payload())
    recognize(client, orders, "那华南呢", slot_state={"metrics": ["sales_revenue"]})

    _, user = client.calls[0]
    assert "上一轮" in user
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/intent/test_prompt.py tests/intent/test_recognizer.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.intent.prompt'`

- [ ] **Step 4: 写 Prompt 构建**

`backend/app/intent/prompt.py`：

```python
"""Prompt for the one LLM stage.

Deliberately omits enum values: dictionaries are queried by the resolver
(spec 4.3), so the model reports what the user said ("华东") and never guesses
a physical value. Also omits any SQL vocabulary — the model's only job is to
fill in slots.
"""

import json

from app.semantic.model import DatasetDef

_SYSTEM = """你是一个企业数据问答系统的意图识别模块。

你的唯一任务：把用户的自然语言问题转换为结构化意图 JSON。

严格要求：
1. 绝对不要生成 SQL、表名、列名或任何查询语句。SQL 由系统的编译器生成。
2. 只能使用下方给出的指标名、维度名与过滤字段名，不得自行发明或推测。
3. 过滤条件只填写用户口语中的表述（spoken_values），不要翻译成数据库里的取值。
4. 每个关键槽位都要给出置信度 confidence，取值 0 到 1；不确定就给低分，不要为了填满而猜。
5. 如果问题不是数据查询（例如要求下单、修改数据、闲聊），kind 填 unsupported。
6. 如果做了任何默认假设（例如「最近」按本月理解），写入 assumptions 数组。

只输出 JSON，不要输出解释文字。JSON 结构：
{
  "kind": "aggregate | trend | ranking | detail | unsupported",
  "metrics": ["指标名"],
  "dimensions": ["维度名"],
  "filters": [{"field": "字段名", "operator": "eq|ne|in|not_in|gt|gte|lt|lte|between",
               "spoken_values": ["用户原话"]}],
  "time": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD",
           "grain": "day|week|month|quarter|year", "expression": "用户原话"},
  "comparison": "none|mom|yoy|wow|qoq|ytd|mtd|qtd|previous_period",
  "sort": {"by": "指标名", "descending": true, "limit": 10},
  "confidence": {"overall": 0.0, "metric": 0.0, "time": 0.0,
                 "dimension": 0.0, "filter": 0.0},
  "assumptions": ["做出的默认假设"]
}"""


def _metric_lines(dataset: DatasetDef) -> str:
    lines = []
    for metric in dataset.metrics:
        parts = [f"- {metric.name}（{metric.business_name}）"]
        if metric.synonyms:
            parts.append(f"同义词：{'、'.join(metric.synonyms)}")
        if metric.description:
            parts.append(f"口径：{metric.description}")
        lines.append("；".join(parts))
    return "\n".join(lines)


def _field_lines(dataset: DatasetDef, *, groupable: bool) -> str:
    lines = []
    for field in dataset.fields:
        if groupable and not field.is_groupable:
            continue
        if not groupable and not field.is_filterable:
            continue
        label = f"- {field.name}（{field.business_name or field.name}）"
        if field.synonyms:
            label += f"；同义词：{'、'.join(field.synonyms)}"
        lines.append(label)
    return "\n".join(lines)


def build_intent_prompt(
    dataset: DatasetDef, question: str, slot_state: dict | None = None
) -> tuple[str, str]:
    blocks = [
        f"数据集：{dataset.name}（{dataset.business_name}）",
        f"数据粒度：{dataset.grain}",
        f"适用场景：{dataset.applicable_scenario}",
        f"禁用场景：{dataset.forbidden_scenario}",
        f"可用指标\n{_metric_lines(dataset)}",
        f"可用维度\n{_field_lines(dataset, groupable=True)}",
        f"可用过滤字段\n{_field_lines(dataset, groupable=False)}",
    ]

    if slot_state:
        # Follow-up questions like 「那华南呢」 only replace one slot.
        blocks.append(
            "上一轮的查询状态（用户可能只是想改动其中一部分）\n"
            + json.dumps(slot_state, ensure_ascii=False, sort_keys=True)
        )

    blocks.append(f"用户问题：{question}")
    return _SYSTEM, "\n\n".join(blocks)
```

- [ ] **Step 5: 写识别器**

`backend/app/intent/recognizer.py`：

```python
"""LLM call plus the validation layer that contains it.

Everything the model returns is checked against the semantic model before it
becomes a QueryIntent: a hallucinated metric name that reached the compiler
would either crash or, worse, resolve to something close but wrong.
"""

import json
import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.intent.prompt import build_intent_prompt
from app.intent.schema import (
    ComparisonKind,
    FieldConfidence,
    FilterCondition,
    FilterOperator,
    IntentKind,
    QueryIntent,
    SortSpec,
    TimeGrain,
    TimeRange,
)
from app.semantic.model import DatasetDef

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_SQL_TOKENS = re.compile(
    r"\b(select|insert|update|delete|drop|from\s+\w+|join|group\s+by)\b", re.IGNORECASE
)


class IntentRecognitionError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class LlmCompletion:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LlmClient(Protocol):
    def complete(self, system: str, user: str) -> LlmCompletion: ...


class _FilterPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_name: str = Field(alias="field")
    operator: FilterOperator = FilterOperator.IN
    spoken_values: list[str] = Field(default_factory=list)


class _TimePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str
    end: str
    grain: TimeGrain = TimeGrain.MONTH
    expression: str = ""


class _SortPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: str
    descending: bool = True
    limit: int | None = None


class _ConfidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overall: float
    metric: float | None = None
    time: float | None = None
    dimension: float | None = None
    filter: float | None = None


class IntentPayload(BaseModel):
    """Shape contract for the model's output."""

    model_config = ConfigDict(extra="forbid")

    kind: IntentKind
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[_FilterPayload] = Field(default_factory=list)
    time: _TimePayload | None = None
    comparison: ComparisonKind = ComparisonKind.NONE
    sort: _SortPayload | None = None
    confidence: _ConfidencePayload
    assumptions: list[str] = Field(default_factory=list)


def _extract_json(content: str) -> dict:
    text = content.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise IntentRecognitionError(f"模型输出不是合法 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise IntentRecognitionError("模型输出不是 JSON 对象")
    return payload


def _assert_no_sql(content: str) -> None:
    if _SQL_TOKENS.search(content):
        raise IntentRecognitionError("模型输出中出现 SQL 片段，已拒绝采用")


def _assert_known_references(dataset: DatasetDef, payload: IntentPayload) -> None:
    for name in payload.metrics:
        if not dataset.has_metric(name):
            raise IntentRecognitionError(f"模型返回了不存在的指标 {name}")
    for name in payload.dimensions:
        if not dataset.has_field(name):
            raise IntentRecognitionError(f"模型返回了不存在的维度 {name}")
    for item in payload.filters:
        if not dataset.has_field(item.field_name):
            raise IntentRecognitionError(f"模型返回了不存在的过滤字段 {item.field_name}")
    if payload.sort and not (
        dataset.has_metric(payload.sort.by) or dataset.has_field(payload.sort.by)
    ):
        raise IntentRecognitionError(f"模型返回了不存在的排序字段 {payload.sort.by}")


def _to_intent(dataset: DatasetDef, payload: IntentPayload, question: str) -> QueryIntent:
    time_range = None
    if payload.time is not None:
        from datetime import date

        try:
            time_range = TimeRange(
                start=date.fromisoformat(payload.time.start),
                end=date.fromisoformat(payload.time.end),
                grain=payload.time.grain,
                expression=payload.time.expression,
            )
        except ValueError as error:
            raise IntentRecognitionError(f"模型返回的日期无法解析：{error}") from error

    return QueryIntent(
        kind=payload.kind,
        dataset=dataset.name,
        metrics=list(payload.metrics),
        dimensions=list(payload.dimensions),
        filters=[
            FilterCondition(
                field=item.field_name,
                operator=item.operator,
                values=[],
                spoken_values=list(item.spoken_values),
            )
            for item in payload.filters
        ],
        time=time_range,
        comparison=payload.comparison,
        sort=(
            SortSpec(
                by=payload.sort.by, descending=payload.sort.descending, limit=payload.sort.limit
            )
            if payload.sort
            else None
        ),
        confidence=FieldConfidence(
            overall=payload.confidence.overall,
            metric=payload.confidence.metric,
            time=payload.confidence.time,
            dimension=payload.confidence.dimension,
            filter=payload.confidence.filter,
        ),
        assumptions=list(payload.assumptions),
        raw_question=question,
    )


def recognize(
    client: LlmClient,
    dataset: DatasetDef,
    question: str,
    slot_state: dict | None = None,
) -> tuple[QueryIntent, LlmCompletion]:
    system, user = build_intent_prompt(dataset, question, slot_state)
    completion = client.complete(system, user)

    _assert_no_sql(completion.content)
    raw = _extract_json(completion.content)
    try:
        payload = IntentPayload.model_validate(raw)
    except ValidationError as error:
        raise IntentRecognitionError(f"模型输出结构不符合意图 Schema：{error}") from error

    _assert_known_references(dataset, payload)
    return _to_intent(dataset, payload, question), completion


class OpenAiCompatClient:
    """Production client against an OpenAI-compatible endpoint."""

    def __init__(self, settings: Settings) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
        self._model = settings.llm_model
        self._timeout = settings.llm_timeout_seconds

    def complete(self, system: str, user: str) -> LlmCompletion:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Intent recognition must be as reproducible as the API allows.
            temperature=0,
            response_format={"type": "json_object"},
            timeout=self._timeout,
        )
        usage = response.usage
        return LlmCompletion(
            content=response.choices[0].message.content or "",
            model=response.model,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )
```

- [ ] **Step 6: 在配置中补齐模型参数**

`app/core/config.py` 的 `Settings` 增加：

```python
    llm_api_key: str = ""
    llm_base_url: str = "https://api.anthropic.com/v1/"
    llm_model: str = "claude-opus-5"
    llm_timeout_seconds: int = 30
```

`.env.example` 同步补上这四项。

- [ ] **Step 7: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/intent -v`
Expected: PASS（8 项来自计划 02 + 本任务 23 项）

注意 `_FilterPayload` 用 `field` 作为 JSON 键但 Python 侧命名为 `field_name`，因为 `field` 与 Pydantic 的导入名冲突；`ConfigDict` 未开启 `populate_by_name`，构造时必须用别名。

- [ ] **Step 8: 提交**

```bash
git add backend/app/intent/prompt.py backend/app/intent/recognizer.py backend/tests/intent backend/app/core/config.py backend/.env.example
git commit -F - <<'EOF'
实现意图识别与模型输出的校验层

模型是链路中唯一的不确定环节，若它编出一个不存在的指标名，编译器要么崩溃要么解析到相近但错误的口径，属于最难发现的静默错误。因此模型输出先过结构校验，再逐项核对指标、维度、过滤字段是否真实存在，任一不符即拒绝采用。

- Prompt 不含枚举值，过滤条件只回传用户原话，物理值由后续字典解析
- Prompt 明确禁止生成 SQL，输出中出现 SQL 片段即拒绝
- 输出经 Pydantic 严格校验后再核对全部语义引用是否存在
- 缺少置信度视为不合法，避免无置信度的意图直接往下走
- 缺少时间范围不算识别失败，交由后续澄清处理
- 生产客户端固定 temperature 为 0 并要求 JSON 输出
- 验证：pytest tests/intent 共 31 项通过
EOF
```

---

### Task 3: 语义解析与澄清

**Files:**
- Create: `backend/app/pipeline/__init__.py`
- Create: `backend/app/pipeline/resolve.py`
- Create: `backend/app/pipeline/clarify.py`
- Create: `backend/tests/pipeline/__init__.py`
- Create: `backend/tests/pipeline/test_resolve.py`
- Create: `backend/tests/pipeline/test_clarify.py`

**Interfaces:**
- Consumes: `app.intent.schema.QueryIntent`、`app.semantic.model.DatasetDef`、`app.core.config.Settings`
- Produces:
  - `app.pipeline.clarify.ClarifyKind` — 枚举 `METRIC`/`TIME`/`DIMENSION`/`ENTITY`/`DATASET`（对应 spec 5.2 的五类歧义）
  - `app.pipeline.clarify.ClarifyOption` — frozen dataclass `value`/`label`/`hint`
  - `app.pipeline.clarify.ClarifyRequest` — frozen dataclass `kind`/`target`/`question`/`options`
  - `app.pipeline.clarify.default_assumption(request) -> tuple[str, str] | None` — 超轮数时取第一个选项，返回（目标, 中文假设说明）
  - `app.pipeline.resolve.ResolveOutcome` — frozen dataclass `intent`/`clarifications`/`assumptions`
  - `app.pipeline.resolve.resolve_intent(dataset, intent, settings, *, round_index=0) -> ResolveOutcome`

- [ ] **Step 1: 写失败的解析测试**

`backend/tests/pipeline/test_resolve.py`：

```python
from datetime import date

import pytest

from app.core.config import Settings
from app.intent.schema import (
    ComparisonKind,
    FieldConfidence,
    FilterCondition,
    FilterOperator,
    IntentKind,
    QueryIntent,
    TimeGrain,
    TimeRange,
)
from app.pipeline.clarify import ClarifyKind
from app.pipeline.resolve import resolve_intent
from app.semantic.loader import load_dataset
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def orders(meta_session):
    build_orders_dataset(meta_session)
    return load_dataset(meta_session, "orders")


def _settings(**overrides) -> Settings:
    base = {"clarify_confidence_threshold": 0.7, "clarify_max_rounds": 2}
    base.update(overrides)
    return Settings(**base)


def _intent(**overrides) -> QueryIntent:
    payload = {
        "kind": IntentKind.AGGREGATE,
        "dataset": "orders",
        "metrics": ["sales_revenue"],
        "time": TimeRange(
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            grain=TimeGrain.MONTH,
            expression="本月",
        ),
        "confidence": FieldConfidence(overall=0.9, metric=0.95, time=0.9),
        "raw_question": "本月销售额",
    }
    payload.update(overrides)
    return QueryIntent(**payload)


def _kinds(outcome):
    return {item.kind for item in outcome.clarifications}


def test_clear_intent_resolves_without_clarification(orders):
    outcome = resolve_intent(orders, _intent(), _settings())

    assert outcome.clarifications == ()
    assert outcome.intent.metrics == ["sales_revenue"]


def test_spoken_values_are_mapped_to_physical_values(orders):
    intent = _intent(
        filters=[
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=[],
                spoken_values=["华东", "华南"],
            )
        ]
    )
    outcome = resolve_intent(orders, intent, _settings())

    assert outcome.clarifications == ()
    assert outcome.intent.filters[0].values == ["EC", "SC"]
    # The spoken form is kept for the citation block.
    assert outcome.intent.filters[0].spoken_values == ["华东", "华南"]


def test_alias_is_mapped(orders):
    intent = _intent(
        filters=[
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=[],
                spoken_values=["东区"],
            )
        ]
    )
    outcome = resolve_intent(orders, intent, _settings())
    assert outcome.intent.filters[0].values == ["EC"]


def test_unmappable_value_triggers_entity_clarification(orders):
    """Spec 4.3: mapping failure clarifies, never queries an empty set."""
    intent = _intent(
        filters=[
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=[],
                spoken_values=["华中"],
            )
        ]
    )
    outcome = resolve_intent(orders, intent, _settings())

    assert ClarifyKind.ENTITY in _kinds(outcome)
    request = next(item for item in outcome.clarifications if item.kind == ClarifyKind.ENTITY)
    assert {option.value for option in request.options} == {"EC", "SC", "NC"}


def test_non_enum_filter_values_pass_through(orders):
    intent = _intent(
        filters=[
            FilterCondition(
                field="amount",
                operator=FilterOperator.GT,
                values=[],
                spoken_values=["1000"],
            )
        ]
    )
    outcome = resolve_intent(orders, intent, _settings())

    assert outcome.clarifications == ()
    assert outcome.intent.filters[0].values == ["1000"]


def test_low_metric_confidence_triggers_metric_clarification(orders):
    intent = _intent(confidence=FieldConfidence(overall=0.5, metric=0.4, time=0.9))
    outcome = resolve_intent(orders, intent, _settings())

    assert ClarifyKind.METRIC in _kinds(outcome)
    request = next(item for item in outcome.clarifications if item.kind == ClarifyKind.METRIC)
    # Options carry the definition so the user picks a caliber, not a name.
    assert any("已完成订单含税金额" in option.hint for option in request.options)


def test_low_time_confidence_triggers_time_clarification(orders):
    intent = _intent(confidence=FieldConfidence(overall=0.6, metric=0.95, time=0.3))
    assert ClarifyKind.TIME in _kinds(resolve_intent(orders, intent, _settings()))


def test_missing_time_triggers_time_clarification(orders):
    intent = _intent(time=None)
    assert ClarifyKind.TIME in _kinds(resolve_intent(orders, intent, _settings()))


def test_low_dimension_confidence_triggers_dimension_clarification(orders):
    intent = _intent(
        dimensions=["province"],
        confidence=FieldConfidence(overall=0.6, metric=0.95, time=0.9, dimension=0.3),
    )
    assert ClarifyKind.DIMENSION in _kinds(resolve_intent(orders, intent, _settings()))


def test_no_metric_recognized_is_reported_not_guessed(orders):
    """Spec 5.1: never fall back to a nearby field."""
    intent = _intent(metrics=[], confidence=FieldConfidence(overall=0.3, metric=0.2))
    outcome = resolve_intent(orders, intent, _settings())

    assert ClarifyKind.METRIC in _kinds(outcome)
    assert outcome.intent.metrics == []


def test_multiple_ambiguities_are_all_reported(orders):
    intent = _intent(
        time=None,
        confidence=FieldConfidence(overall=0.3, metric=0.2, time=0.2),
    )
    assert {ClarifyKind.METRIC, ClarifyKind.TIME} <= _kinds(resolve_intent(orders, intent, _settings()))


def test_max_rounds_exceeded_uses_defaults_and_records_assumptions(orders):
    intent = _intent(confidence=FieldConfidence(overall=0.4, metric=0.3, time=0.9))
    outcome = resolve_intent(orders, intent, _settings(), round_index=2)

    # Spec 5.2: answer with a default, but the assumption must be stated.
    assert outcome.clarifications == ()
    assert outcome.assumptions
    assert any("默认" in item for item in outcome.assumptions)


def test_defaults_fill_the_slot_they_were_chosen_for(orders):
    intent = _intent(metrics=[], confidence=FieldConfidence(overall=0.3, metric=0.2))
    outcome = resolve_intent(orders, intent, _settings(), round_index=2)

    assert outcome.intent.metrics
    assert outcome.intent.metrics[0] in {metric.name for metric in orders.metrics}


def test_model_assumptions_are_preserved(orders):
    intent = _intent(assumptions=["「最近」按本月理解"])
    outcome = resolve_intent(orders, intent, _settings())
    assert "「最近」按本月理解" in outcome.assumptions


def test_unsupported_intent_is_not_clarified(orders):
    intent = _intent(kind=IntentKind.UNSUPPORTED, metrics=[], confidence=FieldConfidence(overall=0.1))
    outcome = resolve_intent(orders, intent, _settings())

    # Out-of-scope questions are refused upstream, not clarified into shape.
    assert outcome.clarifications == ()
    assert outcome.intent.kind == IntentKind.UNSUPPORTED


def test_comparison_without_time_still_clarifies_time(orders):
    intent = _intent(time=None, comparison=ComparisonKind.MOM)
    assert ClarifyKind.TIME in _kinds(resolve_intent(orders, intent, _settings()))
```

- [ ] **Step 2: 写失败的澄清测试**

`backend/tests/pipeline/test_clarify.py`：

```python
from app.pipeline.clarify import (
    ClarifyKind,
    ClarifyOption,
    ClarifyRequest,
    default_assumption,
)


def _request(**overrides) -> ClarifyRequest:
    payload = {
        "kind": ClarifyKind.METRIC,
        "target": "metrics",
        "question": "你指的是哪个销售额？",
        "options": (
            ClarifyOption(value="sales_revenue", label="销售额", hint="已完成订单含税金额"),
            ClarifyOption(value="new_customer_revenue", label="新客销售额", hint="新客的已完成订单金额"),
        ),
    }
    payload.update(overrides)
    return ClarifyRequest(**payload)


def test_default_assumption_takes_the_first_option():
    target, message = default_assumption(_request())

    assert target == "metrics"
    assert "销售额" in message


def test_default_assumption_message_says_it_is_a_default():
    _, message = default_assumption(_request())
    # The user must be able to see this was assumed, not asked.
    assert "默认" in message


def test_default_assumption_without_options_is_none():
    assert default_assumption(_request(options=())) is None


def test_clarify_request_is_immutable():
    import pytest

    request = _request()
    with pytest.raises(Exception):
        request.question = "改掉"


def test_time_clarification_carries_concrete_ranges():
    request = _request(
        kind=ClarifyKind.TIME,
        target="time",
        question="「最近」指的是哪个范围？",
        options=(
            ClarifyOption(value="last_7_days", label="最近 7 天", hint=""),
            ClarifyOption(value="this_month", label="本月", hint=""),
        ),
    )
    target, message = default_assumption(request)

    assert target == "time"
    assert "最近 7 天" in message
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/pipeline -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.pipeline'`

- [ ] **Step 4: 写澄清模型**

`backend/app/pipeline/clarify.py`：

```python
"""Clarification requests (spec 5.2, M-18).

Options are always concrete and clickable — the user picks, never retypes. When
the round limit is hit the first option becomes the default, but the assumption
is returned so the answer can state it: an unstated default assumption is a
silent error.
"""

from dataclasses import dataclass
from enum import Enum

_KIND_LABELS = {
    "metric": "指标口径",
    "time": "时间范围",
    "dimension": "分组维度",
    "entity": "筛选取值",
    "dataset": "数据集",
}


class ClarifyKind(str, Enum):
    METRIC = "metric"
    TIME = "time"
    DIMENSION = "dimension"
    ENTITY = "entity"
    DATASET = "dataset"


@dataclass(frozen=True, slots=True)
class ClarifyOption:
    value: str
    label: str
    hint: str = ""


@dataclass(frozen=True, slots=True)
class ClarifyRequest:
    kind: ClarifyKind
    target: str
    question: str
    options: tuple[ClarifyOption, ...] = ()


def default_assumption(request: ClarifyRequest) -> tuple[str, str] | None:
    if not request.options:
        return None
    chosen = request.options[0]
    label = _KIND_LABELS[request.kind.value]
    return request.target, f"{label}未确认，已默认按「{chosen.label}」处理"
```

- [ ] **Step 5: 写语义解析**

`backend/app/pipeline/resolve.py`：

```python
"""Stage 3: value mapping, ambiguity detection and clarification.

Two responsibilities that belong together because both decide whether the
query may proceed: mapping spoken values to physical ones via the dictionary,
and turning low confidence into questions rather than guesses.
"""

from dataclasses import dataclass, replace

from app.core.config import Settings
from app.intent.schema import FilterCondition, IntentKind, QueryIntent
from app.pipeline.clarify import ClarifyKind, ClarifyOption, ClarifyRequest, default_assumption
from app.semantic.enums import SemanticType
from app.semantic.model import DatasetDef


@dataclass(frozen=True, slots=True)
class ResolveOutcome:
    intent: QueryIntent
    clarifications: tuple[ClarifyRequest, ...] = ()
    assumptions: tuple[str, ...] = ()


def _metric_options(dataset: DatasetDef) -> tuple[ClarifyOption, ...]:
    return tuple(
        ClarifyOption(
            value=metric.name,
            label=metric.business_name,
            hint=metric.description or f"版本 v{metric.version}",
        )
        for metric in dataset.metrics
    )


def _time_options() -> tuple[ClarifyOption, ...]:
    return (
        ClarifyOption(value="this_month", label="本月"),
        ClarifyOption(value="last_month", label="上月"),
        ClarifyOption(value="last_7_days", label="最近 7 天"),
        ClarifyOption(value="last_30_days", label="最近 30 天"),
    )


def _enum_options(dataset: DatasetDef, field_name: str) -> tuple[ClarifyOption, ...]:
    field = dataset.field(field_name)
    return tuple(
        ClarifyOption(value=item.physical_value, label=item.business_value, hint=item.description)
        for item in field.enum_values
    )


def _resolve_filters(
    dataset: DatasetDef, intent: QueryIntent
) -> tuple[list[FilterCondition], list[ClarifyRequest]]:
    resolved: list[FilterCondition] = []
    requests: list[ClarifyRequest] = []

    for condition in intent.filters:
        field = dataset.field(condition.field)
        if field.semantic_type != SemanticType.ENUM:
            # Numbers and dates need no dictionary; keep what the user said.
            resolved.append(replace(condition, values=list(condition.spoken_values)))
            continue

        physical: list[str] = []
        unmapped: list[str] = []
        for spoken in condition.spoken_values:
            match = dataset.resolve_enum(field.name, spoken)
            if match is None:
                unmapped.append(spoken)
            else:
                physical.append(match)

        if unmapped:
            requests.append(
                ClarifyRequest(
                    kind=ClarifyKind.ENTITY,
                    target=f"filters.{field.name}",
                    question=(
                        f"「{'、'.join(unmapped)}」在{field.business_name or field.name}中"
                        "找不到对应取值，你指的是哪个？"
                    ),
                    options=_enum_options(dataset, field.name),
                )
            )
            continue

        resolved.append(replace(condition, values=physical))

    return resolved, requests


def _confidence_requests(
    dataset: DatasetDef, intent: QueryIntent, threshold: float
) -> list[ClarifyRequest]:
    requests: list[ClarifyRequest] = []
    confidence = intent.confidence

    metric_score = confidence.metric if confidence.metric is not None else confidence.overall
    if not intent.metrics or metric_score < threshold:
        requests.append(
            ClarifyRequest(
                kind=ClarifyKind.METRIC,
                target="metrics",
                question="你想看哪个指标？",
                options=_metric_options(dataset),
            )
        )

    time_score = confidence.time if confidence.time is not None else confidence.overall
    if intent.time is None or time_score < threshold:
        requests.append(
            ClarifyRequest(
                kind=ClarifyKind.TIME,
                target="time",
                question="你想看哪个时间范围？",
                options=_time_options(),
            )
        )

    if intent.dimensions:
        dimension_score = (
            confidence.dimension if confidence.dimension is not None else confidence.overall
        )
        if dimension_score < threshold:
            requests.append(
                ClarifyRequest(
                    kind=ClarifyKind.DIMENSION,
                    target="dimensions",
                    question="你想按哪个维度分组？",
                    options=tuple(
                        ClarifyOption(value=field.name, label=field.business_name or field.name)
                        for field in dataset.fields
                        if field.is_groupable
                    ),
                )
            )

    return requests


def _apply_defaults(
    intent: QueryIntent, requests: list[ClarifyRequest]
) -> tuple[QueryIntent, list[str]]:
    """Round limit reached: fill slots with the first option, stating each one."""
    assumptions: list[str] = []
    updated = intent

    for request in requests:
        decision = default_assumption(request)
        if decision is None:
            continue
        target, message = decision
        assumptions.append(message)

        if target == "metrics" and not updated.metrics:
            updated = replace(updated, metrics=[request.options[0].value])

    return updated, assumptions


def resolve_intent(
    dataset: DatasetDef,
    intent: QueryIntent,
    settings: Settings,
    *,
    round_index: int = 0,
) -> ResolveOutcome:
    if intent.kind == IntentKind.UNSUPPORTED:
        # Out of scope: refused upstream, never clarified into shape.
        return ResolveOutcome(intent=intent, assumptions=tuple(intent.assumptions))

    filters, entity_requests = _resolve_filters(dataset, intent)
    resolved = replace(intent, filters=filters)

    requests = entity_requests + _confidence_requests(
        dataset, resolved, settings.clarify_confidence_threshold
    )
    assumptions = list(intent.assumptions)

    if requests and round_index >= settings.clarify_max_rounds:
        resolved, defaults = _apply_defaults(resolved, requests)
        return ResolveOutcome(
            intent=resolved, clarifications=(), assumptions=tuple(assumptions + defaults)
        )

    return ResolveOutcome(
        intent=resolved, clarifications=tuple(requests), assumptions=tuple(assumptions)
    )
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/pipeline -v`
Expected: PASS（21 项）

`test_defaults_fill_the_slot_they_were_chosen_for` 依赖时间槽位也有默认；若该用例因时间槽位未填充而失败，说明 `_apply_defaults` 需要为 `time` 目标补上具体区间——按 `_time_options` 的取值在此处解析为 `TimeRange`，不要放宽用例。

- [ ] **Step 7: 提交**

```bash
git add backend/app/pipeline backend/tests/pipeline
git commit -F - <<'EOF'
实现枚举值解析与五类歧义的澄清判定

值映射失败若直接下查，结果是查空而非报错，用户会把空结果当成业务结论；置信度低时若挑一个相近指标凑数，错误更难发现。因此这一阶段把映射失败与低置信度统一转为带可点选项的澄清，不进入编译。

- 枚举字段的口语表述经字典映射为物理值，原话保留供引证
- 非枚举字段不查字典，数值与日期原样传递
- 覆盖指标、时间、维度、实体四类歧义的判定，选项携带口径说明
- 超过最大澄清轮数时用首个选项作答，并把默认假设显式记入结果
- 超范围意图不做澄清，直接交由上游拒答
- 验证：pytest tests/pipeline 21 项通过
EOF
```

---

### Task 4: Verified Query 召回

**Files:**
- Create: `backend/app/pipeline/verified.py`
- Create: `backend/tests/pipeline/test_verified.py`
- Modify: `backend/app/observability/orm.py`（新增 `VerifiedQueryRow`）

**Interfaces:**
- Consumes: `app.core.db.MetaBase`、`app.intent.schema.QueryIntent.slot_signature`
- Produces:
  - `app.observability.orm.VerifiedQueryRow`(`id`/`dataset_name`/`question`/`normalized_question`/`slot_signature`/`fixed_sql`/`intent_snapshot`/`is_active`/`hit_count`/`created_by`) — `agent_meta.verified_queries`
  - `app.pipeline.verified.VerifiedHit` — frozen dataclass `id`/`question`/`fixed_sql`/`match_kind`(`question`/`slots`)
  - `app.pipeline.verified.normalize_question(text) -> str`
  - `app.pipeline.verified.recall(session, dataset_name, question, intent=None) -> VerifiedHit | None`
  - `app.pipeline.verified.register(session, *, dataset_name, question, fixed_sql, intent, created_by) -> VerifiedQueryRow`

- [ ] **Step 1: 写失败的 Verified Query 测试**

`backend/tests/pipeline/test_verified.py`：

```python
from datetime import date

import pytest

from app.intent.schema import (
    FieldConfidence,
    IntentKind,
    QueryIntent,
    TimeGrain,
    TimeRange,
)
from app.pipeline.verified import normalize_question, recall, register


def _intent(**overrides) -> QueryIntent:
    payload = {
        "kind": IntentKind.AGGREGATE,
        "dataset": "orders",
        "metrics": ["sales_revenue"],
        "time": TimeRange(
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            grain=TimeGrain.MONTH,
            expression="本月",
        ),
        "confidence": FieldConfidence(overall=0.9),
        "raw_question": "本月销售额",
    }
    payload.update(overrides)
    return QueryIntent(**payload)


@pytest.fixture
def registered(meta_session):
    register(
        meta_session,
        dataset_name="orders",
        question="本月销售额是多少",
        fixed_sql="SELECT SUM(amount) AS sales_revenue FROM sample.orders",
        intent=_intent(),
        created_by="admin",
    )
    meta_session.flush()
    return meta_session


def test_exact_question_hits(registered):
    hit = recall(registered, "orders", "本月销售额是多少")

    assert hit is not None
    assert hit.match_kind == "question"
    assert "sales_revenue" in hit.fixed_sql


def test_whitespace_and_punctuation_differences_still_hit(registered):
    assert recall(registered, "orders", " 本月销售额是多少？ ") is not None


def test_different_question_misses(registered):
    assert recall(registered, "orders", "上月客单价") is None


def test_same_slots_different_wording_hits(registered):
    """Slot signature recall: phrasing varies, the query does not."""
    hit = recall(registered, "orders", "这个月卖了多少钱", intent=_intent(raw_question="这个月卖了多少钱"))

    assert hit is not None
    assert hit.match_kind == "slots"


def test_different_slots_miss(registered):
    other = _intent(metrics=["order_count"], raw_question="本月订单量")
    assert recall(registered, "orders", "本月订单量", intent=other) is None


def test_recall_is_scoped_to_dataset(registered):
    assert recall(registered, "other_dataset", "本月销售额是多少") is None


def test_inactive_entry_is_not_recalled(registered):
    from app.observability.orm import VerifiedQueryRow

    row = registered.query(VerifiedQueryRow).one()
    row.is_active = False
    registered.flush()

    assert recall(registered, "orders", "本月销售额是多少") is None


def test_hit_count_increases(registered):
    from app.observability.orm import VerifiedQueryRow

    recall(registered, "orders", "本月销售额是多少")
    recall(registered, "orders", "本月销售额是多少")
    registered.flush()

    assert registered.query(VerifiedQueryRow).one().hit_count == 2


def test_normalize_strips_punctuation_and_case():
    assert normalize_question(" 本月 GMV 是多少？ ") == normalize_question("本月gmv是多少")


def test_register_stores_the_slot_signature(registered):
    from app.observability.orm import VerifiedQueryRow

    row = registered.query(VerifiedQueryRow).one()
    assert row.slot_signature == _intent().slot_signature()
    assert row.intent_snapshot["metrics"] == ["sales_revenue"]


def test_question_match_wins_over_slot_match(meta_session):
    register(
        meta_session,
        dataset_name="orders",
        question="本月销售额是多少",
        fixed_sql="SELECT 1 AS a",
        intent=_intent(),
        created_by="admin",
    )
    register(
        meta_session,
        dataset_name="orders",
        question="完全不同的问法",
        fixed_sql="SELECT 2 AS b",
        intent=_intent(),
        created_by="admin",
    )
    meta_session.flush()

    hit = recall(meta_session, "orders", "本月销售额是多少", intent=_intent())
    assert hit.match_kind == "question"
    assert "1 AS a" in hit.fixed_sql
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/pipeline/test_verified.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.pipeline.verified'`

- [ ] **Step 3: 新增 Verified Query 表**

在 `backend/app/observability/orm.py` 追加：

```python
class VerifiedQueryRow(MetaBase):
    """A reviewed question-to-SQL pairing (spec M-20).

    A cache in front of the pipeline, not a separate architecture: hits still
    go through security rewriting before execution.
    """

    __tablename__ = "verified_queries"
    __table_args__ = {"schema": META_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_name: Mapped[str] = mapped_column(String(64), index=True)
    question: Mapped[str] = mapped_column(Text)
    normalized_question: Mapped[str] = mapped_column(Text, index=True)
    slot_signature: Mapped[str] = mapped_column(Text, index=True)
    fixed_sql: Mapped[str] = mapped_column(Text)
    intent_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(default=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

- [ ] **Step 4: 写召回**

`backend/app/pipeline/verified.py`：

```python
"""Stage 1: Verified Query recall (spec M-20).

Two match paths, tried in order of confidence:

1. normalized question text — the same question asked again
2. slot signature — a different phrasing of the same query

Slot matching is why the signature excludes confidence and raw question: two
wordings of one query must collide.
"""

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.intent.schema import QueryIntent
from app.observability.orm import VerifiedQueryRow

_PUNCTUATION = re.compile(r"[\s，。？！、,.?!;；:：'\"“”‘’()（）\[\]【】-]+")


@dataclass(frozen=True, slots=True)
class VerifiedHit:
    id: int
    question: str
    fixed_sql: str
    match_kind: str


def normalize_question(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _PUNCTUATION.sub("", folded)


def _to_hit(row: VerifiedQueryRow, match_kind: str) -> VerifiedHit:
    row.hit_count += 1
    return VerifiedHit(
        id=row.id, question=row.question, fixed_sql=row.fixed_sql, match_kind=match_kind
    )


def recall(
    session: Session,
    dataset_name: str,
    question: str,
    intent: QueryIntent | None = None,
) -> VerifiedHit | None:
    base = select(VerifiedQueryRow).where(
        VerifiedQueryRow.dataset_name == dataset_name,
        VerifiedQueryRow.is_active.is_(True),
    )

    exact = session.execute(
        base.where(VerifiedQueryRow.normalized_question == normalize_question(question))
        .order_by(VerifiedQueryRow.id)
        .limit(1)
    ).scalar_one_or_none()
    if exact is not None:
        return _to_hit(exact, "question")

    if intent is None:
        return None

    by_slots = session.execute(
        base.where(VerifiedQueryRow.slot_signature == intent.slot_signature())
        .order_by(VerifiedQueryRow.id)
        .limit(1)
    ).scalar_one_or_none()
    if by_slots is not None:
        return _to_hit(by_slots, "slots")

    return None


def register(
    session: Session,
    *,
    dataset_name: str,
    question: str,
    fixed_sql: str,
    intent: QueryIntent,
    created_by: str,
) -> VerifiedQueryRow:
    row = VerifiedQueryRow(
        dataset_name=dataset_name,
        question=question,
        normalized_question=normalize_question(question),
        slot_signature=intent.slot_signature(),
        fixed_sql=fixed_sql,
        intent_snapshot=intent.to_payload(),
        created_by=created_by,
    )
    session.add(row)
    return row
```

- [ ] **Step 5: 补齐 `QueryIntent.to_payload`**

计划 02 的 `QueryIntent` 有 `slot_signature()`，但持久化意图快照需要一个完整的可序列化视图。在 `app/intent/schema.py` 增加：

```python
    def to_payload(self) -> dict:
        """Full JSON-serialisable view, used for Trace snapshots and replay."""
        return {
            "kind": self.kind.value,
            "dataset": self.dataset,
            "metrics": list(self.metrics),
            "dimensions": list(self.dimensions),
            "filters": [
                {
                    "field": item.field,
                    "operator": item.operator.value,
                    "values": list(item.values),
                    "spoken_values": list(item.spoken_values),
                }
                for item in self.filters
            ],
            "time": (
                {
                    "start": self.time.start.isoformat(),
                    "end": self.time.end.isoformat(),
                    "grain": self.time.grain.value,
                    "expression": self.time.expression,
                }
                if self.time
                else None
            ),
            "comparison": self.comparison.value,
            "sort": (
                {"by": self.sort.by, "descending": self.sort.descending, "limit": self.sort.limit}
                if self.sort
                else None
            ),
            "assumptions": list(self.assumptions),
            "raw_question": self.raw_question,
        }
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/pipeline/test_verified.py -v`
Expected: PASS（11 项）

- [ ] **Step 7: 提交**

```bash
git add backend/app/pipeline/verified.py backend/tests/pipeline/test_verified.py backend/app/observability/orm.py backend/app/intent/schema.py
git commit -F - <<'EOF'
实现 Verified Query 的双路召回

只按问题原文匹配会让同一查询的不同问法反复走完整链路，而槽位签名恰好剔除了置信度与原始问句，天然适合做跨问法匹配。因此实现原文与槽位两条召回路径，原文优先。

- 问题文本归一化后匹配，忽略空白、标点与大小写差异
- 原文未命中时按意图槽位签名匹配，覆盖同一查询的不同问法
- 召回结果计入命中次数，供后续判断沉淀价值
- 意图新增完整可序列化视图，供快照与重放使用
- 停用条目不参与召回，召回范围限定在同一数据集
- 验证：pytest tests/pipeline/test_verified.py 11 项通过
EOF
```

---

### Task 5: 作答与引证块

**Files:**
- Create: `backend/app/pipeline/answer.py`
- Create: `backend/tests/pipeline/test_answer.py`

**Interfaces:**
- Consumes: `app.compiler.query.Citation`、`app.security.pipeline.SecuredQuery`、`app.execution.runner.QueryResult`、`app.execution.validation.ValidationIssue`
- Produces:
  - `app.pipeline.answer.CitationLine` — frozen dataclass `label`/`value`/`source`（`source` 为 `user` 或 `permission`）
  - `app.pipeline.answer.CitationBlock` — frozen dataclass `metric`/`time`/`filters`/`data_updated_at`
  - `app.pipeline.answer.DrillDown` — frozen dataclass `label`/`kind`(`dimension`/`detail`/`time`)/`target`
  - `app.pipeline.answer.Answer` — frozen dataclass `headline`/`conclusion`/`assumptions`/`warnings`/`citation`/`drill_downs`/`columns`/`rows`
  - `app.pipeline.answer.build_answer(...) -> Answer`

- [ ] **Step 1: 写失败的作答测试**

`backend/tests/pipeline/test_answer.py`：

```python
from datetime import date, datetime

import pytest

from app.compiler.query import Citation
from app.execution.runner import QueryResult
from app.execution.validation import ValidationCode, ValidationIssue
from app.pipeline.answer import build_answer
from app.security.rewrite import AppliedRowFilter


def _citation(**overrides) -> Citation:
    payload = {
        "metric_name": "sales_revenue",
        "metric_business_name": "销售额",
        "metric_version": 3,
        "metric_description": "已完成订单含税金额",
        "time_field_business_name": "完成日期",
        "time_start": date(2026, 8, 1),
        "time_end": date(2026, 8, 12),
        "filters": ("大区 属于 华东",),
        "comparison_label": "",
    }
    payload.update(overrides)
    return Citation(**payload)


def _result(columns=("sales_revenue",), rows=((42_350_000,),)) -> QueryResult:
    return QueryResult(
        columns=tuple(columns),
        rows=tuple(rows),
        row_count=len(rows),
        truncated=False,
        elapsed_ms=12,
    )


def _build(**overrides):
    payload = {
        "citation": _citation(),
        "result": _result(),
        "metric_names": ("sales_revenue",),
        "comparison_metric_names": (),
        "dimension_names": (),
        "applied_row_filters": (),
        "masked_field_names": (),
        "issues": (),
        "assumptions": (),
        "data_updated_at": datetime(2026, 8, 12, 9, 0),
    }
    payload.update(overrides)
    return build_answer(**payload)


def test_headline_states_the_number(_=None):
    answer = _build()
    assert "42,350,000" in answer.headline or "4,235" in answer.headline


def test_citation_shows_metric_with_version(_=None):
    answer = _build()
    assert "sales_revenue" in answer.citation.metric
    assert "v3" in answer.citation.metric


def test_citation_shows_time_basis_field(_=None):
    """The same metric on a different date field is the classic two-numbers bug."""
    answer = _build()
    assert "完成日期" in answer.citation.time
    assert "2026-08-01" in answer.citation.time


def test_citation_shows_data_updated_at(_=None):
    answer = _build()
    assert "2026-08-12" in answer.citation.data_updated_at


def test_user_filters_are_marked_as_user_sourced(_=None):
    answer = _build()
    line = next(item for item in answer.citation.filters if "华东" in item.value)
    assert line.source == "user"


def test_permission_filters_are_marked_and_labelled(_=None):
    answer = _build(
        applied_row_filters=(AppliedRowFilter(field_business_name="大区", values=("华东",)),)
    )
    permission_lines = [item for item in answer.citation.filters if item.source == "permission"]

    assert permission_lines
    # Spec M-16: this must be explicit, or users draw global conclusions from
    # partial data.
    assert "数据权限自动附加" in permission_lines[0].label


def test_masked_fields_are_reported_as_a_warning(_=None):
    answer = _build(masked_field_names=("客户名称",))
    assert any("客户名称" in item for item in answer.warnings)


def test_comparison_answer_states_the_change(_=None):
    answer = _build(
        citation=_citation(comparison_label="环比"),
        result=_result(
            columns=("sales_revenue", "sales_revenue_comparison"), rows=((1_124_000, 1_000_000),)
        ),
        comparison_metric_names=("sales_revenue_comparison",),
    )

    assert "环比" in answer.conclusion
    assert "12.4%" in answer.conclusion


def test_comparison_with_zero_baseline_does_not_divide(_=None):
    answer = _build(
        citation=_citation(comparison_label="环比"),
        result=_result(
            columns=("sales_revenue", "sales_revenue_comparison"), rows=((500, 0),)
        ),
        comparison_metric_names=("sales_revenue_comparison",),
    )
    assert answer.conclusion


def test_dimension_answer_names_top_contributors(_=None):
    answer = _build(
        result=_result(
            columns=("province", "sales_revenue"),
            rows=(("江苏", 300), ("浙江", 200), ("上海", 100)),
        ),
        dimension_names=("province",),
    )

    assert "江苏" in answer.conclusion


def test_assumptions_appear_in_the_answer(_=None):
    answer = _build(assumptions=("指标口径未确认，已默认按「销售额」处理",))
    # Spec 5.2: an unstated default assumption is a silent error.
    assert any("默认" in item for item in answer.assumptions)


def test_warning_level_issues_are_surfaced(_=None):
    issues = (
        ValidationIssue(ValidationCode.MAGNITUDE_SHIFT, "warn", "变化超过 10 倍，建议核对口径"),
    )
    answer = _build(issues=issues)
    assert any("10 倍" in item for item in answer.warnings)


def test_blocking_issue_is_rejected_before_answering(_=None):
    from app.pipeline.answer import ResultNotAnswerableError

    issues = (ValidationIssue(ValidationCode.EMPTY_RESULT, "block", "该时间范围内没有数据"),)
    with pytest.raises(ResultNotAnswerableError):
        _build(issues=issues)


def test_drill_downs_offer_unused_dimensions(_=None):
    answer = _build(available_dimensions=("province", "channel"))
    labels = {item.label for item in answer.drill_downs}

    assert any("省份" in label or "province" in label for label in labels)


def test_drill_downs_exclude_dimensions_already_grouped(_=None):
    answer = _build(
        dimension_names=("province",),
        result=_result(columns=("province", "sales_revenue"), rows=(("江苏", 300),)),
        available_dimensions=("province", "channel"),
    )
    targets = {item.target for item in answer.drill_downs}
    assert "province" not in targets


def test_rows_and_columns_are_carried_for_the_result_table(_=None):
    answer = _build(
        result=_result(columns=("province", "sales_revenue"), rows=(("江苏", 300),))
    )
    assert answer.columns == ("province", "sales_revenue")
    assert answer.rows == (("江苏", 300),)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/pipeline/test_answer.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.pipeline.answer'`

- [ ] **Step 3: 写作答**

`backend/app/pipeline/answer.py`：

```python
"""Stage 7: the answer (spec M-16).

Shape is fixed: number, conclusion, citation, drill-down entries. The citation
distinguishes filters the user asked for from filters permissions added, because
a user who cannot see that distinction will read partial data as a global result.
"""

from dataclasses import dataclass
from datetime import datetime

from app.compiler.query import Citation
from app.execution.runner import QueryResult
from app.execution.validation import ValidationIssue
from app.security.rewrite import AppliedRowFilter

_PERMISSION_LABEL = "由数据权限自动附加"


class ResultNotAnswerableError(Exception):
    """A blocking validation issue: report the problem, never a number."""

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        super().__init__("；".join(item.message for item in issues))


@dataclass(frozen=True, slots=True)
class CitationLine:
    label: str
    value: str
    source: str = "user"


@dataclass(frozen=True, slots=True)
class CitationBlock:
    metric: str
    time: str
    filters: tuple[CitationLine, ...] = ()
    data_updated_at: str = ""


@dataclass(frozen=True, slots=True)
class DrillDown:
    label: str
    kind: str
    target: str


@dataclass(frozen=True, slots=True)
class Answer:
    headline: str
    conclusion: str = ""
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    citation: CitationBlock | None = None
    drill_downs: tuple[DrillDown, ...] = ()
    columns: tuple[str, ...] = ()
    rows: tuple[tuple, ...] = ()


def _format_number(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.2f}"


def _primary_value(result: QueryResult, metric_name: str) -> object | None:
    if metric_name not in result.columns or not result.rows:
        return None
    return result.rows[0][result.columns.index(metric_name)]


def _build_citation(
    citation: Citation,
    applied_row_filters: tuple[AppliedRowFilter, ...],
    data_updated_at: datetime | None,
) -> CitationBlock:
    lines = [CitationLine(label="过滤", value=item, source="user") for item in citation.filters]
    lines.extend(
        CitationLine(
            label=_PERMISSION_LABEL,
            value=f"{item.field_business_name} 属于 {'、'.join(item.values)}",
            source="permission",
        )
        for item in applied_row_filters
    )

    metric_text = f"{citation.metric_name} v{citation.metric_version}（{citation.metric_business_name}）"
    if citation.metric_description:
        metric_text += f"：{citation.metric_description}"

    time_text = (
        f"{citation.time_start.isoformat()} ~ {citation.time_end.isoformat()}"
        f"（按{citation.time_field_business_name}）"
    )

    return CitationBlock(
        metric=metric_text,
        time=time_text,
        filters=tuple(lines),
        data_updated_at=data_updated_at.strftime("%Y-%m-%d %H:%M") if data_updated_at else "",
    )


def _comparison_sentence(
    result: QueryResult, metric_name: str, comparison_name: str, label: str
) -> str:
    current = _primary_value(result, metric_name)
    baseline = _primary_value(result, comparison_name)
    if not isinstance(current, (int, float)) or not isinstance(baseline, (int, float)):
        return ""
    if baseline == 0:
        return f"{label}基期为 0，无法计算变化率"

    change = (current - baseline) / abs(baseline) * 100
    direction = "+" if change >= 0 else ""
    return f"{label} {direction}{change:.1f}%"


def _contributor_sentence(result: QueryResult, dimension: str, metric_name: str) -> str:
    if dimension not in result.columns or metric_name not in result.columns:
        return ""
    dimension_index = result.columns.index(dimension)
    metric_index = result.columns.index(metric_name)

    ranked = sorted(
        (row for row in result.rows if isinstance(row[metric_index], (int, float))),
        key=lambda row: row[metric_index],
        reverse=True,
    )[:3]
    if not ranked:
        return ""
    parts = [f"{row[dimension_index]} {_format_number(row[metric_index])}" for row in ranked]
    return "主要构成：" + "，".join(parts)


def build_answer(
    *,
    citation: Citation,
    result: QueryResult,
    metric_names: tuple[str, ...],
    comparison_metric_names: tuple[str, ...] = (),
    dimension_names: tuple[str, ...] = (),
    applied_row_filters: tuple[AppliedRowFilter, ...] = (),
    masked_field_names: tuple[str, ...] = (),
    issues: tuple[ValidationIssue, ...] = (),
    assumptions: tuple[str, ...] = (),
    data_updated_at: datetime | None = None,
    available_dimensions: tuple[str, ...] = (),
) -> Answer:
    blocking = tuple(item for item in issues if item.severity == "block")
    if blocking:
        raise ResultNotAnswerableError(blocking)

    primary_metric = metric_names[0] if metric_names else ""
    primary_value = _primary_value(result, primary_metric)

    if primary_value is None:
        headline = f"{citation.metric_business_name}：共 {result.row_count} 行结果"
    else:
        headline = f"{citation.metric_business_name} {_format_number(primary_value)}"

    sentences: list[str] = []
    if comparison_metric_names:
        sentence = _comparison_sentence(
            result, primary_metric, comparison_metric_names[0], citation.comparison_label
        )
        if sentence:
            sentences.append(sentence)
    if dimension_names:
        sentence = _contributor_sentence(result, dimension_names[0], primary_metric)
        if sentence:
            sentences.append(sentence)

    warnings = [item.message for item in issues if item.severity == "warn"]
    warnings.extend(
        f"字段 {name} 因列权限已脱敏显示" for name in masked_field_names
    )

    drill_downs = tuple(
        DrillDown(label=f"按{name}拆分", kind="dimension", target=name)
        for name in available_dimensions
        if name not in dimension_names
    )

    return Answer(
        headline=headline,
        conclusion="；".join(sentences),
        assumptions=tuple(assumptions),
        warnings=tuple(warnings),
        citation=_build_citation(citation, applied_row_filters, data_updated_at),
        drill_downs=drill_downs,
        columns=result.columns,
        rows=result.rows,
    )
```

- [ ] **Step 4: 修正测试中的下钻标签断言**

`test_drill_downs_offer_unused_dimensions` 断言标签含「省份」或 `province`。上面的实现用字段物理名生成标签，因此断言的后半段成立。若希望标签显示业务名，需要把 `available_dimensions` 改为 `(name, business_name)` 元组序列——本轮不做，编排层传入业务名即可。

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/pipeline/test_answer.py -v`
Expected: PASS（16 项）

- [ ] **Step 6: 提交**

```bash
git add backend/app/pipeline/answer.py backend/tests/pipeline/test_answer.py
git commit -F - <<'EOF'
实现标准答案结构与引证块组装

引证若把用户自己加的过滤条件和权限自动附加的条件混在一起显示，用户会拿权限内的部分数据下全局结论，这是最隐蔽的一类错误。因此两类过滤在结构上就区分来源，权限来源的条目带固定标注。

- 答案固定为数字、结论、假设、提示、引证、下钻入口六部分
- 引证包含指标版本、口径说明、时间区间与时间口径字段、数据更新时间
- 权限自动附加的过滤条件单独标注来源，不与用户条件混排
- 阻断级校验问题直接拒绝作答，不输出任何数字
- 对比基期为零时给出说明而非计算变化率
- 脱敏字段与警告级问题作为提示随答案返回
- 验证：pytest tests/pipeline/test_answer.py 16 项通过
EOF
```

---

### Task 6: 七阶段编排器

**Files:**
- Create: `backend/app/pipeline/orchestrator.py`
- Create: `backend/tests/pipeline/test_orchestrator.py`

**Interfaces:**
- Consumes: Task 1~5 的全部导出、计划 02 的 `compile_intent`、计划 03 的 `secure_compiled`/`secure_verified_sql`/`execute`/`validate_result`/`visible_dataset`/`assert_intent_permitted`
- Produces:
  - `app.pipeline.orchestrator.TurnStatus` — 枚举 `ANSWERED`/`CLARIFYING`/`REFUSED`/`FAILED`
  - `app.pipeline.orchestrator.TurnOutcome` — frozen dataclass `status`/`turn_id`/`answer`/`clarifications`/`refusal_reason`/`slot_state`
  - `app.pipeline.orchestrator.QueryOrchestrator(meta_session, sample_connection, llm_client, settings)` — 方法 `ask(*, username, question, conversation_id=None, dataset_name)`

- [ ] **Step 1: 写失败的编排测试**

`backend/tests/pipeline/test_orchestrator.py`：

```python
import json

import pytest

from app.core.config import Settings
from app.observability.trace import Stage, load_trace
from app.pipeline.orchestrator import QueryOrchestrator, TurnStatus
from tests.security.factories import build_principals
from tests.semantic.factories import build_orders_dataset


class StubClient:
    """Queue of canned payloads, one per expected recognition call."""

    def __init__(self, *payloads: str) -> None:
        self.payloads = list(payloads)
        self.calls = 0

    def complete(self, system: str, user: str):
        from app.intent.recognizer import LlmCompletion

        self.calls += 1
        content = self.payloads.pop(0) if self.payloads else self.payloads_default
        return LlmCompletion(content=content, model="stub", prompt_tokens=90, completion_tokens=15)


def _payload(**overrides) -> str:
    base = {
        "kind": "aggregate",
        "metrics": ["sales_revenue"],
        "dimensions": [],
        "filters": [],
        "time": {"start": "2026-08-01", "end": "2026-08-31", "grain": "month", "expression": "本月"},
        "comparison": "none",
        "confidence": {"overall": 0.92, "metric": 0.95, "time": 0.93},
        "assumptions": [],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def _settings(**overrides) -> Settings:
    base = {
        "clarify_confidence_threshold": 0.7,
        "clarify_max_rounds": 2,
        "max_result_rows": 1000,
        "cost_warn_rows": 10_000,
        "cost_reject_rows": 100_000,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def env(meta_session):
    build_orders_dataset(meta_session)
    build_principals(meta_session)
    return meta_session


def _orchestrator(env, sample_conn, client, settings=None):
    return QueryOrchestrator(
        meta_session=env,
        sample_connection=sample_conn,
        llm_client=client,
        settings=settings or _settings(),
    )


def test_happy_path_answers_with_citation(env, sample_conn):
    outcome = _orchestrator(env, sample_conn, StubClient(_payload())).ask(
        username="admin", question="本月销售额", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.ANSWERED
    assert outcome.answer.citation.metric.startswith("sales_revenue v3")
    assert outcome.answer.citation.data_updated_at


def test_all_seven_stages_are_traced(env, sample_conn):
    outcome = _orchestrator(env, sample_conn, StubClient(_payload())).ask(
        username="admin", question="本月销售额", dataset_name="orders"
    )

    stages = [item.stage for item in load_trace(env, outcome.turn_id)]
    assert stages == [
        Stage.VERIFIED_RECALL.value,
        Stage.INTENT.value,
        Stage.SEMANTIC_RESOLVE.value,
        Stage.COMPILE.value,
        Stage.SECURITY.value,
        Stage.EXECUTE.value,
        Stage.ANSWER.value,
    ]


def test_intent_stage_records_model_and_tokens(env, sample_conn):
    outcome = _orchestrator(env, sample_conn, StubClient(_payload())).ask(
        username="admin", question="本月销售额", dataset_name="orders"
    )

    stage = next(
        item for item in load_trace(env, outcome.turn_id) if item.stage == Stage.INTENT.value
    )
    assert stage.model == "stub"
    assert stage.prompt_tokens == 90


def test_compiled_sql_is_in_the_trace(env, sample_conn):
    outcome = _orchestrator(env, sample_conn, StubClient(_payload())).ask(
        username="admin", question="本月销售额", dataset_name="orders"
    )

    stage = next(
        item for item in load_trace(env, outcome.turn_id) if item.stage == Stage.SECURITY.value
    )
    assert "SELECT" in stage.output_payload["sql"].upper()


def test_intent_snapshot_is_stored_for_replay(env, sample_conn):
    from app.observability.orm import TurnRow

    outcome = _orchestrator(env, sample_conn, StubClient(_payload())).ask(
        username="admin", question="本月销售额", dataset_name="orders"
    )

    turn = env.get(TurnRow, outcome.turn_id)
    assert turn.intent_snapshot["metrics"] == ["sales_revenue"]


def test_low_confidence_returns_clarification_not_a_number(env, sample_conn):
    payload = _payload(confidence={"overall": 0.4, "metric": 0.3, "time": 0.9})
    outcome = _orchestrator(env, sample_conn, StubClient(payload)).ask(
        username="admin", question="业绩怎么样", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.CLARIFYING
    assert outcome.answer is None
    assert outcome.clarifications


def test_clarifying_turn_stops_before_compilation(env, sample_conn):
    payload = _payload(confidence={"overall": 0.4, "metric": 0.3, "time": 0.9})
    outcome = _orchestrator(env, sample_conn, StubClient(payload)).ask(
        username="admin", question="业绩怎么样", dataset_name="orders"
    )

    stages = {item.stage for item in load_trace(env, outcome.turn_id)}
    assert Stage.COMPILE.value not in stages


def test_unsupported_question_is_refused(env, sample_conn):
    payload = _payload(kind="unsupported", metrics=[], confidence={"overall": 0.1})
    outcome = _orchestrator(env, sample_conn, StubClient(payload)).ask(
        username="admin", question="帮我把这单改成已完成", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.REFUSED
    assert outcome.refusal_reason


def test_permission_refusal_leaks_no_metadata(env, sample_conn):
    payload = _payload(metrics=["total_cost"])
    outcome = _orchestrator(env, sample_conn, StubClient(payload)).ask(
        username="east_manager", question="本月总成本", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.REFUSED
    for leak in ("total_cost", "cost", "orders", "sample"):
        assert leak not in outcome.refusal_reason


def test_denied_columns_are_absent_from_the_prompt(env, sample_conn):
    """Recall-time invisibility: the model is never told the field exists."""
    client = StubClient(_payload())
    _orchestrator(env, sample_conn, client).ask(
        username="east_manager", question="本月销售额", dataset_name="orders"
    )

    assert "cost" not in client.last_user_prompt


def test_row_policy_appears_in_the_citation(env, sample_conn):
    outcome = _orchestrator(env, sample_conn, StubClient(_payload())).ask(
        username="east_manager", question="本月销售额", dataset_name="orders"
    )

    permission_lines = [
        item for item in outcome.answer.citation.filters if item.source == "permission"
    ]
    assert permission_lines and "华东" in permission_lines[0].value


def test_verified_query_hit_skips_intent_recognition(env, sample_conn):
    from app.pipeline.verified import register
    from tests.pipeline.test_verified import _intent as verified_intent

    register(
        env,
        dataset_name="orders",
        question="本月销售额",
        fixed_sql="SELECT SUM(amount) AS sales_revenue FROM sample.orders",
        intent=verified_intent(),
        created_by="admin",
    )
    env.flush()

    client = StubClient(_payload())
    outcome = _orchestrator(env, sample_conn, client).ask(
        username="admin", question="本月销售额", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.ANSWERED
    assert client.calls == 0


def test_verified_query_hit_still_gets_row_policy(env, sample_conn):
    """The recall path must not become a permission bypass."""
    from app.pipeline.verified import register
    from tests.pipeline.test_verified import _intent as verified_intent

    register(
        env,
        dataset_name="orders",
        question="本月销售额",
        fixed_sql="SELECT SUM(amount) AS sales_revenue FROM sample.orders",
        intent=verified_intent(),
        created_by="admin",
    )
    env.flush()

    outcome = _orchestrator(env, sample_conn, StubClient()).ask(
        username="east_manager", question="本月销售额", dataset_name="orders"
    )

    stage = next(
        item for item in load_trace(env, outcome.turn_id) if item.stage == Stage.SECURITY.value
    )
    assert "'EC'" in stage.output_payload["sql"]


def test_empty_result_does_not_produce_a_number(env, sample_conn):
    payload = _payload(
        time={"start": "2020-01-01", "end": "2020-01-31", "grain": "month", "expression": "2020年1月"}
    )
    outcome = _orchestrator(env, sample_conn, StubClient(payload)).ask(
        username="admin", question="2020年1月销售额", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.FAILED
    assert outcome.answer is None
    assert "没有数据" in outcome.refusal_reason


def test_slot_state_is_persisted_for_the_next_turn(env, sample_conn):
    orchestrator = _orchestrator(env, sample_conn, StubClient(_payload(), _payload()))
    first = orchestrator.ask(username="admin", question="本月销售额", dataset_name="orders")

    assert first.slot_state["metrics"] == ["sales_revenue"]

    second = orchestrator.ask(
        username="admin",
        question="那按省份看",
        conversation_id=first.conversation_id,
        dataset_name="orders",
    )
    assert second.status == TurnStatus.ANSWERED


def test_followup_receives_previous_slots_in_the_prompt(env, sample_conn):
    client = StubClient(_payload(), _payload())
    orchestrator = _orchestrator(env, sample_conn, client)
    first = orchestrator.ask(username="admin", question="本月销售额", dataset_name="orders")
    orchestrator.ask(
        username="admin",
        question="那华南呢",
        conversation_id=first.conversation_id,
        dataset_name="orders",
    )

    assert "上一轮" in client.last_user_prompt


def test_clarify_round_counting_falls_back_to_defaults(env, sample_conn):
    low = _payload(confidence={"overall": 0.4, "metric": 0.3, "time": 0.9})
    client = StubClient(low, low, low)
    orchestrator = _orchestrator(env, sample_conn, client, _settings(clarify_max_rounds=1))

    first = orchestrator.ask(username="admin", question="业绩怎么样", dataset_name="orders")
    assert first.status == TurnStatus.CLARIFYING

    second = orchestrator.ask(
        username="admin",
        question="业绩怎么样",
        conversation_id=first.conversation_id,
        dataset_name="orders",
    )

    # Round limit reached: answer, but the assumption must be visible.
    assert second.status == TurnStatus.ANSWERED
    assert second.answer.assumptions


def test_unpublished_dataset_is_refused(meta_session, sample_conn):
    build_orders_dataset(meta_session, published=False)
    build_principals(meta_session)

    outcome = _orchestrator(meta_session, sample_conn, StubClient(_payload())).ask(
        username="admin", question="本月销售额", dataset_name="orders"
    )
    assert outcome.status == TurnStatus.REFUSED


def test_recognition_failure_is_reported_as_failed(env, sample_conn):
    outcome = _orchestrator(env, sample_conn, StubClient("这不是 JSON")).ask(
        username="admin", question="本月销售额", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.FAILED
    stage = next(
        item for item in load_trace(env, outcome.turn_id) if item.stage == Stage.INTENT.value
    )
    assert stage.error


def test_inactive_user_is_refused_without_detail(env, sample_conn):
    outcome = _orchestrator(env, sample_conn, StubClient(_payload())).ask(
        username="retired_user", question="本月销售额", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.REFUSED
    assert "orders" not in outcome.refusal_reason
```

- [ ] **Step 2: 给 StubClient 补上被断言的属性**

上面用到了 `client.last_user_prompt` 与 `StubClient()` 无参构造。补全测试桩：

```python
class StubClient:
    """Queue of canned payloads, one per expected recognition call."""

    payloads_default = "{}"

    def __init__(self, *payloads: str) -> None:
        self.payloads = list(payloads)
        self.calls = 0
        self.last_user_prompt = ""

    def complete(self, system: str, user: str):
        from app.intent.recognizer import LlmCompletion

        self.calls += 1
        self.last_user_prompt = user
        content = self.payloads.pop(0) if self.payloads else self.payloads_default
        return LlmCompletion(content=content, model="stub", prompt_tokens=90, completion_tokens=15)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/pipeline/test_orchestrator.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.pipeline.orchestrator'`

- [ ] **Step 4: 写编排器**

`backend/app/pipeline/orchestrator.py`：

```python
"""The seven-stage pipeline (spec 3.2, M-09).

The orchestrator owns flow control and tracing; stages stay unaware of their
position. Three exits: answered, clarifying, refused. Nothing reaches an answer
without passing security rewriting — including Verified Query hits.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.compiler.errors import CompileError
from app.compiler.query import Citation, compile_intent
from app.core.config import Settings
from app.execution.runner import ExecutionFailedError, execute
from app.execution.validation import validate_result
from app.intent.recognizer import IntentRecognitionError, LlmClient, recognize
from app.intent.schema import IntentKind, QueryIntent, merge_followup
from app.observability.orm import ConversationRow, TurnRow
from app.observability.trace import Stage, TraceRecorder
from app.pipeline.answer import Answer, ResultNotAnswerableError, build_answer
from app.pipeline.clarify import ClarifyRequest
from app.pipeline.resolve import resolve_intent
from app.pipeline.verified import recall
from app.security.columns import PermissionDeniedError, assert_intent_permitted, visible_dataset
from app.security.guardrails import QueryTooExpensiveError
from app.security.pipeline import SecuredQuery, secure_compiled, secure_verified_sql
from app.security.principal import PrincipalNotFoundError, load_principal
from app.security.whitelist import AstRejectedError
from app.semantic.loader import load_dataset
from app.semantic.model import SemanticError
from enum import Enum

_GENERIC_REFUSAL = "你没有该数据的访问权限"
_OUT_OF_SCOPE = "这个问题超出了当前数据集能回答的范围，我无法据此给出数字"


class TurnStatus(str, Enum):
    ANSWERED = "answered"
    CLARIFYING = "clarifying"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    status: TurnStatus
    turn_id: int
    conversation_id: int
    answer: Answer | None = None
    clarifications: tuple[ClarifyRequest, ...] = ()
    refusal_reason: str = ""
    slot_state: dict | None = None


class QueryOrchestrator:
    def __init__(
        self,
        *,
        meta_session: Session,
        sample_connection: Connection,
        llm_client: LlmClient,
        settings: Settings,
    ) -> None:
        self._session = meta_session
        self._connection = sample_connection
        self._client = llm_client
        self._settings = settings

    def ask(
        self,
        *,
        username: str,
        question: str,
        dataset_name: str,
        conversation_id: int | None = None,
    ) -> TurnOutcome:
        conversation = self._conversation(conversation_id, username, question, dataset_name)
        turn = TurnRow(conversation_id=conversation.id, question=question)
        self._session.add(turn)
        self._session.flush()
        recorder = TraceRecorder(self._session, turn.id)

        try:
            return self._run(recorder, conversation, turn, username, question, dataset_name)
        except (PermissionDeniedError, PrincipalNotFoundError):
            return self._refuse(turn, _GENERIC_REFUSAL)
        except AstRejectedError:
            # Structural rejection is an internal fault, not a user message.
            return self._fail(turn, "查询无法安全执行，已阻止")
        except QueryTooExpensiveError as error:
            return self._fail(turn, error.estimate.message)
        except SemanticError:
            return self._refuse(turn, _OUT_OF_SCOPE)
        except CompileError:
            return self._fail(turn, "语义配置存在问题，已记录待管理员处理")
        except IntentRecognitionError:
            return self._fail(turn, "没有理解这个问题，请换一种说法")
        except ExecutionFailedError:
            return self._fail(turn, "查询执行失败，已记录详情")
        except ResultNotAnswerableError as error:
            return self._fail(turn, str(error))

    # --- stages -----------------------------------------------------------

    def _run(
        self,
        recorder: TraceRecorder,
        conversation: ConversationRow,
        turn: TurnRow,
        username: str,
        question: str,
        dataset_name: str,
    ) -> TurnOutcome:
        principal = load_principal(self._session, username)
        full_dataset = load_dataset(self._session, dataset_name)
        if not full_dataset.is_published:
            return self._refuse(turn, _OUT_OF_SCOPE)

        # Denied columns disappear before the model ever sees the dataset.
        dataset = visible_dataset(full_dataset, principal)

        # Stage 1
        with recorder.stage_timer(Stage.VERIFIED_RECALL, {"question": question}) as span:
            hit = recall(self._session, dataset_name, question)
            span.output = {"hit": hit.id if hit else None}

        if hit is not None:
            secured = self._secure_verified(recorder, hit.fixed_sql, dataset, principal)
            return self._finish(
                recorder, conversation, turn, dataset, secured, self._verified_citation(dataset)
            )

        # Stage 2
        slot_state = conversation.slot_state or None
        with recorder.stage_timer(Stage.INTENT, {"question": question, "slots": slot_state}) as span:
            intent, completion = recognize(self._client, dataset, question, slot_state)
            span.model = completion.model
            span.prompt_tokens = completion.prompt_tokens
            span.completion_tokens = completion.completion_tokens
            span.output = intent.to_payload()

        if slot_state:
            intent = merge_followup(QueryIntent.from_payload(slot_state), intent)

        turn.intent_snapshot = intent.to_payload()

        if intent.kind == IntentKind.UNSUPPORTED:
            return self._refuse(turn, _OUT_OF_SCOPE)

        # Stage 3
        round_index = conversation.slot_state.get("clarify_rounds", 0) if conversation.slot_state else 0
        with recorder.stage_timer(Stage.SEMANTIC_RESOLVE, intent.to_payload()) as span:
            outcome = resolve_intent(
                dataset, intent, self._settings, round_index=round_index
            )
            span.output = {
                "intent": outcome.intent.to_payload(),
                "clarifications": [item.target for item in outcome.clarifications],
                "assumptions": list(outcome.assumptions),
            }

        if outcome.clarifications:
            conversation.slot_state = {
                **outcome.intent.to_payload(),
                "clarify_rounds": round_index + 1,
            }
            turn.status = TurnStatus.CLARIFYING.value
            return TurnOutcome(
                status=TurnStatus.CLARIFYING,
                turn_id=turn.id,
                conversation_id=conversation.id,
                clarifications=outcome.clarifications,
                slot_state=conversation.slot_state,
            )

        resolved = outcome.intent
        assert_intent_permitted(dataset, resolved, principal)

        # Stage 4
        with recorder.stage_timer(Stage.COMPILE, resolved.to_payload()) as span:
            compiled = compile_intent(dataset, resolved)
            span.output = {"sql": compiled.sql, "metrics": list(compiled.metric_names)}

        # Stage 5
        with recorder.stage_timer(Stage.SECURITY, {"sql": compiled.sql}) as span:
            secured = secure_compiled(
                compiled, dataset, principal, self._connection, self._settings
            )
            span.output = {
                "sql": secured.sql,
                "row_filters": [item.field_business_name for item in secured.applied_row_filters],
                "masked": list(secured.masked_field_names),
                "estimated_rows": secured.cost.estimated_rows,
            }

        return self._finish(
            recorder,
            conversation,
            turn,
            dataset,
            secured,
            compiled.citation,
            intent=resolved,
            assumptions=outcome.assumptions,
            metric_names=compiled.metric_names,
            comparison_metric_names=compiled.comparison_metric_names,
            dimension_names=compiled.dimension_names,
        )

    def _secure_verified(self, recorder, fixed_sql, dataset, principal) -> SecuredQuery:
        with recorder.stage_timer(Stage.SECURITY, {"sql": fixed_sql}) as span:
            secured = secure_verified_sql(
                fixed_sql, dataset, principal, self._connection, self._settings
            )
            span.output = {
                "sql": secured.sql,
                "row_filters": [item.field_business_name for item in secured.applied_row_filters],
                "masked": list(secured.masked_field_names),
                "estimated_rows": secured.cost.estimated_rows,
            }
        return secured

    def _finish(
        self,
        recorder: TraceRecorder,
        conversation: ConversationRow,
        turn: TurnRow,
        dataset,
        secured: SecuredQuery,
        citation: Citation,
        *,
        intent: QueryIntent | None = None,
        assumptions: tuple[str, ...] = (),
        metric_names: tuple[str, ...] = (),
        comparison_metric_names: tuple[str, ...] = (),
        dimension_names: tuple[str, ...] = (),
    ) -> TurnOutcome:
        # Stage 6
        with recorder.stage_timer(Stage.EXECUTE, {"sql": secured.sql}) as span:
            result = execute(secured, self._settings, connection=self._connection)
            issues = validate_result(
                result,
                has_filters=bool(intent.filters) if intent else False,
                comparison_columns=dict(zip(metric_names, comparison_metric_names)),
            )
            span.output = {
                "row_count": result.row_count,
                "elapsed_ms": result.elapsed_ms,
                "issues": [item.code.value for item in issues],
            }

        # Stage 7
        with recorder.stage_timer(Stage.ANSWER, {"row_count": result.row_count}) as span:
            answer = build_answer(
                citation=citation,
                result=result,
                metric_names=metric_names or (citation.metric_name,),
                comparison_metric_names=comparison_metric_names,
                dimension_names=dimension_names,
                applied_row_filters=secured.applied_row_filters,
                masked_field_names=secured.masked_field_names,
                issues=issues,
                assumptions=assumptions,
                data_updated_at=self._data_updated_at(dataset),
                available_dimensions=tuple(
                    field.name for field in dataset.fields if field.is_groupable
                ),
            )
            span.output = {"headline": answer.headline}

        turn.status = TurnStatus.ANSWERED.value
        turn.answer = {"headline": answer.headline, "conclusion": answer.conclusion}
        if intent is not None:
            conversation.slot_state = {**intent.to_payload(), "clarify_rounds": 0}

        return TurnOutcome(
            status=TurnStatus.ANSWERED,
            turn_id=turn.id,
            conversation_id=conversation.id,
            answer=answer,
            slot_state=conversation.slot_state,
        )

    # --- helpers ----------------------------------------------------------

    def _conversation(
        self, conversation_id: int | None, username: str, question: str, dataset_name: str
    ) -> ConversationRow:
        if conversation_id is not None:
            existing = self._session.get(ConversationRow, conversation_id)
            if existing is not None:
                return existing

        principal = load_principal(self._session, username)
        row = ConversationRow(
            user_id=principal.user_id,
            title=question[:64],
            dataset_name=dataset_name,
            slot_state={},
        )
        self._session.add(row)
        self._session.flush()
        return row

    def _verified_citation(self, dataset) -> Citation:
        """Verified entries carry their own intent snapshot; fall back to the
        dataset's primary metric for the citation shape."""
        metric = dataset.metrics[0]
        return Citation(
            metric_name=metric.name,
            metric_business_name=metric.business_name,
            metric_version=metric.version,
            metric_description=metric.description,
            time_field_business_name=(
                dataset.field(metric.time_field).business_name or metric.time_field
            ),
            time_start=datetime.now().date(),
            time_end=datetime.now().date(),
        )

    def _data_updated_at(self, dataset) -> datetime | None:
        """Freshness shown in the citation: the latest business date in scope."""
        column = dataset.metrics[0].time_field
        physical = dataset.field(column).physical_column
        statement = text(f"SELECT MAX({physical}) FROM {dataset.physical_table}")
        value = self._connection.execute(statement).scalar()
        if value is None:
            return None
        return datetime.combine(value, datetime.min.time())

    def _refuse(self, turn: TurnRow, reason: str) -> TurnOutcome:
        turn.status = TurnStatus.REFUSED.value
        return TurnOutcome(
            status=TurnStatus.REFUSED,
            turn_id=turn.id,
            conversation_id=turn.conversation_id,
            refusal_reason=reason,
        )

    def _fail(self, turn: TurnRow, reason: str) -> TurnOutcome:
        turn.status = TurnStatus.FAILED.value
        return TurnOutcome(
            status=TurnStatus.FAILED,
            turn_id=turn.id,
            conversation_id=turn.conversation_id,
            refusal_reason=reason,
        )
```

- [ ] **Step 5: 补齐 `QueryIntent.from_payload`**

`merge_followup` 需要把持久化的槽位还原为 `QueryIntent`。在 `app/intent/schema.py` 增加与 `to_payload` 对称的类方法：

```python
    @classmethod
    def from_payload(cls, payload: dict) -> "QueryIntent":
        """Inverse of to_payload. Unknown keys (such as clarify_rounds) are ignored."""
        time_payload = payload.get("time")
        sort_payload = payload.get("sort")
        return cls(
            kind=IntentKind(payload.get("kind", IntentKind.AGGREGATE.value)),
            dataset=payload.get("dataset", ""),
            metrics=list(payload.get("metrics", [])),
            dimensions=list(payload.get("dimensions", [])),
            filters=[
                FilterCondition(
                    field=item["field"],
                    operator=FilterOperator(item["operator"]),
                    values=list(item.get("values", [])),
                    spoken_values=list(item.get("spoken_values", [])),
                )
                for item in payload.get("filters", [])
            ],
            time=(
                TimeRange(
                    start=date.fromisoformat(time_payload["start"]),
                    end=date.fromisoformat(time_payload["end"]),
                    grain=TimeGrain(time_payload["grain"]),
                    expression=time_payload.get("expression", ""),
                )
                if time_payload
                else None
            ),
            comparison=ComparisonKind(payload.get("comparison", ComparisonKind.NONE.value)),
            sort=(
                SortSpec(
                    by=sort_payload["by"],
                    descending=sort_payload.get("descending", True),
                    limit=sort_payload.get("limit"),
                )
                if sort_payload
                else None
            ),
            confidence=FieldConfidence(overall=1.0),
            assumptions=list(payload.get("assumptions", [])),
            raw_question=payload.get("raw_question", ""),
        )
```

对应的往返测试补进 `tests/intent/test_schema.py`：

```python
def test_payload_round_trip_preserves_slots():
    intent = QueryIntent(
        kind=IntentKind.RANKING,
        dataset="orders",
        metrics=["sales_revenue"],
        dimensions=["province"],
        filters=[
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=["EC"],
                spoken_values=["华东"],
            )
        ],
        time=TimeRange(
            start=date(2026, 8, 1), end=date(2026, 8, 31), grain=TimeGrain.MONTH, expression="本月"
        ),
        comparison=ComparisonKind.MOM,
        sort=SortSpec(by="sales_revenue", descending=True, limit=5),
        confidence=FieldConfidence(overall=0.9),
        raw_question="本月各省销售额排行",
    )
    restored = QueryIntent.from_payload(intent.to_payload())

    assert restored.slot_signature() == intent.slot_signature()


def test_from_payload_ignores_unknown_keys():
    payload = {"kind": "aggregate", "dataset": "orders", "metrics": ["sales_revenue"],
               "clarify_rounds": 2}
    assert QueryIntent.from_payload(payload).metrics == ["sales_revenue"]
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/pipeline/test_orchestrator.py tests/intent/test_schema.py -v`
Expected: PASS（编排 20 项 + 意图 Schema 10 项）

- [ ] **Step 7: 提交**

```bash
git add backend/app/pipeline/orchestrator.py backend/tests/pipeline/test_orchestrator.py backend/app/intent/schema.py backend/tests/intent/test_schema.py
git commit -F - <<'EOF'
串联七阶段问数管道

管道有三个出口：作答、澄清、拒答，若把出口判断散落在各阶段，很容易出现某条分支绕过安全改写或漏记 Trace。因此把流程控制与 Trace 记录收在编排器一处，阶段本身不感知自己处于第几步。

- 七个阶段逐一落 Trace，澄清与拒答分支在中途终止且阶段记录完整
- 无权列在加载数据集后立即剔除，模型的输入中不含该字段
- Verified Query 命中跳过意图识别，但同样进入安全改写
- 越权与超范围统一回固定文案，异常明细只进 Trace
- 会话保存结构化槽位与澄清轮数，追问在槽位上做覆盖式合并
- 意图新增与序列化对称的还原方法，支持槽位回填与重放
- 验证：pytest 编排 20 项、意图 Schema 10 项通过
EOF
```

---

### Task 7: 问答与 Trace 的 HTTP 接口

**Files:**
- Create: `backend/app/api/chat.py`
- Create: `backend/app/api/trace.py`
- Create: `backend/app/observability/schemas.py`
- Create: `backend/app/observability/service.py`
- Modify: `backend/app/core/db.py`（新增 `get_sample_connection` 依赖）
- Modify: `backend/app/main.py`（挂载两个 router）
- Create: `backend/tests/api/test_chat_api.py`
- Create: `backend/tests/api/test_trace_api.py`

**Interfaces:**
- Consumes: Task 6 的 `QueryOrchestrator`/`TurnOutcome`、Task 1 的 `load_trace`/`ConversationRow`/`TurnRow`/`FeedbackRow`
- Produces:
  - `app.core.db.get_sample_connection()` — FastAPI 依赖，yield `Connection`
  - `app.core.security.get_current_username()` — 从 `X-Username` 头取身份，缺失则 401
  - `app.observability.schemas.AskIn`、`AnswerOut`、`ClarifyOut`、`AskOut`、`ConversationOut`、`TurnOut`、`FeedbackIn`、`TraceStageOut`、`TraceOut`
  - `app.observability.service.list_conversations(session, user_id)`、`list_turns(session, conversation_id)`、`save_feedback(session, turn_id, payload)`、`get_trace(session, turn_id)`、`replay_turn(session, turn_id)`
  - REST 路由：`POST /api/chat/ask`、`GET /api/chat/conversations`、`GET /api/chat/conversations/{id}/turns`、`POST /api/chat/turns/{id}/feedback`、`GET /api/trace/turns/{id}`、`POST /api/trace/turns/{id}/replay`

本轮身份取最简形式：`X-Username` 请求头。这是 S-11 中 `api-gateway` 认证职责的占位实现，落在一个独立依赖里，接完真实登录只换这一个函数。

- [ ] **Step 1: 写失败的问答接口测试**

`backend/tests/api/test_chat_api.py`：

```python
import json

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_meta_session, get_sample_connection
from app.main import app
from tests.security.factories import build_principals
from tests.semantic.factories import build_orders_dataset


def _payload(**overrides) -> str:
    base = {
        "kind": "aggregate",
        "metrics": ["sales_revenue"],
        "dimensions": [],
        "filters": [],
        "time": {"start": "2026-08-01", "end": "2026-08-31", "grain": "month", "expression": "本月"},
        "comparison": "none",
        "confidence": {"overall": 0.92, "metric": 0.95, "time": 0.93},
        "assumptions": [],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


class StubClient:
    payloads_default = _payload()

    def __init__(self, *payloads: str) -> None:
        self.payloads = list(payloads)
        self.last_user_prompt = ""

    def complete(self, system: str, user: str):
        from app.intent.recognizer import LlmCompletion

        self.last_user_prompt = user
        content = self.payloads.pop(0) if self.payloads else self.payloads_default
        return LlmCompletion(content=content, model="stub", prompt_tokens=90, completion_tokens=15)


@pytest.fixture
def stub():
    return StubClient()


@pytest.fixture
def client(meta_session, sample_conn, stub):
    from app.api import chat

    build_orders_dataset(meta_session)
    build_principals(meta_session)
    app.dependency_overrides[get_meta_session] = lambda: meta_session
    app.dependency_overrides[get_sample_connection] = lambda: sample_conn
    app.dependency_overrides[chat.get_llm_client] = lambda: stub
    yield TestClient(app)
    app.dependency_overrides.clear()


def _ask(client, question="本月销售额", username="admin", **extra):
    body = {"question": question, "dataset_name": "orders", **extra}
    return client.post("/api/chat/ask", json=body, headers={"X-Username": username})


def test_ask_returns_answer_with_citation(client):
    response = _ask(client)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["answer"]["citation"]["metric"].startswith("sales_revenue v3")


def test_ask_without_identity_is_401(client):
    response = client.post(
        "/api/chat/ask", json={"question": "本月销售额", "dataset_name": "orders"}
    )
    assert response.status_code == 401


def test_ask_returns_turn_and_conversation_ids(client):
    body = _ask(client).json()

    assert body["turn_id"] > 0
    assert body["conversation_id"] > 0


def test_permission_denied_answers_200_with_refused_status(client, stub):
    """A refusal is a normal turn, not an HTTP error: it must be traceable."""
    stub.payloads.append(_payload(metrics=["total_cost"]))
    body = _ask(client, username="east_manager").json()

    assert body["status"] == "refused"
    assert body["answer"] is None
    assert "权限" in body["refusal_reason"]


def test_refusal_response_leaks_no_metadata(client, stub):
    stub.payloads.append(_payload(metrics=["total_cost"]))
    raw = _ask(client, username="east_manager").text

    for leak in ("total_cost", "sample.orders", "region_code"):
        assert leak not in raw


def test_clarification_is_returned_with_options(client, stub):
    stub.payloads.append(_payload(confidence={"overall": 0.4, "metric": 0.3, "time": 0.9}))
    body = _ask(client, question="业绩怎么样").json()

    assert body["status"] == "clarifying"
    assert body["clarifications"][0]["options"]


def test_followup_continues_the_same_conversation(client):
    first = _ask(client).json()
    second = _ask(client, question="那按省份看", conversation_id=first["conversation_id"]).json()

    assert second["conversation_id"] == first["conversation_id"]


def test_slot_state_is_returned_for_the_condition_panel(client):
    body = _ask(client).json()

    assert body["slot_state"]["metrics"] == ["sales_revenue"]
    assert body["slot_state"]["time"]["start"] == "2026-08-01"


def test_answer_carries_row_data_for_the_result_pane(client):
    body = _ask(client).json()

    assert body["answer"]["columns"]
    assert body["answer"]["rows"]


def test_unknown_dataset_returns_404(client):
    response = client.post(
        "/api/chat/ask",
        json={"question": "本月销售额", "dataset_name": "ghost"},
        headers={"X-Username": "admin"},
    )
    assert response.status_code == 404


def test_conversation_list_is_scoped_to_the_caller(client):
    _ask(client, username="admin")
    _ask(client, username="analyst")

    mine = client.get("/api/chat/conversations", headers={"X-Username": "analyst"}).json()

    assert len(mine) == 1
    assert mine[0]["title"]


def test_turns_of_a_conversation_are_ordered(client):
    first = _ask(client).json()
    _ask(client, question="那按省份看", conversation_id=first["conversation_id"])

    turns = client.get(
        f"/api/chat/conversations/{first['conversation_id']}/turns",
        headers={"X-Username": "admin"},
    ).json()

    assert [item["question"] for item in turns] == ["本月销售额", "那按省份看"]


def test_other_users_conversation_turns_are_404(client):
    mine = _ask(client, username="admin").json()

    response = client.get(
        f"/api/chat/conversations/{mine['conversation_id']}/turns",
        headers={"X-Username": "analyst"},
    )
    assert response.status_code == 404


def test_negative_feedback_requires_a_category(client):
    turn_id = _ask(client).json()["turn_id"]

    response = client.post(
        f"/api/chat/turns/{turn_id}/feedback",
        json={"is_positive": False},
        headers={"X-Username": "admin"},
    )
    assert response.status_code == 422


def test_negative_feedback_with_attribution_is_stored(client, meta_session):
    from app.observability.orm import FeedbackRow

    turn_id = _ask(client).json()["turn_id"]

    response = client.post(
        f"/api/chat/turns/{turn_id}/feedback",
        json={"is_positive": False, "category": "metric", "comment": "口径不对"},
        headers={"X-Username": "admin"},
    )

    assert response.status_code == 201
    stored = meta_session.query(FeedbackRow).filter_by(turn_id=turn_id).one()
    assert stored.category == "metric"


def test_positive_feedback_needs_no_category(client):
    turn_id = _ask(client).json()["turn_id"]

    response = client.post(
        f"/api/chat/turns/{turn_id}/feedback",
        json={"is_positive": True},
        headers={"X-Username": "admin"},
    )
    assert response.status_code == 201


def test_unknown_feedback_category_is_rejected(client):
    turn_id = _ask(client).json()["turn_id"]

    response = client.post(
        f"/api/chat/turns/{turn_id}/feedback",
        json={"is_positive": False, "category": "vibes"},
        headers={"X-Username": "admin"},
    )
    assert response.status_code == 422
```

- [ ] **Step 2: 写失败的 Trace 接口测试**

`backend/tests/api/test_trace_api.py`：

```python
import pytest
from fastapi.testclient import TestClient

from app.core.db import get_meta_session, get_sample_connection
from app.main import app
from tests.api.test_chat_api import StubClient, _payload
from tests.security.factories import build_principals
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def stub():
    return StubClient()


@pytest.fixture
def client(meta_session, sample_conn, stub):
    from app.api import chat

    build_orders_dataset(meta_session)
    build_principals(meta_session)
    app.dependency_overrides[get_meta_session] = lambda: meta_session
    app.dependency_overrides[get_sample_connection] = lambda: sample_conn
    app.dependency_overrides[chat.get_llm_client] = lambda: stub
    yield TestClient(app)
    app.dependency_overrides.clear()


def _ask(client, username="admin", question="本月销售额"):
    return client.post(
        "/api/chat/ask",
        json={"question": question, "dataset_name": "orders"},
        headers={"X-Username": username},
    ).json()


def test_trace_lists_all_seven_stages(client):
    turn_id = _ask(client)["turn_id"]

    body = client.get(f"/api/trace/turns/{turn_id}", headers={"X-Username": "admin"}).json()

    assert [item["stage"] for item in body["stages"]] == [
        "verified_recall",
        "intent",
        "semantic_resolve",
        "compile",
        "security",
        "execute",
        "answer",
    ]


def test_trace_exposes_sql_and_tokens(client):
    turn_id = _ask(client)["turn_id"]

    stages = client.get(f"/api/trace/turns/{turn_id}", headers={"X-Username": "admin"}).json()[
        "stages"
    ]

    intent = next(item for item in stages if item["stage"] == "intent")
    security = next(item for item in stages if item["stage"] == "security")
    assert intent["prompt_tokens"] == 90
    assert "SELECT" in security["output_payload"]["sql"].upper()


def test_trace_reports_elapsed_per_stage(client):
    turn_id = _ask(client)["turn_id"]

    stages = client.get(f"/api/trace/turns/{turn_id}", headers={"X-Username": "admin"}).json()[
        "stages"
    ]
    assert all(item["elapsed_ms"] >= 0 for item in stages)


def test_trace_header_carries_question_and_status(client):
    turn_id = _ask(client)["turn_id"]

    body = client.get(f"/api/trace/turns/{turn_id}", headers={"X-Username": "admin"}).json()

    assert body["question"] == "本月销售额"
    assert body["status"] == "answered"


def test_trace_of_another_users_turn_is_404(client):
    turn_id = _ask(client, username="admin")["turn_id"]

    response = client.get(f"/api/trace/turns/{turn_id}", headers={"X-Username": "analyst"})
    assert response.status_code == 404


def test_unknown_turn_is_404(client):
    response = client.get("/api/trace/turns/999999", headers={"X-Username": "admin"})
    assert response.status_code == 404


def test_replay_recompiles_from_the_intent_snapshot(client):
    turn_id = _ask(client)["turn_id"]

    body = client.post(
        f"/api/trace/turns/{turn_id}/replay", headers={"X-Username": "admin"}
    ).json()

    assert "SELECT" in body["sql"].upper()
    assert body["matches_original"] is True


def test_replay_does_not_call_the_model(client, stub):
    turn_id = _ask(client)["turn_id"]
    before = stub.last_user_prompt

    client.post(f"/api/trace/turns/{turn_id}/replay", headers={"X-Username": "admin"})

    assert stub.last_user_prompt == before


def test_replay_applies_the_current_permissions(client, stub):
    """Replay is a diagnosis tool, not a permission bypass: rewriting happens again."""
    turn_id = _ask(client, username="east_manager")["turn_id"]

    body = client.post(
        f"/api/trace/turns/{turn_id}/replay", headers={"X-Username": "east_manager"}
    ).json()

    assert "'EC'" in body["sql"]


def test_replay_of_a_turn_without_snapshot_is_409(client, stub):
    stub.payloads.append(_payload(kind="unsupported", metrics=[]))
    turn_id = _ask(client, question="帮我改一下这单")["turn_id"]

    response = client.post(
        f"/api/trace/turns/{turn_id}/replay", headers={"X-Username": "admin"}
    )
    assert response.status_code == 409
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/api/test_chat_api.py tests/api/test_trace_api.py -v`
Expected: FAIL，`ImportError: cannot import name 'get_sample_connection'`

- [ ] **Step 4: 写身份依赖与样本库连接依赖**

`backend/app/core/security.py`：

```python
"""Minimal identity for this round: a caller-supplied username header.

This is the placeholder for the api-gateway authentication responsibility
(spec 3.1). Real login replaces this one function; nothing downstream reads
the request object.
"""

from fastapi import Header, HTTPException, status


def get_current_username(x_username: str | None = Header(default=None)) -> str:
    if not x_username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少身份信息"
        )
    return x_username
```

`backend/app/core/db.py` 追加：

```python
def get_sample_connection() -> Iterator[Connection]:
    """Read-only business-data connection. Committing is never needed here;
    the connection is closed per request so a cancelled query cannot leak."""
    with sample_engine.connect() as connection:
        yield connection
```

- [ ] **Step 5: 写响应模型**

`backend/app/observability/schemas.py`：

```python
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FEEDBACK_CATEGORIES = ("metric", "time", "sql", "calculation", "conclusion")


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    dataset_name: str
    conversation_id: int | None = None


class CitationLineOut(BaseModel):
    label: str
    value: str
    source: Literal["user", "permission"]


class CitationOut(BaseModel):
    metric: str
    time: str
    filters: list[CitationLineOut] = Field(default_factory=list)
    data_updated_at: str = ""


class DrillDownOut(BaseModel):
    label: str
    kind: str
    target: str


class AnswerOut(BaseModel):
    headline: str
    conclusion: str = ""
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    citation: CitationOut | None = None
    drill_downs: list[DrillDownOut] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)


class ClarifyOptionOut(BaseModel):
    value: str
    label: str
    hint: str = ""


class ClarifyOut(BaseModel):
    kind: str
    target: str
    question: str
    options: list[ClarifyOptionOut] = Field(default_factory=list)


class AskOut(BaseModel):
    status: Literal["answered", "clarifying", "refused", "failed"]
    conversation_id: int
    turn_id: int
    answer: AnswerOut | None = None
    clarifications: list[ClarifyOut] = Field(default_factory=list)
    refusal_reason: str = ""
    slot_state: dict[str, Any] | None = None


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    dataset_name: str
    updated_at: datetime


class TurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    question: str
    status: str
    answer: dict[str, Any] | None = None
    created_at: datetime


class FeedbackIn(BaseModel):
    is_positive: bool
    category: str = ""
    comment: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _check_category(self) -> "FeedbackIn":
        """M-38: a thumbs-down without attribution is not usable eval data."""
        if self.is_positive:
            return self
        if self.category not in _FEEDBACK_CATEGORIES:
            raise ValueError(f"负反馈必须归因到 {_FEEDBACK_CATEGORIES} 之一")
        return self


class TraceStageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stage: str
    sequence: int
    input_payload: dict[str, Any] | None = None
    output_payload: dict[str, Any] | None = None
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: int = 0
    error: str = ""


class TraceOut(BaseModel):
    turn_id: int
    question: str
    status: str
    intent_snapshot: dict[str, Any] | None = None
    stages: list[TraceStageOut] = Field(default_factory=list)


class ReplayOut(BaseModel):
    sql: str
    display_sql: str
    matches_original: bool
    applied_row_filters: list[str] = Field(default_factory=list)
    masked_field_names: list[str] = Field(default_factory=list)
```

- [ ] **Step 6: 写服务层**

`backend/app/observability/service.py`：

```python
"""Read models and side effects for the chat/trace endpoints.

Ownership checks live here rather than in the routers: every lookup goes
through a caller-scoped query, so an unowned id is indistinguishable from a
missing one.
"""

from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.compiler.query import compile_intent
from app.core.config import Settings
from app.intent.schema import QueryIntent
from app.observability.orm import ConversationRow, FeedbackRow, TraceStageRow, TurnRow
from app.observability.schemas import (
    ConversationOut,
    FeedbackIn,
    ReplayOut,
    TraceOut,
    TraceStageOut,
    TurnOut,
)
from app.observability.trace import Stage, load_trace
from app.security.columns import visible_dataset
from app.security.pipeline import secure_compiled
from app.security.principal import load_principal
from app.semantic.loader import load_dataset


class NotFoundError(Exception):
    """Missing or not owned by the caller — the endpoint answers 404 either way."""


class NotReplayableError(Exception):
    """No intent snapshot: clarifying, refused and failed turns cannot replay."""


def list_conversations(session: Session, username: str) -> list[ConversationOut]:
    principal = load_principal(session, username)
    statement = (
        select(ConversationRow)
        .where(ConversationRow.user_id == principal.user_id)
        .order_by(ConversationRow.updated_at.desc())
    )
    return [ConversationOut.model_validate(row) for row in session.execute(statement).scalars()]


def list_turns(session: Session, username: str, conversation_id: int) -> list[TurnOut]:
    _owned_conversation(session, username, conversation_id)
    statement = (
        select(TurnRow)
        .where(TurnRow.conversation_id == conversation_id)
        .order_by(TurnRow.id)
    )
    return [TurnOut.model_validate(row) for row in session.execute(statement).scalars()]


def save_feedback(session: Session, username: str, turn_id: int, payload: FeedbackIn) -> None:
    _owned_turn(session, username, turn_id)
    session.add(
        FeedbackRow(
            turn_id=turn_id,
            is_positive=payload.is_positive,
            category=payload.category,
            comment=payload.comment,
        )
    )
    session.flush()


def get_trace(session: Session, username: str, turn_id: int) -> TraceOut:
    turn = _owned_turn(session, username, turn_id)
    return TraceOut(
        turn_id=turn.id,
        question=turn.question,
        status=turn.status,
        intent_snapshot=turn.intent_snapshot,
        stages=[TraceStageOut.model_validate(row) for row in load_trace(session, turn_id)],
    )


def replay_turn(
    session: Session,
    username: str,
    turn_id: int,
    *,
    connection: Connection,
    settings: Settings,
) -> ReplayOut:
    """Recompile from the stored intent, no model call. Security rewriting runs
    again against current permissions, so replay can never widen access."""
    turn = _owned_turn(session, username, turn_id)
    if not turn.intent_snapshot:
        raise NotReplayableError

    conversation = session.get(ConversationRow, turn.conversation_id)
    principal = load_principal(session, username)
    dataset = visible_dataset(load_dataset(session, conversation.dataset_name), principal)

    intent = QueryIntent.from_payload(turn.intent_snapshot)
    compiled = compile_intent(dataset, intent)
    secured = secure_compiled(compiled, dataset, principal, connection, settings)

    return ReplayOut(
        sql=secured.sql,
        display_sql=secured.display_sql,
        matches_original=secured.sql == _original_sql(session, turn_id),
        applied_row_filters=[item.field_business_name for item in secured.applied_row_filters],
        masked_field_names=list(secured.masked_field_names),
    )


def _original_sql(session: Session, turn_id: int) -> str:
    for row in load_trace(session, turn_id):
        if row.stage == Stage.SECURITY.value and row.output_payload:
            return row.output_payload.get("sql", "")
    return ""


def _owned_conversation(session: Session, username: str, conversation_id: int) -> ConversationRow:
    principal = load_principal(session, username)
    row = session.get(ConversationRow, conversation_id)
    if row is None or row.user_id != principal.user_id:
        raise NotFoundError
    return row


def _owned_turn(session: Session, username: str, turn_id: int) -> TurnRow:
    turn = session.get(TurnRow, turn_id)
    if turn is None:
        raise NotFoundError
    _owned_conversation(session, username, turn.conversation_id)
    return turn
```

- [ ] **Step 7: 写问答路由**

`backend/app/api/chat.py`：

```python
from dataclasses import asdict, is_dataclass

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import get_meta_session, get_sample_connection
from app.core.security import get_current_username
from app.intent.recognizer import LlmClient, OpenAiCompatClient
from app.observability.schemas import (
    AnswerOut,
    AskIn,
    AskOut,
    ClarifyOut,
    ConversationOut,
    FeedbackIn,
    TurnOut,
)
from app.observability.service import NotFoundError, list_conversations, list_turns, save_feedback
from app.pipeline.orchestrator import QueryOrchestrator, TurnOutcome
from app.semantic.model import SemanticError

router = APIRouter(prefix="/api/chat", tags=["chat"])


def get_llm_client(settings: Settings = Depends(get_settings)) -> LlmClient:
    """Separate provider so tests can substitute a stub without patching."""
    return OpenAiCompatClient(settings)


@router.post("/ask", response_model=AskOut)
def post_ask(
    payload: AskIn,
    username: str = Depends(get_current_username),
    session: Session = Depends(get_meta_session),
    connection: Connection = Depends(get_sample_connection),
    client: LlmClient = Depends(get_llm_client),
    settings: Settings = Depends(get_settings),
):
    orchestrator = QueryOrchestrator(
        meta_session=session,
        sample_connection=connection,
        llm_client=client,
        settings=settings,
    )
    try:
        outcome = orchestrator.ask(
            username=username,
            question=payload.question,
            dataset_name=payload.dataset_name,
            conversation_id=payload.conversation_id,
        )
    except SemanticError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="数据集不存在")
    return _to_ask_out(outcome)


def _to_ask_out(outcome: TurnOutcome) -> AskOut:
    return AskOut(
        status=outcome.status.value,
        conversation_id=outcome.conversation_id,
        turn_id=outcome.turn_id,
        answer=AnswerOut.model_validate(_plain(outcome.answer)) if outcome.answer else None,
        clarifications=[
            ClarifyOut.model_validate(_plain(item)) for item in outcome.clarifications
        ],
        refusal_reason=outcome.refusal_reason,
        slot_state=outcome.slot_state,
    )


def _plain(value):
    """Dataclasses (including nested ones) into dicts pydantic can validate."""
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if hasattr(value, "value") and hasattr(value, "name"):
        return value.value
    return value


@router.get("/conversations", response_model=list[ConversationOut])
def get_conversations(
    username: str = Depends(get_current_username),
    session: Session = Depends(get_meta_session),
):
    return list_conversations(session, username)


@router.get("/conversations/{conversation_id}/turns", response_model=list[TurnOut])
def get_conversation_turns(
    conversation_id: int,
    username: str = Depends(get_current_username),
    session: Session = Depends(get_meta_session),
):
    try:
        return list_turns(session, username, conversation_id)
    except NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")


@router.post("/turns/{turn_id}/feedback", status_code=status.HTTP_201_CREATED)
def post_feedback(
    turn_id: int,
    payload: FeedbackIn,
    username: str = Depends(get_current_username),
    session: Session = Depends(get_meta_session),
):
    try:
        save_feedback(session, username, turn_id, payload)
    except NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    return {"ok": True}
```

`_plain` 里的枚举分支处理 `ClarifyKind`：`asdict` 不会把枚举转成字符串，而 `ClarifyOut.kind` 是 `str`。

- [ ] **Step 8: 写 Trace 路由**

`backend/app/api/trace.py`：

```python
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import get_meta_session, get_sample_connection
from app.core.security import get_current_username
from app.observability.schemas import ReplayOut, TraceOut
from app.observability.service import (
    NotFoundError,
    NotReplayableError,
    get_trace,
    replay_turn,
)

router = APIRouter(prefix="/api/trace", tags=["trace"])


@router.get("/turns/{turn_id}", response_model=TraceOut)
def get_turn_trace(
    turn_id: int,
    username: str = Depends(get_current_username),
    session: Session = Depends(get_meta_session),
):
    try:
        return get_trace(session, username, turn_id)
    except NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")


@router.post("/turns/{turn_id}/replay", response_model=ReplayOut)
def post_turn_replay(
    turn_id: int,
    username: str = Depends(get_current_username),
    session: Session = Depends(get_meta_session),
    connection: Connection = Depends(get_sample_connection),
    settings: Settings = Depends(get_settings),
):
    try:
        return replay_turn(
            session, username, turn_id, connection=connection, settings=settings
        )
    except NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    except NotReplayableError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="该轮没有可重放的意图快照"
        )
```

- [ ] **Step 9: 挂载路由**

`backend/app/main.py` 中补上：

```python
from app.api import chat, trace

app.include_router(chat.router)
app.include_router(trace.router)
```

- [ ] **Step 10: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/api -v`
Expected: PASS（语义 API 计划 01 的若干项 + 问答 17 项 + Trace 10 项）

- [ ] **Step 11: 全量回归**

Run: `cd backend && python -m pytest -v`
Expected: PASS

- [ ] **Step 12: 提交**

```bash
git add backend/app/api/chat.py backend/app/api/trace.py backend/app/observability/schemas.py backend/app/observability/service.py backend/app/core/security.py backend/app/core/db.py backend/app/main.py backend/tests/api/test_chat_api.py backend/tests/api/test_trace_api.py
git commit -F - <<'EOF'
开放问答与 Trace 的 HTTP 接口

编排器此前只能在进程内调用，前端拿不到答案、澄清、槽位与 Trace。归属校验若散在路由里，容易出现某个接口只判存在不判归属，凭 id 就能读到别人的会话。

- 拒答与澄清都以 200 正常返回并带 turn_id，保证可追溯而非丢进 HTTP 错误
- 会话、轮次、Trace、重放统一走调用者作用域查询，非本人的 id 与不存在同样回 404
- 负反馈必须归因到五类之一，缺归因在入参校验阶段即拒绝
- 重放只从意图快照重编译，不调模型，且重新过安全改写按当前权限生效
- 身份取 X-Username 头并收敛在单个依赖里，接入真实登录只替换该函数
- 验证：pytest 问答 17 项、Trace 10 项通过，后端全量回归通过
EOF
```

---

## 自查

**Spec 覆盖**（对应设计文档 3.2 全流程、4.4、5.1~5.3、5.7、6 的对话与 Trace 部分、8）：

| Spec 条目 | 承载任务 |
|---|---|
| M-34 Trace（每阶段输入/输出/模型/Token/耗时/错误） | Task 1、6、7 |
| M-17 结构化意图（LLM 只识别意图，不写 SQL） | Task 2 |
| M-18 澄清（低置信度不猜） | Task 3 |
| 5.1 枚举映射失败转澄清，不查空 | Task 3（`_resolve_filters`） |
| 5.2 超过澄清轮数用默认假设，但假设必须写在答案里 | Task 3（`_apply_defaults`）+ Task 5（`Answer.assumptions`） |
| M-19 结构化多轮上下文（槽位覆盖，非聊天记录堆叠） | Task 6（`slot_state`）+ Task 7（`AskOut.slot_state`） |
| M-20 Verified Query 召回 | Task 4 |
| 3.2「Verified Query 的固定 SQL 也必须过安全改写」 | Task 6（命中走 `secure_verified_sql`） |
| M-09 分阶段 Pipeline（七阶段） | Task 6 |
| M-16 引证（口径、时间、过滤、权限附加、数据时间） | Task 5 |
| 6「← 数据权限自动附加」必须显式出现 | Task 5（`source="permission"` 的引证行） |
| M-15 阻断级问题不给数字 | Task 5（`ResultNotAnswerableError`）+ Task 6（转 FAILED） |
| M-38 反馈归因采集 | Task 1（`FeedbackRow`）+ Task 7（`FeedbackIn` 校验） |
| 5.6 越权拒答不泄漏元数据 | Task 6（统一固定文案）+ Task 7（响应无泄漏断言） |
| M-39 工作台所需的后端契约 | Task 7 |

**测试规模**：Task 1 的 9 项 + Task 2 的 23 项 + Task 3 的 21 项 + Task 4 的 11 项 + Task 5 的 16 项 + Task 6 的 22 项 + Task 7 的 27 项 = 129 项。其中 Task 2 的 23 项拆为 `test_prompt.py` 10 项与 `test_recognizer.py` 13 项，Task 3 的 21 项拆为 `test_resolve.py` 16 项与 `test_clarify.py` 5 项，Task 6 的 22 项含 `test_schema.py` 新增的 2 项往返测试，Task 7 的 27 项拆为 `test_chat_api.py` 17 项与 `test_trace_api.py` 10 项。

**安全测试清单**（在计划 03 门禁之上新增，同样必须全绿）：

- 无权列不进 Prompt：`test_orchestrator.py::test_denied_columns_are_absent_from_the_prompt`
- Verified Query 命中仍受行级权限：`test_orchestrator.py::test_verified_query_hit_still_gets_row_policy`
- 越权拒答不泄漏元数据：`test_orchestrator.py::test_permission_refusal_leaks_no_metadata`、`test_chat_api.py::test_refusal_response_leaks_no_metadata`
- 模型输出中的 SQL 被拒：`test_recognizer.py::test_payload_containing_sql_is_rejected`
- 重放不是越权通道：`test_trace_api.py::test_replay_applies_the_current_permissions`
- 他人会话与轮次不可读：`test_chat_api.py::test_other_users_conversation_turns_are_404`、`test_trace_api.py::test_trace_of_another_users_turn_is_404`

**类型一致性**：`TurnOutcome.slot_state` 与 `QueryIntent.to_payload()` 同构，额外携带 `clarify_rounds`，`from_payload` 忽略该键；`Answer`/`ClarifyRequest` 是 dataclass，由 `app.api.chat._plain` 转成 pydantic 可校验的 dict；`TraceStageOut` 直接 `from_attributes` 映射 `TraceStageRow`。

**对前序计划的回填**（实施时若前序计划已完成，作为独立小改动提交）：

| 回填项 | 位置 | 原因 |
|---|---|---|
| `QueryIntent.to_payload()` | 计划 02 的意图 Schema | Trace 快照、槽位持久化、Verified 登记都需要可序列化视图 |
| `QueryIntent.from_payload()` | 计划 02 的意图 Schema（Task 6 Step 5） | 追问合并与重放需要从持久化槽位还原 |
| `Settings` 新增 `llm_timeout_seconds: float = 30.0` | 在计划 01 Task 1 Step 4 的 Settings 上补这一项 | Task 2 的模型客户端；其余三项计划 01 已定义 |
| `get_sample_connection` | `app/core/db.py`（计划 04 Task 7 Step 4 自身新增） | 执行与护栏需要请求级样本库连接 |

**一处有意的简化**：Task 6 中 Verified Query 命中时的引证块取数据集首个指标构造（`_verified_citation`）。严格做法是从 `VerifiedQueryRow.intent_snapshot` 还原意图再取口径。之所以先不做：本轮 Verified Query 只由管理员从已成功的轮次登记，快照与固定 SQL 必然同源，而完整还原会把「重放」的逻辑复制一份到编排器里。实施时若发现引证与实际 SQL 不符，改为从快照还原，这是一处局部替换。

**一处已知缺口**：澄清的「回答」目前走同一个 `/api/chat/ask` 接口——用户点选项后前端把选中值拼进问题文本再问一次，由 `slot_state` 保证其余槽位不丢。更准的做法是单独的 `POST /api/chat/turns/{id}/clarify` 直接写槽位，不再过一次意图识别。本轮不做，原因是选项回填后重新识别一遍能顺带纠正首轮的其他误判，而直写槽位会把首轮的错误固化。前端计划里据此约定澄清回填的交互。

## 交付物

完成本计划后：后端具备完整的问数闭环——提问、澄清、作答、引证、反馈、Trace、重放都有 HTTP 接口，可用 curl 或 Swagger 端到端跑通。**此时还没有界面**，那是计划 05。
