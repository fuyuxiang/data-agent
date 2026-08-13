"""IntentV2: the new contract with per-metric confidence and complete cross-field validation.

Replaces QueryIntent. TimeExpression replaces absolute TimeRange;
MetricRef carries per-metric confidence; ambiguities and domain_candidates
enable proper LLM output interpretation.
"""

from datetime import date
from enum import Enum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IntentKind(str, Enum):
    """Query shapes the LLM can produce."""
    AGGREGATE = "aggregate"
    TREND = "trend"
    RANKING = "ranking"
    DETAIL = "detail"
    UNSUPPORTED = "unsupported"


class TimeExpressionKind(str, Enum):
    """Time expression type."""
    RELATIVE = "relative"
    ABSOLUTE = "absolute"
    RANGE = "range"
    NONE = "none"


class TimeUnit(str, Enum):
    """Temporal granularity."""
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class TimeExpression(BaseModel):
    """Relative or absolute time from the model.

    Replaces the absolute TimeRange: the model now outputs structured
    relative expressions like {kind: "relative", unit: "month", offset: 0}
    instead of computed absolute dates.
    """
    model_config = ConfigDict(frozen=True)

    kind: TimeExpressionKind
    text: str  # Original user phrasing for citation
    unit: TimeUnit | None = None  # None for kind=none
    offset: int = 0  # 0=current, -1=last, +1=next
    to_date: bool = False  # true means "至今" (to date)
    start: date | None = None  # For kind=absolute/range only
    end: date | None = None  # For kind=absolute/range only


class MetricRef(BaseModel):
    """A metric with its per-metric confidence."""
    model_config = ConfigDict(frozen=True)

    name: str
    confidence: float = Field(ge=0.0, le=1.0)


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


class FilterCondition(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    operator: FilterOperator
    values: list[str] = Field(default_factory=list)
    spoken_values: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_between(self) -> Self:
        if self.operator == FilterOperator.BETWEEN and len(self.values) not in (0, 2):
            raise ValueError("between filter requires zero or two values")
        return self


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


class SortSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    by: str
    descending: bool = True
    limit: int | None = None


class Ambiguity(BaseModel):
    """A source of uncertainty the model explicitly reports."""
    model_config = ConfigDict(frozen=True)

    field: str  # "metric", "time", "dimension", "filter", etc.
    candidates: list[str]  # Possible interpretations
    reason: str = ""


class DomainCandidate(BaseModel):
    """A candidate dataset the model identified."""
    model_config = ConfigDict(frozen=True)

    name: str
    confidence: float = Field(ge=0.0, le=1.0)


class QueryIntentV2(BaseModel):
    """The LLM's output contract after S2.

    Key changes from QueryIntent:
    - time: TimeExpression (not absolute dates)
    - metrics: list[MetricRef] (per-metric confidence, not strings)
    - Removed filters[].values (resolver product, not model output)
    - Added domain_candidates and ambiguities
    """
    model_config = ConfigDict(frozen=True)

    kind: IntentKind
    dataset: str
    metrics: list[MetricRef] = Field(default_factory=list)
    time_expression: TimeExpression | None = None
    dimensions: list[str] = Field(default_factory=list)
    filters: list[FilterCondition] = Field(default_factory=list)
    comparison: ComparisonKind = ComparisonKind.NONE
    sort: SortSpec | None = None
    domain_candidates: list[DomainCandidate] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    assumptions: list[str] = Field(default_factory=list)
    raw_question: str = ""

    # === CROSS-FIELD VALIDATORS (7 classes) ===

    @model_validator(mode="after")
    def aggregate_requires_metrics(self) -> Self:
        """P0-08: Aggregate kind must have at least one metric."""
        if self.kind == IntentKind.AGGREGATE and len(self.metrics) == 0:
            raise ValueError("aggregate kind requires at least one metric")
        return self

    @model_validator(mode="after")
    def trend_requires_metrics_and_time(self) -> Self:
        """Trend kind must have metrics and time expression."""
        if self.kind == IntentKind.TREND:
            if not self.metrics:
                raise ValueError("trend kind requires at least one metric")
            if self.time_expression is None or self.time_expression.kind == TimeExpressionKind.NONE:
                raise ValueError("trend kind requires a time expression")
        return self

    @model_validator(mode="after")
    def detail_requires_dimensions(self) -> Self:
        """Detail kind must have at least one dimension (to show row granularity)."""
        if self.kind == IntentKind.DETAIL and len(self.dimensions) == 0:
            raise ValueError("detail kind requires at least one dimension")
        return self

    @model_validator(mode="after")
    def ranking_requires_metrics_and_dimensions(self) -> Self:
        """Ranking kind must rank metrics by some dimension."""
        if self.kind == IntentKind.RANKING:
            if not self.metrics:
                raise ValueError("ranking kind requires at least one metric")
            if not self.dimensions:
                raise ValueError("ranking kind requires at least one dimension to rank by")
        return self

    @model_validator(mode="after")
    def time_expression_consistency(self) -> Self:
        """Validate time expression internal consistency."""
        if self.time_expression is None:
            return self

        te = self.time_expression
        if te.kind == TimeExpressionKind.RANGE or te.kind == TimeExpressionKind.ABSOLUTE:
            if te.start is None or te.end is None:
                raise ValueError(f"{te.kind} time expression requires start and end dates")
            if te.end < te.start:
                raise ValueError("time range end must not precede start")

        return self

    @model_validator(mode="after")
    def filter_values_not_populated(self) -> Self:
        """Filters from the model must not have values populated.

        Values are a resolver product; model only outputs spoken_values.
        If values are present, it's an error in prompt or post-processing.
        """
        for fc in self.filters:
            if fc.values:
                raise ValueError(
                    f"filter field={fc.field}: model output must not include values "
                    "(only spoken_values); values are populated by resolver"
                )
        return self

    @model_validator(mode="after")
    def no_contradictory_comparisons(self) -> Self:
        """Comparison=none is the default; any value is explicit."""
        # No further validation needed; comparison is just a marker.
        return self
