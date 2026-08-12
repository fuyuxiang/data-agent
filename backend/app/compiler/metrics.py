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