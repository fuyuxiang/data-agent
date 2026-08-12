from enum import Enum


class SemanticType(str, Enum):
    AMOUNT = "amount"
    QUANTITY = "quantity"
    RATIO = "ratio"
    DATE = "date"
    ID = "id"
    ENUM = "enum"
    TEXT = "text"


class Aggregation(str, Enum):
    SUM = "sum"
    COUNT = "count"
    DISTINCT_COUNT = "distinct_count"
    AVG = "avg"
    MAX = "max"
    MIN = "min"
    NONE = "none"


class MetricKind(str, Enum):
    ATOMIC = "atomic"
    DERIVED = "derived"
    COMPOSITE = "composite"
    RATIO = "ratio"


class AggregationBehavior(str, Enum):
    """How a metric behaves when rolled up across rows."""

    ADDITIVE = "additive"
    RECALCULATE = "recalculate"
    LAST_VALUE = "last_value"


class Sensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"