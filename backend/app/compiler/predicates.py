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
