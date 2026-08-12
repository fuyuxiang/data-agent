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