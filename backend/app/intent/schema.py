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
    def check_between(self) -> Self:
        # Values are populated by the resolver stage (plan 04 task 3). Until
        # then, a filter may carry only spoken_values, so an empty ``values``
        # list is expected, not an error.
        if self.operator == FilterOperator.BETWEEN and len(self.values) not in (0, 2):
            raise ValueError("between filter requires zero or two values")
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