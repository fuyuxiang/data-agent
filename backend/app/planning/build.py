"""Build Canonical Plans from QueryIntents (S3 Task 2).

build_plan() is the pure semantic compile step: it takes an intent + dataset
and produces a CanonicalQueryPlan with no SQL, no principal, no permissions.

compile_plan() is the dialect compile step: it takes a CanonicalPlan and
produces a CompiledQuery (AST + SQL). It reuses the existing _build_select /
_build_comparison helpers from query.py.

compile_intent() is preserved as a backward-compatible wrapper:
  compile_intent(dataset, intent) == compile_plan(build_plan(dataset, intent), dataset)
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from app.compiler.query import (
    CompiledQuery,
    Citation,
    _apply_sort,
    _build_comparison,
    _build_select,
    _render_filters,
)
from app.compiler.time_windows import comparison_label
from app.intent.schema import ComparisonKind, IntentKind, QueryIntent
from app.planning.canonical import (
    PLAN_VERSION,
    CanonicalQueryPlan,
    InconsistentTimeBasisError,
    Measure,
    ResolvedTimeRange,
    TimeBasisKind,
    TimeExpression,
    TypedFilter,
)
from app.semantic.model import DatasetDef


def _build_measures(intent: QueryIntent, dataset: DatasetDef) -> tuple[Measure, ...]:
    """Build Measure tuples from intent.metrics.

    Each measure includes the time basis (physical column) for that metric.
    """
    measures: list[Measure] = []
    for i, name in enumerate(intent.metrics):
        metric = dataset.metric(name)
        # Use the metric's time_field as the canonical time basis
        time_basis = metric.time_field
        alias = f"m_{i}" if len(intent.metrics) > 1 else None
        measures.append(Measure(
            metric_name=name,
            version=metric.version,
            time_basis=time_basis,
            alias=alias,
        ))
    return tuple(measures)


def _validate_measures_time_basis(measures: tuple[Measure, ...]) -> None:
    """Validate all measures have the same time basis.

    Multi-metric queries with different time bases need explicit user approval
    (separate aggregation) because silently using the first metric's time field
    filters the others incorrectly. We default to refusal + clarification.
    """
    if len(measures) < 2:
        return

    baseline = measures[0].time_basis
    for m in measures[1:]:
        if m.time_basis != baseline:
            raise InconsistentTimeBasisError(
                metric_a=measures[0].metric_name,
                basis_a=baseline,
                metric_b=m.metric_name,
                basis_b=m.time_basis,
            )


def _build_typed_filters(intent: QueryIntent) -> tuple[TypedFilter, ...]:
    """Build typed filters from intent.filters.

    Only includes filters with resolved values (TypedFilter requires values).
    Filters with only spoken_values are pending resolution.
    """
    filters: list[TypedFilter] = []
    for condition in intent.filters:
        if condition.values:
            filters.append(TypedFilter(
                field=condition.field,
                operator=condition.operator.value,
                values=tuple(condition.values),
            ))
    return tuple(filters)


def _build_required_field_lineage(
    intent: QueryIntent,
    dataset: DatasetDef,
) -> tuple[str, ...]:
    """Collect physical columns this plan will read.

    Lineage is used by the policy compiler to grant/deny access without
    walking the AST. Currently includes source columns of all metrics.
    """
    lineage: set[str] = set()

    # Metric source fields
    for metric_name in intent.metrics:
        metric = dataset.metric(metric_name)
        if metric.source_field:
            lineage.add(f"{dataset.physical_table}.{metric.source_field}")
        # Time field
        if metric.time_field:
            lineage.add(f"{dataset.physical_table}.{metric.time_field}")

    # Dimension fields
    for dim in intent.dimensions:
        if dataset.has_field(dim):
            field = dataset.field(dim)
            lineage.add(f"{dataset.physical_table}.{field.physical_column}")

    # Filter fields
    for condition in intent.filters:
        if dataset.has_field(condition.field):
            field = dataset.field(condition.field)
            lineage.add(f"{dataset.physical_table}.{field.physical_column}")

    return tuple(sorted(lineage))


def _build_resolved_time_range(
    intent: QueryIntent,
) -> Optional[ResolvedTimeRange]:
    """Convert intent.time to ResolvedTimeRange, preserving original expression.

    If intent.time is None, returns None (no time filter).
    """
    if intent.time is None:
        return None

    # The intent.time already has resolved dates; we wrap it in a
    # ResolvedTimeRange with a reconstructed TimeExpression so the
    # canonical plan can be re-resolved by Trusted Assets.
    raw = intent.time.expression or ""

    # Distinguish relative vs absolute by whether expression contains a date
    has_date_pattern = any(c.isdigit() for c in raw)
    if has_date_pattern and raw:
        # Absolute or explicit range
        kind = "absolute"
        expression = TimeExpression(
            kind="absolute",
            expression=raw,
            start_date=intent.time.start,
            end_date=intent.time.end,
        )
    elif raw:
        # Relative time (e.g., "本月", "上季度")
        expression = TimeExpression(
            kind="relative",
            expression=raw,
        )
    else:
        # No expression, just a date range
        expression = TimeExpression(
            kind="absolute",
            expression="",
            start_date=intent.time.start,
            end_date=intent.time.end,
        )

    return ResolvedTimeRange(
        expression=expression,
        start=intent.time.start,
        end=intent.time.end,
        grain=TimeBasisKind(intent.time.grain.value),
    )


def build_plan(
    dataset: DatasetDef,
    intent: QueryIntent,
    *,
    semantic_revision_id: int = 1,
    domain: str = "default",
) -> CanonicalQueryPlan:
    """Build a Canonical Plan from a QueryIntent (pure semantic, no IO).

    This is the S3 Task 2 half: it produces a stable, cacheable plan without
    emitting SQL or consulting any policy. The dialect compile happens later
    in compile_plan().

    Raises:
        InconsistentTimeBasisError: when multiple metrics use different time
            fields. The orchestrator should treat this as a clarification event.
    """
    if intent.kind == IntentKind.UNSUPPORTED:
        raise ValueError(intent.dataset, "意图不受支持，不应进入编译阶段")

    # 1. Build measures with time basis
    measures = _build_measures(intent, dataset)

    # 2. Validate time basis consistency across measures
    _validate_measures_time_basis(measures)

    # 3. Build typed filters
    typed_filters = _build_typed_filters(intent)

    # 4. Build resolved time range (preserving TimeExpression)
    resolved_time_range = _build_resolved_time_range(intent)

    # 5. Collect required field lineage
    required_lineage = _build_required_field_lineage(intent, dataset)

    # 6. Carry through comparison, sort, pagination
    comparison = intent.comparison.value if intent.comparison != ComparisonKind.NONE else None
    sort = None
    if intent.sort:
        sort = {
            "by": intent.sort.by,
            "descending": intent.sort.descending,
            "limit": intent.sort.limit,
        }
    pagination = (intent.sort.limit if intent.sort else None)

    return CanonicalQueryPlan(
        plan_version=PLAN_VERSION,
        semantic_revision_id=semantic_revision_id,
        domain=domain,
        dataset=dataset.name,
        measures=measures,
        group_by=tuple(sorted(intent.dimensions)),
        typed_filters=typed_filters,
        resolved_time_range=resolved_time_range,
        comparison=comparison,
        sort=sort,
        pagination=pagination,
        required_field_lineage=required_lineage,
        assumptions=tuple(intent.assumptions),
        clarifications=(),
    )


def compile_plan(
    plan: CanonicalQueryPlan,
    dataset: DatasetDef,
) -> CompiledQuery:
    """Compile a Canonical Plan to SQL AST (dialect compile).

    This is the second half of the S3 split: it takes a CanonicalQueryPlan
    and reuses the existing build_select / build_comparison helpers to emit
    SQL. The plan is the source of truth; this function is pure (no IO).
    """
    if not dataset.is_published:
        raise ValueError(dataset.name, "数据集未通过语义体检发布，不允许用于问答")

    if plan.resolved_time_range is None:
        raise ValueError(dataset.name, "Canonical Plan 缺少时间范围，无法编译")

    # Reconstruct a QueryIntent-like view from the plan
    # This is the bridge: the plan and the intent both describe the same query,
    # but the plan is stable and the intent is per-call.
    metrics = [dataset.metric(m.metric_name) for m in plan.measures]
    primary = metrics[0]

    # Build a synthetic intent-shaped view for the existing helpers
    class _View:
        pass

    view = _View()
    view.kind = IntentKind.AGGREGATE  # Default; the original intent kind is
    # not preserved in the plan because it doesn't affect compilation directly.
    view.dataset = plan.dataset
    view.metrics = [m.metric_name for m in plan.measures]
    view.dimensions = list(plan.group_by)
    view.comparison = (
        ComparisonKind(plan.comparison) if plan.comparison else ComparisonKind.NONE
    )
    view.sort = _build_sort_from_plan(plan)
    view.filters = _build_filter_conditions_from_plan(plan)
    view.time = _build_time_range_from_plan(plan)
    view.assumptions = list(plan.assumptions)

    comparison_names: tuple[str, ...] = ()

    if view.comparison == ComparisonKind.NONE:
        tree = _apply_sort(
            view, _build_select(dataset, view, metrics, view.time)
        )
    else:
        tree, comparison_names, _ = _build_comparison(dataset, view, metrics)
        tree = _apply_sort(view, tree)

    citation = Citation(
        metric_name=primary.name,
        metric_business_name=primary.business_name,
        metric_version=primary.version,
        metric_description=primary.description,
        time_field_business_name=(
            dataset.field(primary.time_field).business_name or primary.time_field
        ),
        time_start=view.time.start,
        time_end=view.time.end,
        filters=_render_filters(dataset, view),
        comparison_label=comparison_label(view.comparison),
    )

    return CompiledQuery(
        ast=tree,
        sql=tree.sql(dialect="postgres", pretty=True),
        sql_compact=tree.sql(dialect="postgres"),
        dataset_name=dataset.name,
        physical_table=dataset.physical_table,
        metric_names=tuple(item.name for item in metrics),
        dimension_names=tuple(plan.group_by),
        citation=citation,
        comparison_metric_names=comparison_names,
    )


def _build_sort_from_plan(plan: CanonicalQueryPlan):
    """Reconstruct a SortSpec-like view from the plan."""
    if plan.sort is None:
        return None
    from app.intent.schema import SortSpec
    return SortSpec(
        by=plan.sort["by"],
        descending=plan.sort["descending"],
        limit=plan.sort["limit"],
    )


def _build_filter_conditions_from_plan(plan: CanonicalQueryPlan):
    """Reconstruct filter conditions from the plan."""
    from app.intent.schema import FilterCondition, FilterOperator
    return [
        FilterCondition(
            field=f.field,
            operator=FilterOperator(f.operator),
            values=list(f.values),
            spoken_values=[],
        )
        for f in plan.typed_filters
    ]


def _build_time_range_from_plan(plan: CanonicalQueryPlan):
    """Reconstruct TimeRange from ResolvedTimeRange."""
    if plan.resolved_time_range is None:
        return None
    from app.intent.schema import TimeRange, TimeGrain
    return TimeRange(
        start=plan.resolved_time_range.start,
        end=plan.resolved_time_range.end,
        grain=TimeGrain(plan.resolved_time_range.grain.value),
        expression=plan.resolved_time_range.expression.expression,
    )


def compile_intent_v2(dataset: DatasetDef, intent: QueryIntent) -> CompiledQuery:
    """Backward-compatible wrapper: build_plan + compile_plan.

    Same behaviour as the original compile_intent(), but goes through the
    canonical plan step first. Use this when migrating callers to S3.
    """
    plan = build_plan(dataset, intent)
    return compile_plan(plan, dataset)
