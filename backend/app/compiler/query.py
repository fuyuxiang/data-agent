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
    if intent.kind in {
        IntentKind.AGGREGATE,
        IntentKind.TREND,
        IntentKind.RANKING,
    } and not intent.metrics:
        raise CompileError(
            intent.dataset,
            f"{intent.kind.value} 意图必须至少包含一个指标",
        )
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
