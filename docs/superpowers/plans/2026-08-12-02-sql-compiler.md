# 意图契约与 SQL 编译器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 定义意图 Schema 这一核心契约，并实现把「意图 + 语义模型」确定性编译为 PostgreSQL AST 的编译器。

**Architecture:** 编译器是纯函数：输入 `QueryIntent` 与 `DatasetDef`，输出 `CompiledQuery`（含 sqlglot AST、参数、引证元信息）。不触碰数据库、不调用 LLM、无副作用。SQL 以 sqlglot 表达式树构建而非字符串拼接——后续的 RLS 注入与 AST 白名单都在同一棵树上操作。时间对比通过双 CTE（当期 / 对比期）实现，不用窗口函数。

**Tech Stack:** Python 3.11、sqlglot 25.x、Pydantic v2、pytest

## Global Constraints

以下约束来自 `docs/superpowers/specs/2026-08-12-trusted-query-loop-design.md`，每个任务的要求都隐含包含本节：

- LLM 不生成 SQL。编译器的输入是结构化意图，输出是 SQL AST。
- 同一份意图必须编译出同一条 SQL（可重复，无随机性、无时间依赖）。
- 「允许聚合」是硬约束：字段未标某聚合，编译器拒绝产出该聚合，抛异常而非降级。
- 比率指标标注 `Recalculate`，禁止对其求和。
- 时间对比（同比/环比/YoY/MoM/YTD/MTD）由编译器内置模板生成，不由 LLM 生成。
- 指标的时间口径取自 `MetricDef.time_field`，不由意图指定。
- 编译期失败属于配置问题，错误信息面向管理员，须指明具体指标与字段。
- SQL 方言只支持 PostgreSQL。
- 本轮单数据集，不做多表 Join。
- 代码注释与标识符用英文；文档与提交信息用中文。

## 前置

本计划依赖计划 01 全部完成：`app.semantic.model`（`DatasetDef`/`FieldDef`/`MetricDef`）、`app.semantic.enums`、`tests.semantic.factories.build_orders_dataset`。

---

### Task 1: 意图 Schema（核心契约）

**Files:**
- Create: `backend/app/intent/__init__.py`
- Create: `backend/app/intent/schema.py`
- Create: `backend/tests/intent/__init__.py`
- Create: `backend/tests/intent/test_schema.py`

**Interfaces:**
- Consumes: 无（本计划起点）
- Produces:
  - `app.intent.schema.IntentKind` — `AGGREGATE/TREND/RANKING/DETAIL/UNSUPPORTED`
  - `app.intent.schema.TimeGrain` — `DAY/WEEK/MONTH/QUARTER/YEAR`
  - `app.intent.schema.ComparisonKind` — `NONE/MOM/YOY/WOW/QOQ/YTD/MTD/QTD/PREVIOUS_PERIOD`
  - `app.intent.schema.FilterOperator` — `EQ/NE/IN/NOT_IN/GT/GTE/LT/LTE/BETWEEN`
  - `app.intent.schema.TimeRange` — Pydantic 模型，字段 `start: date`、`end: date`、`grain: TimeGrain`、`expression: str`（原始表述，如「本月」）
  - `app.intent.schema.FilterCondition` — 字段 `field: str`、`operator: FilterOperator`、`values: list[str]`、`spoken_values: list[str]`
  - `app.intent.schema.SortSpec` — 字段 `by: str`（指标名）、`descending: bool`、`limit: int | None`
  - `app.intent.schema.FieldConfidence` — 字段 `metric: float`、`time: float`、`dimension: float`、`filter: float`、`overall: float`
  - `app.intent.schema.QueryIntent` — 字段 `kind: IntentKind`、`dataset: str`、`metrics: list[str]`、`time: TimeRange | None`、`dimensions: list[str]`、`filters: list[FilterCondition]`、`comparison: ComparisonKind`、`sort: SortSpec | None`、`confidence: FieldConfidence`、`assumptions: list[str]`、`raw_question: str`
  - `QueryIntent.slot_signature() -> str` — 稳定字符串，用于 Trace 与 Verified Query 比对
  - `QueryIntent.merge_followup(other) -> QueryIntent` — 多轮上下文槽位合并（M-19）

- [ ] **Step 1: 写失败的契约测试**

`backend/tests/intent/test_schema.py`：

```python
from datetime import date

import pytest
from pydantic import ValidationError

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


def _intent(**overrides) -> QueryIntent:
    payload = {
        "kind": IntentKind.AGGREGATE,
        "dataset": "orders",
        "metrics": ["sales_revenue"],
        "time": TimeRange(
            start=date(2026, 8, 1), end=date(2026, 8, 12), grain=TimeGrain.MONTH, expression="本月"
        ),
        "dimensions": [],
        "filters": [
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=["EC"],
                spoken_values=["华东"],
            )
        ],
        "comparison": ComparisonKind.MOM,
        "confidence": FieldConfidence(metric=0.95, time=0.9, dimension=1.0, filter=0.92, overall=0.9),
        "raw_question": "华东本月销售额环比",
    }
    payload.update(overrides)
    return QueryIntent(**payload)


def test_intent_round_trips_through_json():
    intent = _intent()
    restored = QueryIntent.model_validate_json(intent.model_dump_json())
    assert restored == intent


def test_time_range_rejects_end_before_start():
    with pytest.raises(ValidationError):
        TimeRange(start=date(2026, 8, 12), end=date(2026, 8, 1), grain=TimeGrain.MONTH, expression="")


def test_aggregate_intent_requires_at_least_one_metric():
    with pytest.raises(ValidationError):
        _intent(metrics=[])


def test_slot_signature_is_stable_and_order_insensitive():
    left = _intent(dimensions=["province", "channel"])
    right = _intent(dimensions=["channel", "province"])
    assert left.slot_signature() == right.slot_signature()


def test_slot_signature_changes_with_filter_value():
    base = _intent()
    other = _intent(
        filters=[
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=["SC"],
                spoken_values=["华南"],
            )
        ]
    )
    assert base.slot_signature() != other.slot_signature()


def test_signature_excludes_confidence_and_raw_question():
    # Confidence varies run to run; it must not affect cache identity.
    base = _intent()
    other = _intent(
        confidence=FieldConfidence(metric=0.5, time=0.5, dimension=0.5, filter=0.5, overall=0.5),
        raw_question="换个说法问同一件事",
    )
    assert base.slot_signature() == other.slot_signature()


def test_merge_followup_replaces_only_provided_slots():
    # "那华南呢" changes the filter slot and nothing else (spec M-19).
    base = _intent(dimensions=["province"])
    followup = QueryIntent(
        kind=IntentKind.AGGREGATE,
        dataset="orders",
        metrics=[],
        time=None,
        dimensions=[],
        filters=[
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=["SC"],
                spoken_values=["华南"],
            )
        ],
        comparison=ComparisonKind.NONE,
        confidence=FieldConfidence(metric=0.0, time=0.0, dimension=0.0, filter=0.9, overall=0.9),
        raw_question="那华南呢",
    )

    merged = base.merge_followup(followup)

    assert merged.metrics == ["sales_revenue"]
    assert merged.dimensions == ["province"]
    assert merged.time == base.time
    assert merged.filters[0].values == ["SC"]
    assert merged.raw_question == "那华南呢"


def test_merge_followup_can_add_dimension_without_losing_filter():
    base = _intent()
    followup = QueryIntent(
        kind=IntentKind.AGGREGATE,
        dataset="orders",
        metrics=[],
        time=None,
        dimensions=["province"],
        filters=[],
        comparison=ComparisonKind.NONE,
        confidence=FieldConfidence(metric=0.0, time=0.0, dimension=0.9, filter=0.0, overall=0.9),
        raw_question="按省拆一下",
    )

    merged = base.merge_followup(followup)

    assert merged.dimensions == ["province"]
    assert merged.filters[0].values == ["EC"]
    assert merged.comparison == ComparisonKind.MOM
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/intent/test_schema.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.intent'`

- [ ] **Step 3: 写意图 Schema**

`backend/app/intent/schema.py`：

```python
"""The intent schema is the system's core contract (spec M-17 / 3.3).

It is simultaneously: the LLM's only output, the compiler's only input,
the clarification decision basis, the Trace replay unit, and the evaluation
comparison target. Nothing else in the pipeline may invent its own shape.
"""

import json
from datetime import date
from enum import Enum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IntentKind(str, Enum):
    AGGREGATE = "aggregate"
    TREND = "trend"
    RANKING = "ranking"
    DETAIL = "detail"
    UNSUPPORTED = "unsupported"


class TimeGrain(str, Enum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class ComparisonKind(str, Enum):
    NONE = "none"
    MOM = "mom"
    YOY = "yoy"
    WOW = "wow"
    QOQ = "qoq"
    YTD = "ytd"
    MTD = "mtd"
    QTD = "qtd"
    PREVIOUS_PERIOD = "previous_period"


class FilterOperator(str, Enum):
    EQ = "eq"
    NE = "ne"
    IN = "in"
    NOT_IN = "not_in"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    BETWEEN = "between"


class TimeRange(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: date
    end: date
    grain: TimeGrain = TimeGrain.DAY
    expression: str = ""

    @model_validator(mode="after")
    def check_order(self) -> Self:
        if self.end < self.start:
            raise ValueError("time range end must not precede start")
        return self


class FilterCondition(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    operator: FilterOperator
    values: list[str] = Field(default_factory=list)
    # What the user actually said, kept for citations and clarification copy.
    spoken_values: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_values(self) -> Self:
        if self.operator == FilterOperator.BETWEEN and len(self.values) != 2:
            raise ValueError("between filter requires exactly two values")
        if not self.values:
            raise ValueError("filter requires at least one value")
        return self


class SortSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    by: str
    descending: bool = True
    limit: int | None = None


class FieldConfidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric: float = 0.0
    time: float = 0.0
    dimension: float = 0.0
    filter: float = 0.0
    overall: float = 0.0


class QueryIntent(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: IntentKind
    dataset: str
    metrics: list[str] = Field(default_factory=list)
    time: TimeRange | None = None
    dimensions: list[str] = Field(default_factory=list)
    filters: list[FilterCondition] = Field(default_factory=list)
    comparison: ComparisonKind = ComparisonKind.NONE
    sort: SortSpec | None = None
    confidence: FieldConfidence = FieldConfidence()
    # Defaults applied on the user's behalf must be surfaced in the answer
    # (spec 5.2): an unstated assumption is a silent error.
    assumptions: list[str] = Field(default_factory=list)
    raw_question: str = ""

    @model_validator(mode="after")
    def check_metrics_present(self) -> Self:
        needs_metric = {
            IntentKind.AGGREGATE,
            IntentKind.TREND,
            IntentKind.RANKING,
        }
        if self.kind in needs_metric and not self.metrics:
            raise ValueError(f"{self.kind.value} intent requires at least one metric")
        return self

    def slot_signature(self) -> str:
        """Stable identity of the query slots.

        Excludes confidence, assumptions and raw_question: those vary between
        runs of the same question and must not split the Verified Query cache.
        """
        payload = {
            "kind": self.kind.value,
            "dataset": self.dataset,
            "metrics": sorted(self.metrics),
            "time": None
            if self.time is None
            else {
                "start": self.time.start.isoformat(),
                "end": self.time.end.isoformat(),
                "grain": self.time.grain.value,
            },
            "dimensions": sorted(self.dimensions),
            "filters": sorted(
                (
                    {
                        "field": item.field,
                        "operator": item.operator.value,
                        "values": sorted(item.values),
                    }
                    for item in self.filters
                ),
                key=lambda item: (item["field"], item["operator"]),
            ),
            "comparison": self.comparison.value,
            "sort": None
            if self.sort is None
            else {"by": self.sort.by, "descending": self.sort.descending, "limit": self.sort.limit},
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def merge_followup(self, other: "QueryIntent") -> "QueryIntent":
        """Overlay a follow-up turn's populated slots onto this intent.

        Structured slot carry-over, not chat-history stacking (spec M-19).
        A slot the follow-up left empty keeps its previous value; filters are
        replaced per field so "那华南呢" swaps region without dropping others.
        """
        merged_filters = {item.field: item for item in self.filters}
        for item in other.filters:
            merged_filters[item.field] = item

        return QueryIntent(
            kind=other.kind if other.kind != IntentKind.UNSUPPORTED else self.kind,
            dataset=other.dataset or self.dataset,
            metrics=other.metrics or self.metrics,
            time=other.time or self.time,
            dimensions=other.dimensions or self.dimensions,
            filters=list(merged_filters.values()),
            comparison=(
                other.comparison if other.comparison != ComparisonKind.NONE else self.comparison
            ),
            sort=other.sort or self.sort,
            confidence=other.confidence,
            assumptions=other.assumptions,
            raw_question=other.raw_question,
        )
```

`backend/app/intent/__init__.py`、`backend/tests/intent/__init__.py` 留空。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/intent/test_schema.py -v`
Expected: PASS（9 项）

- [ ] **Step 5: 提交**

```bash
git add backend/app/intent backend/tests/intent
git commit -F - <<'EOF'
定义意图 Schema 作为系统核心契约

意图结构同时是模型的唯一输出、编译器的唯一输入、澄清判断依据、Trace 重放单元与评测比对对象，若各环节各自定义形状，链路无法重放也无法评测。因此先冻结这份契约再实现其他环节。

- 意图模型全部 frozen，含指标、时间、维度、过滤、对比、排序与逐槽位置信度
- slot_signature 排除置信度与原始问法，使同一问题的不同表述命中同一缓存
- merge_followup 按字段覆盖过滤条件，实现结构化多轮而非聊天记录堆叠
- 时间范围校验起止顺序，聚合类意图强制要求至少一个指标
- 验证：pytest tests/intent/test_schema.py 9 项通过
EOF
```

---

### Task 2: 时间对比模板

**Files:**
- Create: `backend/app/compiler/__init__.py`
- Create: `backend/app/compiler/errors.py`
- Create: `backend/app/compiler/time_windows.py`
- Create: `backend/tests/compiler/__init__.py`
- Create: `backend/tests/compiler/test_time_windows.py`

**Interfaces:**
- Consumes: `app.intent.schema.ComparisonKind`、`TimeRange`、`TimeGrain`（Task 1）
- Produces:
  - `app.compiler.errors.CompileError` — 基类，属性 `code: str`、`target: str`、`message: str`
  - `app.compiler.errors.AggregationNotAllowedError`、`UnsupportedComparisonError`、`FieldNotQueryableError`、`FieldNotGroupableError`、`FieldNotFilterableError`、`RatioMetricSumError`、`MetricConfigError`（均继承 `CompileError`）
  - `app.compiler.time_windows.comparison_range(current: TimeRange, comparison: ComparisonKind) -> TimeRange` — 返回对比期区间，`NONE` 抛 `UnsupportedComparisonError`
  - `app.compiler.time_windows.comparison_label(comparison: ComparisonKind) -> str` — 中文标签，用于答案文案

- [ ] **Step 1: 写失败的时间模板测试**

`backend/tests/compiler/test_time_windows.py`：

```python
from datetime import date

import pytest

from app.compiler.errors import UnsupportedComparisonError
from app.compiler.time_windows import comparison_label, comparison_range
from app.intent.schema import ComparisonKind, TimeGrain, TimeRange


def _month_range() -> TimeRange:
    return TimeRange(
        start=date(2026, 8, 1), end=date(2026, 8, 31), grain=TimeGrain.MONTH, expression="本月"
    )


def test_mom_shifts_back_one_calendar_month():
    result = comparison_range(_month_range(), ComparisonKind.MOM)
    assert result.start == date(2026, 7, 1)
    assert result.end == date(2026, 7, 31)


def test_mom_handles_shorter_previous_month():
    # 2026-03-01..2026-03-31 compared month over month lands on February,
    # which has 28 days in 2026. The end must clamp, not overflow into March.
    current = TimeRange(
        start=date(2026, 3, 1), end=date(2026, 3, 31), grain=TimeGrain.MONTH, expression="本月"
    )
    result = comparison_range(current, ComparisonKind.MOM)
    assert result.start == date(2026, 2, 1)
    assert result.end == date(2026, 2, 28)


def test_yoy_shifts_back_one_year():
    result = comparison_range(_month_range(), ComparisonKind.YOY)
    assert result.start == date(2025, 8, 1)
    assert result.end == date(2025, 8, 31)


def test_yoy_handles_leap_day():
    current = TimeRange(
        start=date(2024, 2, 29), end=date(2024, 2, 29), grain=TimeGrain.DAY, expression="当天"
    )
    result = comparison_range(current, ComparisonKind.YOY)
    assert result.start == date(2023, 2, 28)
    assert result.end == date(2023, 2, 28)


def test_wow_shifts_back_seven_days():
    current = TimeRange(
        start=date(2026, 8, 10), end=date(2026, 8, 16), grain=TimeGrain.WEEK, expression="本周"
    )
    result = comparison_range(current, ComparisonKind.WOW)
    assert result.start == date(2026, 8, 3)
    assert result.end == date(2026, 8, 9)


def test_qoq_shifts_back_three_months():
    current = TimeRange(
        start=date(2026, 7, 1), end=date(2026, 9, 30), grain=TimeGrain.QUARTER, expression="本季"
    )
    result = comparison_range(current, ComparisonKind.QOQ)
    assert result.start == date(2026, 4, 1)
    assert result.end == date(2026, 6, 30)


def test_previous_period_uses_same_length_window():
    current = TimeRange(
        start=date(2026, 8, 1), end=date(2026, 8, 12), grain=TimeGrain.DAY, expression="本月至今"
    )
    result = comparison_range(current, ComparisonKind.PREVIOUS_PERIOD)
    assert result.start == date(2026, 7, 20)
    assert result.end == date(2026, 7, 31)


def test_ytd_compares_against_same_span_last_year():
    current = TimeRange(
        start=date(2026, 1, 1), end=date(2026, 8, 12), grain=TimeGrain.DAY, expression="年初至今"
    )
    result = comparison_range(current, ComparisonKind.YTD)
    assert result.start == date(2025, 1, 1)
    assert result.end == date(2025, 8, 12)


def test_mtd_compares_against_same_span_last_month():
    current = TimeRange(
        start=date(2026, 8, 1), end=date(2026, 8, 12), grain=TimeGrain.DAY, expression="本月至今"
    )
    result = comparison_range(current, ComparisonKind.MTD)
    assert result.start == date(2026, 7, 1)
    assert result.end == date(2026, 7, 12)


def test_qtd_compares_against_same_span_last_quarter():
    current = TimeRange(
        start=date(2026, 7, 1), end=date(2026, 8, 12), grain=TimeGrain.DAY, expression="本季至今"
    )
    result = comparison_range(current, ComparisonKind.QTD)
    assert result.start == date(2026, 4, 1)
    assert result.end == date(2026, 5, 12)


def test_comparison_none_is_rejected():
    with pytest.raises(UnsupportedComparisonError):
        comparison_range(_month_range(), ComparisonKind.NONE)


def test_labels_are_chinese():
    assert comparison_label(ComparisonKind.MOM) == "环比"
    assert comparison_label(ComparisonKind.YOY) == "同比"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/compiler/test_time_windows.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.compiler'`

- [ ] **Step 3: 写编译异常**

`backend/app/compiler/errors.py`：

```python
"""Compile-time failures.

These are configuration problems, not runtime problems (spec 5.3): the message
is written for an administrator and must name the offending metric or field.
"""


class CompileError(Exception):
    code = "COMPILE_ERROR"

    def __init__(self, target: str, message: str) -> None:
        self.target = target
        self.message = message
        super().__init__(f"[{self.code}] {target}: {message}")


class AggregationNotAllowedError(CompileError):
    code = "AGGREGATION_NOT_ALLOWED"


class RatioMetricSumError(CompileError):
    code = "RATIO_METRIC_SUM"


class UnsupportedComparisonError(CompileError):
    code = "UNSUPPORTED_COMPARISON"


class FieldNotQueryableError(CompileError):
    code = "FIELD_NOT_QUERYABLE"


class FieldNotGroupableError(CompileError):
    code = "FIELD_NOT_GROUPABLE"


class FieldNotFilterableError(CompileError):
    code = "FIELD_NOT_FILTERABLE"


class MetricConfigError(CompileError):
    code = "METRIC_CONFIG_ERROR"
```

- [ ] **Step 4: 写时间模板**

`backend/app/compiler/time_windows.py`：

```python
"""Built-in time comparison templates (spec M-05 / 4.5).

Windows are computed here, in Python, never generated by the LLM: hand-written
window functions are a high-error-rate path and the same question must always
produce the same range.
"""

from calendar import monthrange
from datetime import date, timedelta

from app.compiler.errors import UnsupportedComparisonError
from app.intent.schema import ComparisonKind, TimeRange

_LABELS = {
    ComparisonKind.MOM: "环比",
    ComparisonKind.YOY: "同比",
    ComparisonKind.WOW: "周环比",
    ComparisonKind.QOQ: "季环比",
    ComparisonKind.YTD: "年初至今同比",
    ComparisonKind.MTD: "月初至今环比",
    ComparisonKind.QTD: "季初至今环比",
    ComparisonKind.PREVIOUS_PERIOD: "上一周期",
}


def _shift_months(value: date, months: int) -> date:
    """Shift a date by whole months, clamping to the target month's length."""
    total = value.year * 12 + (value.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def _shift_years(value: date, years: int) -> date:
    return _shift_months(value, years * 12)


def comparison_range(current: TimeRange, comparison: ComparisonKind) -> TimeRange:
    """Return the baseline window for a comparison against `current`."""
    if comparison == ComparisonKind.NONE:
        raise UnsupportedComparisonError("comparison", "无对比方式时不应请求对比区间")

    if comparison == ComparisonKind.MOM:
        start = _shift_months(current.start, -1)
        end = _shift_months(current.end, -1)
    elif comparison in (ComparisonKind.YOY, ComparisonKind.YTD):
        start = _shift_years(current.start, -1)
        end = _shift_years(current.end, -1)
    elif comparison == ComparisonKind.WOW:
        start = current.start - timedelta(days=7)
        end = current.end - timedelta(days=7)
    elif comparison == ComparisonKind.QOQ:
        start = _shift_months(current.start, -3)
        end = _shift_months(current.end, -3)
    elif comparison == ComparisonKind.MTD:
        start = _shift_months(current.start, -1)
        end = _shift_months(current.end, -1)
    elif comparison == ComparisonKind.QTD:
        start = _shift_months(current.start, -3)
        end = _shift_months(current.end, -3)
    else:  # PREVIOUS_PERIOD: immediately preceding window of equal length.
        span = current.end - current.start
        end = current.start - timedelta(days=1)
        start = end - span

    return TimeRange(
        start=start,
        end=end,
        grain=current.grain,
        expression=f"{_LABELS[comparison]}对比期",
    )


def comparison_label(comparison: ComparisonKind) -> str:
    if comparison == ComparisonKind.NONE:
        return ""
    return _LABELS[comparison]
```

`backend/app/compiler/__init__.py`、`backend/tests/compiler/__init__.py` 留空。

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/compiler/test_time_windows.py -v`
Expected: PASS（12 项）

- [ ] **Step 6: 提交**

```bash
git add backend/app/compiler backend/tests/compiler
git commit -F - <<'EOF'
实现时间对比内置模板

企业问数中过半带时间对比，交给模型现写窗口函数错误率高且同一问题两次结果可能不同。改为在编译期用日历运算得出对比区间，并处理月末与闰日的边界。

- 支持环比、同比、周环比、季环比、上一周期与三类至今口径
- 月份平移按目标月实际天数截断，避免 3 月 31 日回推到 2 月溢出
- 闰日同比回退到上一年 2 月 28 日而非报错
- 编译异常统一携带 code 与 target，错误信息面向管理员定位配置问题
- 验证：pytest tests/compiler/test_time_windows.py 12 项通过
EOF
```

---

### Task 3: 指标表达式构建

**Files:**
- Create: `backend/app/compiler/metrics.py`
- Create: `backend/tests/compiler/test_metrics.py`

**Interfaces:**
- Consumes: `app.semantic.model.DatasetDef`/`MetricDef`、`app.semantic.enums.Aggregation`/`MetricKind`/`AggregationBehavior`、`app.compiler.errors.*`
- Produces:
  - `app.compiler.metrics.build_metric_expression(dataset, metric) -> sqlglot.exp.Expression` — 单个指标的聚合表达式（含 `fixed_filter` 的 `FILTER (WHERE ...)`）
  - `app.compiler.metrics.build_metric_projection(dataset, metric) -> sqlglot.exp.Alias` — 带 `AS <metric.name>` 的投影
  - `app.compiler.metrics.resolve_metric_dependencies(dataset, metric) -> list[MetricDef]` — 复合/比率指标依赖的原子指标，拓扑有序、去重
  - `app.compiler.metrics.assert_aggregation_allowed(dataset, metric) -> None` — 违反「允许聚合」时抛 `AggregationNotAllowedError`

- [ ] **Step 1: 写失败的指标表达式测试**

`backend/tests/compiler/test_metrics.py`：

```python
from dataclasses import replace

import pytest

from app.compiler.errors import AggregationNotAllowedError, MetricConfigError, RatioMetricSumError
from app.compiler.metrics import (
    assert_aggregation_allowed,
    build_metric_projection,
    resolve_metric_dependencies,
)
from app.semantic.enums import Aggregation, AggregationBehavior, MetricKind
from app.semantic.loader import load_dataset
from app.semantic.model import MetricDef
from tests.semantic.factories import build_orders_dataset


def _sql(expression) -> str:
    return expression.sql(dialect="postgres")


@pytest.fixture
def orders(meta_session):
    build_orders_dataset(meta_session)
    return load_dataset(meta_session, "orders")


def test_atomic_metric_compiles_to_filtered_sum(orders):
    projection = build_metric_projection(orders, orders.metric("sales_revenue"))
    sql = _sql(projection)
    assert "SUM(" in sql
    assert "amount" in sql
    # fixed_filter must ride along as a FILTER clause, not leak into WHERE.
    assert "FILTER(WHERE" in sql.replace(" ", "").replace("FILTER (WHERE", "FILTER(WHERE")
    assert "AS sales_revenue" in sql


def test_count_metric_uses_count(orders):
    sql = _sql(build_metric_projection(orders, orders.metric("order_count")))
    assert "COUNT(" in sql
    assert "AS order_count" in sql


def test_derived_metric_includes_its_own_filter(orders):
    sql = _sql(build_metric_projection(orders, orders.metric("new_customer_revenue")))
    assert "is_new_customer" in sql


def test_ratio_metric_divides_dependencies_with_null_guard(orders):
    sql = _sql(build_metric_projection(orders, orders.metric("gross_margin_rate")))
    # Division must be guarded: spec 5.5 lists divide-by-zero as a checked case.
    assert "NULLIF" in sql.upper()
    assert "AS gross_margin_rate" in sql


def test_ratio_dependencies_are_resolved_in_order(orders):
    deps = resolve_metric_dependencies(orders, orders.metric("gross_margin_rate"))
    names = [item.name for item in deps]
    assert set(names) == {"sales_revenue", "total_cost"}


def test_atomic_metric_has_no_dependencies(orders):
    assert resolve_metric_dependencies(orders, orders.metric("sales_revenue")) == []


def test_disallowed_aggregation_is_rejected(orders):
    # province allows no aggregation at all.
    bad = MetricDef(
        name="bad_metric",
        business_name="错误指标",
        kind=MetricKind.ATOMIC.value,
        time_field="completed_date",
        source_field="province",
        aggregation=Aggregation.SUM.value,
    )
    with pytest.raises(AggregationNotAllowedError) as excinfo:
        assert_aggregation_allowed(orders, bad)
    assert "province" in str(excinfo.value)


def test_summing_a_ratio_metric_is_rejected(orders):
    ratio = orders.metric("gross_margin_rate")
    summable = replace(
        ratio,
        kind=MetricKind.ATOMIC.value,
        aggregation=Aggregation.SUM.value,
        source_field="amount",
        aggregation_behavior=AggregationBehavior.RECALCULATE.value,
    )
    with pytest.raises(RatioMetricSumError):
        assert_aggregation_allowed(orders, summable)


def test_metric_missing_source_field_is_rejected(orders):
    broken = MetricDef(
        name="broken",
        business_name="缺字段",
        kind=MetricKind.ATOMIC.value,
        time_field="completed_date",
        source_field=None,
        aggregation=Aggregation.SUM.value,
    )
    with pytest.raises(MetricConfigError):
        build_metric_projection(orders, broken)


def test_compilation_is_repeatable(orders):
    metric = orders.metric("sales_revenue")
    first = _sql(build_metric_projection(orders, metric))
    second = _sql(build_metric_projection(orders, metric))
    assert first == second
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/compiler/test_metrics.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.compiler.metrics'`

- [ ] **Step 3: 写指标表达式构建器**

`backend/app/compiler/metrics.py`：

```python
"""Metric expression construction.

Aggregations come from the semantic layer's allowed_aggregations, which is a
hard constraint (spec 4.2): a field not marked SUM never yields SUM. This is
what stops the agent from summing balances or customer tiers — errors the
business side would almost never notice.
"""

import sqlglot
from sqlglot import exp

from app.compiler.errors import (
    AggregationNotAllowedError,
    MetricConfigError,
    RatioMetricSumError,
)
from app.semantic.enums import Aggregation, AggregationBehavior, MetricKind
from app.semantic.model import DatasetDef, MetricDef

_AGGREGATION_NODES: dict[str, type[exp.AggFunc]] = {
    Aggregation.SUM.value: exp.Sum,
    Aggregation.COUNT.value: exp.Count,
    Aggregation.AVG.value: exp.Avg,
    Aggregation.MAX.value: exp.Max,
    Aggregation.MIN.value: exp.Min,
}

_AGGREGATE_KINDS = (MetricKind.ATOMIC.value, MetricKind.DERIVED.value)
_EXPRESSION_KINDS = (MetricKind.COMPOSITE.value, MetricKind.RATIO.value)


def assert_aggregation_allowed(dataset: DatasetDef, metric: MetricDef) -> None:
    """Reject aggregations the semantic layer has not whitelisted."""
    if metric.kind not in _AGGREGATE_KINDS:
        return

    if (
        metric.aggregation_behavior == AggregationBehavior.RECALCULATE.value
        and metric.aggregation == Aggregation.SUM.value
    ):
        raise RatioMetricSumError(
            metric.name, "该指标标注为 recalculate，不允许求和，必须按公式重算"
        )

    if not metric.source_field or not dataset.has_field(metric.source_field):
        raise MetricConfigError(metric.name, f"指标引用的字段不存在：{metric.source_field}")

    field = dataset.field(metric.source_field)
    if metric.aggregation not in field.allowed_aggregations:
        raise AggregationNotAllowedError(
            metric.name,
            f"字段 {metric.source_field} 的允许聚合为 "
            f"{list(field.allowed_aggregations)}，不含 {metric.aggregation}",
        )


def _parse_condition(metric: MetricDef, condition: str) -> exp.Expression:
    try:
        return sqlglot.parse_one(condition, dialect="postgres")
    except Exception as error:  # sqlglot raises several parse error types
        raise MetricConfigError(metric.name, f"指标限定条件无法解析：{condition}") from error


def _build_aggregate(dataset: DatasetDef, metric: MetricDef) -> exp.Expression:
    assert_aggregation_allowed(dataset, metric)

    node_type = _AGGREGATION_NODES.get(metric.aggregation or "")
    column = exp.column(dataset.field(metric.source_field).physical_column)

    if metric.aggregation == Aggregation.DISTINCT_COUNT.value:
        aggregate: exp.Expression = exp.Count(this=exp.Distinct(expressions=[column]))
    elif node_type is None:
        raise MetricConfigError(metric.name, f"不支持的聚合方式：{metric.aggregation}")
    else:
        aggregate = node_type(this=column)

    if metric.fixed_filter.strip():
        aggregate = exp.Filter(
            this=aggregate,
            expression=exp.Where(this=_parse_condition(metric, metric.fixed_filter)),
        )
    return aggregate


def resolve_metric_dependencies(dataset: DatasetDef, metric: MetricDef) -> list[MetricDef]:
    """Atomic metrics a composite/ratio metric depends on, de-duplicated."""
    if metric.kind not in _EXPRESSION_KINDS:
        return []

    try:
        tree = sqlglot.parse_one(metric.expression, dialect="postgres")
    except Exception as error:
        raise MetricConfigError(metric.name, f"指标表达式无法解析：{metric.expression}") from error

    known = {item.name for item in dataset.metrics}
    resolved: list[MetricDef] = []
    seen: set[str] = set()
    for column in tree.find_all(exp.Column):
        name = column.name
        if name in known and name not in seen and name != metric.name:
            seen.add(name)
            resolved.append(dataset.metric(name))
    return resolved


def _build_expression_metric(dataset: DatasetDef, metric: MetricDef) -> exp.Expression:
    dependencies = resolve_metric_dependencies(dataset, metric)
    if not dependencies:
        raise MetricConfigError(metric.name, "复合指标表达式未引用任何已知指标")

    substitutions = {
        item.name: _build_aggregate(dataset, item) for item in dependencies
    }
    tree = sqlglot.parse_one(metric.expression, dialect="postgres")

    def substitute(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and node.name in substitutions:
            return substitutions[node.name].copy()
        return node

    tree = tree.transform(substitute)

    # Guard division so an empty denominator yields NULL instead of an error
    # (spec 5.5 treats divide-by-zero as a checked failure).
    def guard_division(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Div):
            return exp.Div(
                this=node.this,
                expression=exp.func("NULLIF", node.expression, exp.Literal.number(0)),
            )
        return node

    return tree.transform(guard_division)


def build_metric_expression(dataset: DatasetDef, metric: MetricDef) -> exp.Expression:
    if metric.kind in _AGGREGATE_KINDS:
        return _build_aggregate(dataset, metric)
    if metric.kind in _EXPRESSION_KINDS:
        return _build_expression_metric(dataset, metric)
    raise MetricConfigError(metric.name, f"未知的指标类型：{metric.kind}")


def build_metric_projection(dataset: DatasetDef, metric: MetricDef) -> exp.Alias:
    return exp.alias_(build_metric_expression(dataset, metric), metric.name)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/compiler/test_metrics.py -v`
Expected: PASS（10 项）

- [ ] **Step 5: 提交**

```bash
git add backend/app/compiler/metrics.py backend/tests/compiler/test_metrics.py
git commit -F - <<'EOF'
实现四类指标的表达式构建

指标口径若在每次提问时现场生成，同一问题两次结果可能不同，企业无法接受。改为从指标定义确定性地构建聚合表达式，并把允许聚合作为硬约束在此处拦截。

- 原子与派生指标编译为带 FILTER 子句的聚合，限定条件不泄漏到外层 WHERE
- 复合与比率指标按表达式引用的依赖指标做原地替换，除法统一包 NULLIF 防除零
- 字段未标注对应聚合时直接抛错并回显允许聚合列表，不做降级
- 标注 recalculate 的指标被求和时单独拦截，避免比率指标在汇总时算错
- 验证：pytest tests/compiler/test_metrics.py 10 项通过
EOF
```

---

### Task 4: 过滤与维度编译

**Files:**
- Create: `backend/app/compiler/predicates.py`
- Create: `backend/tests/compiler/test_predicates.py`

**Interfaces:**
- Consumes: `app.intent.schema.FilterCondition`/`FilterOperator`/`TimeRange`、`app.semantic.model.DatasetDef`、`app.compiler.errors.*`
- Produces:
  - `app.compiler.predicates.build_filter_predicate(dataset, condition) -> exp.Expression`
  - `app.compiler.predicates.build_time_predicate(dataset, time_field, window) -> exp.Expression` — 生成 `BETWEEN` 谓词
  - `app.compiler.predicates.build_dimension_projection(dataset, name) -> exp.Alias`
  - `app.compiler.predicates.combine_predicates(items) -> exp.Expression | None` — 以 `AND` 归并，空列表返回 `None`

- [ ] **Step 1: 写失败的谓词测试**

`backend/tests/compiler/test_predicates.py`：

```python
from datetime import date

import pytest

from app.compiler.errors import FieldNotFilterableError, FieldNotGroupableError
from app.compiler.predicates import (
    build_dimension_projection,
    build_filter_predicate,
    build_time_predicate,
    combine_predicates,
)
from app.intent.schema import FilterCondition, FilterOperator, TimeGrain, TimeRange
from app.semantic.loader import load_dataset
from tests.semantic.factories import build_orders_dataset


def _sql(expression) -> str:
    return expression.sql(dialect="postgres")


@pytest.fixture
def orders(meta_session):
    build_orders_dataset(meta_session)
    return load_dataset(meta_session, "orders")


def test_in_filter_uses_physical_values(orders):
    condition = FilterCondition(
        field="region_code",
        operator=FilterOperator.IN,
        values=["EC", "SC"],
        spoken_values=["华东", "华南"],
    )
    sql = _sql(build_filter_predicate(orders, condition))
    assert "region_code" in sql
    assert "'EC'" in sql and "'SC'" in sql
    # The spoken form must never reach SQL.
    assert "华东" not in sql


def test_eq_filter_compiles_to_equality(orders):
    condition = FilterCondition(
        field="channel", operator=FilterOperator.EQ, values=["online"], spoken_values=["线上"]
    )
    assert "=" in _sql(build_filter_predicate(orders, condition))


def test_between_filter_compiles_to_between(orders):
    condition = FilterCondition(
        field="created_date",
        operator=FilterOperator.BETWEEN,
        values=["2026-08-01", "2026-08-31"],
        spoken_values=["八月"],
    )
    assert "BETWEEN" in _sql(build_filter_predicate(orders, condition)).upper()


def test_comparison_operators_compile(orders):
    for operator, token in (
        (FilterOperator.GT, ">"),
        (FilterOperator.GTE, ">="),
        (FilterOperator.LT, "<"),
        (FilterOperator.LTE, "<="),
    ):
        condition = FilterCondition(
            field="amount", operator=operator, values=["1000"], spoken_values=["一千"]
        )
        assert token in _sql(build_filter_predicate(orders, condition))


def test_not_in_filter_compiles(orders):
    condition = FilterCondition(
        field="status", operator=FilterOperator.NOT_IN, values=["cancelled"], spoken_values=["已取消"]
    )
    sql = _sql(build_filter_predicate(orders, condition)).upper()
    assert "NOT" in sql and "IN" in sql


def test_filter_on_non_filterable_field_is_rejected(meta_session):
    from dataclasses import replace

    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    locked = tuple(
        replace(item, is_filterable=False) if item.name == "cost" else item
        for item in dataset.fields
    )
    dataset = replace(dataset, fields=locked)

    condition = FilterCondition(
        field="cost", operator=FilterOperator.GT, values=["1"], spoken_values=["一"]
    )
    with pytest.raises(FieldNotFilterableError):
        build_filter_predicate(dataset, condition)


def test_time_predicate_is_inclusive_between(orders):
    window = TimeRange(
        start=date(2026, 8, 1), end=date(2026, 8, 12), grain=TimeGrain.MONTH, expression="本月"
    )
    sql = _sql(build_time_predicate(orders, "completed_date", window)).upper()
    assert "BETWEEN" in sql
    assert "2026-08-01" in sql and "2026-08-12" in sql


def test_dimension_projection_uses_business_alias(orders):
    sql = _sql(build_dimension_projection(orders, "province"))
    assert "province" in sql


def test_non_groupable_field_cannot_be_a_dimension(orders):
    # amount is a measure, not a dimension.
    with pytest.raises(FieldNotGroupableError):
        build_dimension_projection(orders, "amount")


def test_combine_predicates_joins_with_and(orders):
    left = build_filter_predicate(
        orders,
        FilterCondition(
            field="region_code", operator=FilterOperator.IN, values=["EC"], spoken_values=["华东"]
        ),
    )
    right = build_filter_predicate(
        orders,
        FilterCondition(
            field="channel", operator=FilterOperator.EQ, values=["online"], spoken_values=["线上"]
        ),
    )
    assert "AND" in _sql(combine_predicates([left, right])).upper()


def test_combine_predicates_returns_none_when_empty():
    assert combine_predicates([]) is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/compiler/test_predicates.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.compiler.predicates'`

- [ ] **Step 3: 写谓词构建器**

`backend/app/compiler/predicates.py`：

```python
"""Filter, time and dimension construction.

Values reaching SQL are always physical values resolved through the enum
dictionary upstream; spoken forms are kept only for citations.
"""

from functools import reduce

from sqlglot import exp

from app.compiler.errors import FieldNotFilterableError, FieldNotGroupableError
from app.intent.schema import FilterCondition, FilterOperator, TimeRange
from app.semantic.model import DatasetDef

_BINARY_NODES: dict[str, type[exp.Binary]] = {
    FilterOperator.EQ.value: exp.EQ,
    FilterOperator.NE.value: exp.NEQ,
    FilterOperator.GT.value: exp.GT,
    FilterOperator.GTE.value: exp.GTE,
    FilterOperator.LT.value: exp.LT,
    FilterOperator.LTE.value: exp.LTE,
}


def _literal(value: str) -> exp.Expression:
    """Render a value as a SQL literal.

    Numeric-looking values become numbers so comparisons on numeric columns do
    not force a cast; everything else stays a quoted string.
    """
    try:
        float(value)
    except ValueError:
        return exp.Literal.string(value)
    return exp.Literal.number(value)


def build_filter_predicate(dataset: DatasetDef, condition: FilterCondition) -> exp.Expression:
    field = dataset.field(condition.field)
    if not field.is_filterable:
        raise FieldNotFilterableError(
            f"{dataset.name}.{field.name}", "该字段在语义配置中标记为不可筛选"
        )

    column = exp.column(field.physical_column)

    if condition.operator == FilterOperator.IN:
        return exp.In(this=column, expressions=[_literal(item) for item in condition.values])
    if condition.operator == FilterOperator.NOT_IN:
        return exp.Not(
            this=exp.In(this=column, expressions=[_literal(item) for item in condition.values])
        )
    if condition.operator == FilterOperator.BETWEEN:
        low, high = condition.values
        return exp.Between(this=column, low=_literal(low), high=_literal(high))

    node_type = _BINARY_NODES[condition.operator.value]
    return node_type(this=column, expression=_literal(condition.values[0]))


def build_time_predicate(
    dataset: DatasetDef, time_field: str, window: TimeRange
) -> exp.Expression:
    """Inclusive date window on the metric's declared time field."""
    field = dataset.field(time_field)
    return exp.Between(
        this=exp.column(field.physical_column),
        low=exp.cast(exp.Literal.string(window.start.isoformat()), "date"),
        high=exp.cast(exp.Literal.string(window.end.isoformat()), "date"),
    )


def build_dimension_projection(dataset: DatasetDef, name: str) -> exp.Alias:
    field = dataset.field(name)
    if not field.is_groupable:
        raise FieldNotGroupableError(
            f"{dataset.name}.{field.name}", "该字段在语义配置中标记为不可分组"
        )
    return exp.alias_(exp.column(field.physical_column), field.name)


def combine_predicates(items: list[exp.Expression]) -> exp.Expression | None:
    if not items:
        return None
    return reduce(lambda left, right: exp.And(this=left, expression=right), items)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/compiler/test_predicates.py -v`
Expected: PASS（11 项）

- [ ] **Step 5: 提交**

```bash
git add backend/app/compiler/predicates.py backend/tests/compiler/test_predicates.py
git commit -F - <<'EOF'
实现过滤条件、时间窗口与维度的谓词构建

过滤值必须以物理值进入 SQL，用户口语表述只用于引证，否则枚举映射失效会直接查空。同时可筛选与可分组标记需要在编译期强制，避免把度量字段当维度分组。

- 支持等值、不等、区间、大小比较与集合的正反向匹配
- 数值型字面量不加引号，避免在数值列上产生隐式转换
- 时间谓词按指标声明的时间口径字段生成闭区间，并显式转为 date 类型
- 字段标记为不可筛选或不可分组时抛出定位到具体字段的编译错误
- 验证：pytest tests/compiler/test_predicates.py 11 项通过
EOF
```

---

### Task 5: 查询编译器主体

**Files:**
- Create: `backend/app/compiler/query.py`
- Create: `backend/tests/compiler/test_query_compiler.py`

**Interfaces:**
- Consumes: Task 2、3、4 的全部导出、`app.intent.schema.QueryIntent`、`app.semantic.model.DatasetDef`
- Produces:
  - `app.compiler.query.Citation` — frozen dataclass，属性 `metric_name: str`、`metric_business_name: str`、`metric_version: int`、`metric_description: str`、`time_field_business_name: str`、`time_start: date`、`time_end: date`、`filters: tuple[str, ...]`、`comparison_label: str`
  - `app.compiler.query.CompiledQuery` — frozen dataclass，属性 `ast: exp.Expression`、`sql: str`、`dataset_name: str`、`physical_table: str`、`metric_names: tuple[str, ...]`、`dimension_names: tuple[str, ...]`、`citation: Citation`、`comparison_metric_names: tuple[str, ...]`
  - `app.compiler.query.compile_intent(dataset: DatasetDef, intent: QueryIntent) -> CompiledQuery`

- [ ] **Step 1: 写失败的编译器测试**

`backend/tests/compiler/test_query_compiler.py`：

```python
from datetime import date

import pytest

from app.compiler.errors import CompileError, FieldNotGroupableError
from app.compiler.query import compile_intent
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
from app.semantic.loader import load_dataset
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def orders(meta_session):
    build_orders_dataset(meta_session)
    return load_dataset(meta_session, "orders")


def _august() -> TimeRange:
    return TimeRange(
        start=date(2026, 8, 1), end=date(2026, 8, 31), grain=TimeGrain.MONTH, expression="本月"
    )


def _intent(**overrides) -> QueryIntent:
    payload = {
        "kind": IntentKind.AGGREGATE,
        "dataset": "orders",
        "metrics": ["sales_revenue"],
        "time": _august(),
        "confidence": FieldConfidence(overall=0.9),
        "raw_question": "本月销售额",
    }
    payload.update(overrides)
    return QueryIntent(**payload)


def test_simple_aggregate_compiles(orders):
    compiled = compile_intent(orders, _intent())

    assert "SELECT" in compiled.sql.upper()
    assert "sample.orders" in compiled.sql
    assert "sales_revenue" in compiled.sql
    assert compiled.metric_names == ("sales_revenue",)


def test_time_window_uses_metric_time_field(orders):
    # sales_revenue is measured on completed_date, not created_date.
    compiled = compile_intent(orders, _intent())
    assert "completed_date" in compiled.sql
    assert "created_date" not in compiled.sql


def test_dimension_produces_group_by(orders):
    compiled = compile_intent(orders, _intent(dimensions=["province"]))

    assert "GROUP BY" in compiled.sql.upper()
    assert compiled.dimension_names == ("province",)


def test_filter_values_are_physical(orders):
    compiled = compile_intent(
        orders,
        _intent(
            filters=[
                FilterCondition(
                    field="region_code",
                    operator=FilterOperator.IN,
                    values=["EC"],
                    spoken_values=["华东"],
                )
            ]
        ),
    )
    assert "'EC'" in compiled.sql


def test_ranking_intent_applies_order_and_limit(orders):
    compiled = compile_intent(
        orders,
        _intent(
            kind=IntentKind.RANKING,
            dimensions=["province"],
            sort=SortSpec(by="sales_revenue", descending=True, limit=3),
        ),
    )
    upper = compiled.sql.upper()
    assert "ORDER BY" in upper
    assert "DESC" in upper
    assert "LIMIT 3" in upper


def test_comparison_produces_both_periods(orders):
    compiled = compile_intent(orders, _intent(comparison=ComparisonKind.MOM))

    # Current and baseline windows must both appear.
    assert "2026-08-01" in compiled.sql
    assert "2026-07-01" in compiled.sql
    assert compiled.comparison_metric_names == ("sales_revenue_comparison",)
    assert compiled.citation.comparison_label == "环比"


def test_citation_carries_metric_version_and_time_basis(orders):
    compiled = compile_intent(orders, _intent())

    assert compiled.citation.metric_name == "sales_revenue"
    assert compiled.citation.metric_version == 3
    assert compiled.citation.time_field_business_name == "完成日期"
    assert compiled.citation.time_start == date(2026, 8, 1)


def test_citation_renders_filters_in_business_terms(orders):
    compiled = compile_intent(
        orders,
        _intent(
            filters=[
                FilterCondition(
                    field="region_code",
                    operator=FilterOperator.IN,
                    values=["EC"],
                    spoken_values=["华东"],
                )
            ]
        ),
    )
    assert any("大区" in item and "华东" in item for item in compiled.citation.filters)


def test_ratio_metric_compiles_without_group_by_sum(orders):
    compiled = compile_intent(orders, _intent(metrics=["gross_margin_rate"]))
    assert "NULLIF" in compiled.sql.upper()


def test_multiple_metrics_compile_together(orders):
    compiled = compile_intent(orders, _intent(metrics=["sales_revenue", "order_count"]))
    assert compiled.metric_names == ("sales_revenue", "order_count")
    assert "order_count" in compiled.sql


def test_measure_as_dimension_is_rejected(orders):
    with pytest.raises(FieldNotGroupableError):
        compile_intent(orders, _intent(dimensions=["amount"]))


def test_unsupported_intent_is_rejected(orders):
    intent = QueryIntent(
        kind=IntentKind.UNSUPPORTED,
        dataset="orders",
        metrics=[],
        confidence=FieldConfidence(overall=0.2),
        raw_question="帮我下单",
    )
    with pytest.raises(CompileError):
        compile_intent(orders, intent)


def test_unpublished_dataset_is_rejected(meta_session):
    build_orders_dataset(meta_session, published=False)
    dataset = load_dataset(meta_session, "orders")

    with pytest.raises(CompileError):
        compile_intent(dataset, _intent())


def test_same_intent_compiles_to_identical_sql(orders):
    intent = _intent(dimensions=["province"], comparison=ComparisonKind.YOY)
    assert compile_intent(orders, intent).sql == compile_intent(orders, intent).sql
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/compiler/test_query_compiler.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.compiler.query'`

- [ ] **Step 3: 写编译器主体**

`backend/app/compiler/query.py`：

```python
"""The SQL compiler (spec 2 / 3.2 stage 4).

Pure function: intent + semantic model in, SQL AST out. No database access,
no LLM call, no clock read — the same intent must always produce the same SQL.

Comparison queries are built as two CTEs (current window and baseline window)
joined on the grouping dimensions, rather than window functions: the shape
stays readable in Trace and each side can be inspected on its own.
"""

from dataclasses import dataclass
from datetime import date

from sqlglot import exp

from app.compiler.errors import CompileError, FieldNotQueryableError
from app.compiler.metrics import build_metric_projection
from app.compiler.predicates import (
    build_dimension_projection,
    build_filter_predicate,
    build_time_predicate,
    combine_predicates,
)
from app.compiler.time_windows import comparison_label, comparison_range
from app.intent.schema import ComparisonKind, IntentKind, QueryIntent, TimeRange
from app.semantic.model import DatasetDef, MetricDef

_COMPARISON_SUFFIX = "_comparison"
_CURRENT_CTE = "current_period"
_BASELINE_CTE = "baseline_period"

_OPERATOR_TEXT = {
    "eq": "=",
    "ne": "≠",
    "in": "属于",
    "not_in": "不属于",
    "gt": ">",
    "gte": "≥",
    "lt": "<",
    "lte": "≤",
    "between": "介于",
}


@dataclass(frozen=True, slots=True)
class Citation:
    """Everything the answer must be able to show (spec M-16)."""

    metric_name: str
    metric_business_name: str
    metric_version: int
    metric_description: str
    time_field_business_name: str
    time_start: date
    time_end: date
    filters: tuple[str, ...] = ()
    comparison_label: str = ""


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    ast: exp.Expression
    sql: str
    dataset_name: str
    physical_table: str
    metric_names: tuple[str, ...]
    dimension_names: tuple[str, ...]
    citation: Citation
    comparison_metric_names: tuple[str, ...] = ()


def _table(dataset: DatasetDef) -> exp.Table:
    parts = dataset.physical_table.split(".")
    if len(parts) == 2:
        return exp.Table(this=exp.to_identifier(parts[1]), db=exp.to_identifier(parts[0]))
    return exp.Table(this=exp.to_identifier(parts[0]))


def _assert_queryable(dataset: DatasetDef, metric: MetricDef) -> None:
    if metric.source_field and dataset.has_field(metric.source_field):
        field = dataset.field(metric.source_field)
        if not field.is_queryable:
            raise FieldNotQueryableError(
                f"{dataset.name}.{field.name}", "该字段在语义配置中标记为不可查询"
            )


def _render_filters(dataset: DatasetDef, intent: QueryIntent) -> tuple[str, ...]:
    rendered: list[str] = []
    for condition in intent.filters:
        field = dataset.field(condition.field)
        spoken = condition.spoken_values or condition.values
        rendered.append(
            f"{field.business_name or field.name} "
            f"{_OPERATOR_TEXT[condition.operator.value]} "
            f"{'、'.join(spoken)}"
        )
    return tuple(rendered)


def _build_select(
    dataset: DatasetDef,
    intent: QueryIntent,
    metrics: list[MetricDef],
    window: TimeRange,
    suffix: str = "",
) -> exp.Select:
    """One period's SELECT: dimensions, metrics, time window and filters."""
    projections: list[exp.Expression] = [
        build_dimension_projection(dataset, name) for name in intent.dimensions
    ]
    for metric in metrics:
        _assert_queryable(dataset, metric)
        projection = build_metric_projection(dataset, metric)
        if suffix:
            projection = exp.alias_(projection.this, f"{metric.name}{suffix}")
        projections.append(projection)

    predicates = [build_time_predicate(dataset, metrics[0].time_field, window)]
    predicates.extend(build_filter_predicate(dataset, item) for item in intent.filters)

    select = exp.select(*projections).from_(_table(dataset))
    where = combine_predicates(predicates)
    if where is not None:
        select = select.where(where)
    if intent.dimensions:
        select = select.group_by(
            *[exp.column(dataset.field(name).physical_column) for name in intent.dimensions]
        )
    return select


def _apply_sort(intent: QueryIntent, select: exp.Select) -> exp.Select:
    if intent.sort is None:
        return select
    ordered = select.order_by(
        exp.Ordered(this=exp.column(intent.sort.by), desc=intent.sort.descending)
    )
    if intent.sort.limit is not None:
        ordered = ordered.limit(intent.sort.limit)
    return ordered


def _build_comparison(
    dataset: DatasetDef, intent: QueryIntent, metrics: list[MetricDef]
) -> tuple[exp.Expression, tuple[str, ...], TimeRange]:
    baseline_window = comparison_range(intent.time, intent.comparison)
    current = _build_select(dataset, intent, metrics, intent.time)
    baseline = _build_select(
        dataset, intent, metrics, baseline_window, suffix=_COMPARISON_SUFFIX
    )

    comparison_names = tuple(f"{item.name}{_COMPARISON_SUFFIX}" for item in metrics)
    projections: list[exp.Expression] = [
        exp.column(name, table=_CURRENT_CTE) for name in intent.dimensions
    ]
    projections.extend(exp.column(item.name, table=_CURRENT_CTE) for item in metrics)
    projections.extend(exp.column(name, table=_BASELINE_CTE) for name in comparison_names)

    outer = exp.select(*projections).from_(_CURRENT_CTE)
    if intent.dimensions:
        join_condition = combine_predicates(
            [
                exp.EQ(
                    this=exp.column(name, table=_CURRENT_CTE),
                    expression=exp.column(name, table=_BASELINE_CTE),
                )
                for name in intent.dimensions
            ]
        )
        outer = outer.join(_BASELINE_CTE, on=join_condition, join_type="full outer")
    else:
        # Scalar comparison: both sides yield exactly one row.
        outer = outer.join(_BASELINE_CTE, join_type="cross")

    tree = outer.with_(_CURRENT_CTE, as_=current).with_(_BASELINE_CTE, as_=baseline)
    return tree, comparison_names, baseline_window


def compile_intent(dataset: DatasetDef, intent: QueryIntent) -> CompiledQuery:
    if intent.kind == IntentKind.UNSUPPORTED:
        raise CompileError(intent.dataset, "意图不受支持，不应进入编译阶段")
    if not dataset.is_published:
        raise CompileError(dataset.name, "数据集未通过语义体检发布，不允许用于问答")
    if intent.time is None:
        raise CompileError(dataset.name, "缺少时间范围，无法确定指标口径区间")
    if not intent.metrics:
        raise CompileError(dataset.name, "缺少指标，无法编译查询")

    metrics = [dataset.metric(name) for name in intent.metrics]
    primary = metrics[0]
    comparison_names: tuple[str, ...] = ()

    if intent.comparison == ComparisonKind.NONE:
        tree: exp.Expression = _apply_sort(
            intent, _build_select(dataset, intent, metrics, intent.time)
        )
    else:
        tree, comparison_names, _ = _build_comparison(dataset, intent, metrics)
        tree = _apply_sort(intent, tree)

    citation = Citation(
        metric_name=primary.name,
        metric_business_name=primary.business_name,
        metric_version=primary.version,
        metric_description=primary.description,
        time_field_business_name=(
            dataset.field(primary.time_field).business_name or primary.time_field
        ),
        time_start=intent.time.start,
        time_end=intent.time.end,
        filters=_render_filters(dataset, intent),
        comparison_label=comparison_label(intent.comparison),
    )

    return CompiledQuery(
        ast=tree,
        sql=tree.sql(dialect="postgres", pretty=True),
        dataset_name=dataset.name,
        physical_table=dataset.physical_table,
        metric_names=tuple(item.name for item in metrics),
        dimension_names=tuple(intent.dimensions),
        citation=citation,
        comparison_metric_names=comparison_names,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/compiler/test_query_compiler.py -v`
Expected: PASS（14 项）

- [ ] **Step 5: 运行全部测试**

Run: `cd backend && python -m pytest -v`
Expected: PASS（计划 01 的 25 项 + 本计划 56 项）

- [ ] **Step 6: 提交**

```bash
git add backend/app/compiler/query.py backend/tests/compiler/test_query_compiler.py
git commit -F - <<'EOF'
实现意图到 SQL 的确定性编译器

链路要求同一份意图必然编译出同一条 SQL，且口径、时间与过滤条件要能直接用于答案引证。因此把编译器实现为不访问数据库、不调用模型、不读时钟的纯函数，引证信息在编译期一并产出而非事后反解析 SQL。

- 时间窗口取自指标声明的时间口径字段，而非意图指定，避免同一指标出现两个数字
- 对比查询编译为当期与对比期双 CTE 外连接，Trace 中两侧可分别检视
- 排行意图应用排序与限制，维度查询生成对应 GROUP BY
- 未发布数据集与不受支持意图在入口处拒绝编译
- 引证携带指标版本、时间口径业务名与业务化过滤描述
- 验证：pytest 全量通过，其中编译器主体 14 项
EOF
```

---

## 自查

**Spec 覆盖**（对应设计文档 2、3.3、4.4、4.5、5.3）：

| Spec 条目 | 承载任务 |
|---|---|
| 3.3 意图 Schema 核心契约 | Task 1 |
| 2 LLM 不生成 SQL / 编译器确定性 | Task 5（纯函数 + 可重复性测试） |
| 4.4 四类指标 + 版本 + 比率禁止求和 | Task 3 |
| 4.5 时间计算内置模板 | Task 2 |
| M-19 结构化多轮槽位合并 | Task 1（`merge_followup`） |
| M-16 引证（口径/时间/过滤） | Task 5（`Citation`） |
| 4.2 允许聚合硬约束 | Task 3（`assert_aggregation_allowed`） |
| 5.3 编译期错误面向管理员 | Task 2（`CompileError` 携带 target） |
| 5.5 除零防护 | Task 3（`NULLIF` 包裹） |

**类型一致性**：`QueryIntent` 字段名在 Task 1 定义，Task 5 逐一引用一致；`CompiledQuery.ast` 供计划 03 的安全改写消费；`Citation` 字段供计划 04 的答案组装消费；`build_metric_projection` 返回 `exp.Alias`，Task 5 通过 `.this` 取内层表达式重命名，与 Task 3 的返回类型吻合。

**跨计划接口**：计划 03 消费 `CompiledQuery`（在其 AST 上注入 RLS 与做白名单终检）；计划 04 消费 `QueryIntent`、`Citation` 与 `compile_intent`。

## 交付物

完成本计划后：给定一份意图与已发布的语义模型，可确定性地编译出可执行的 PostgreSQL SQL，并同时得到答案所需的引证信息。**此时 SQL 还未经权限改写、也未执行**——那是计划 03。

