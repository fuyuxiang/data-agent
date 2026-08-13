"""Tests for build_plan() and compile_plan() (S3 Task 2)."""

from datetime import date

import pytest

from app.planning.canonical import (
    InconsistentTimeBasisError,
    Measure,
    TimeBasisKind,
)
from app.planning.build import build_plan, compile_plan, compile_intent_v2


pytestmark = pytest.mark.no_db


# ---- Test fixtures --------------------------------------------------------


def _make_dataset():
    """Build a minimal dataset for testing."""
    from app.semantic.model import DatasetDef, FieldDef, MetricDef

    metrics = (
        MetricDef(
            name="sales_amount",
            business_name="销售额",
            description="订单金额",
            kind="atomic",
            time_field="order_date",
            version=1,
            aggregation_behavior="additive",
            source_field="amount",
            aggregation="sum",
        ),
        MetricDef(
            name="refund_amount",
            business_name="退款额",
            description="退款金额",
            kind="atomic",
            time_field="refund_date",  # Different time field!
            version=1,
            aggregation_behavior="additive",
            source_field="amount",
            aggregation="sum",
        ),
    )

    fields = (
        FieldDef(
            name="region",
            business_name="地区",
            physical_column="region",
            semantic_type="category",
            is_groupable=True,
            is_filterable=True,
            is_queryable=True,
        ),
        FieldDef(
            name="order_date",
            business_name="订单日期",
            physical_column="order_date",
            semantic_type="date",
            is_groupable=True,
            is_filterable=True,
        ),
        FieldDef(
            name="amount",
            business_name="金额",
            physical_column="amount",
            semantic_type="measure",
            allowed_aggregations=("sum",),
        ),
    )

    return DatasetDef(
        name="orders",
        business_name="订单",
        grain="day",
        applicable_scenario="订单分析",
        forbidden_scenario="",
        physical_table="sample.orders",
        is_published=True,
        metrics=metrics,
        fields=fields,
    )


def _make_intent(*, metrics=("sales_amount",), comparison=None, sort=None, assumptions=None):
    """Build a minimal QueryIntent."""
    from app.intent.schema import (
        ComparisonKind,
        FilterCondition,
        IntentKind,
        QueryIntent,
        TimeGrain,
        TimeRange,
        FieldConfidence,
    )

    return QueryIntent(
        kind=IntentKind.AGGREGATE,
        dataset="orders",
        metrics=list(metrics),
        dimensions=("region",),
        time=TimeRange(
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            grain=TimeGrain.MONTH,
            expression="本月",
        ),
        filters=[
            FilterCondition(
                field="region",
                operator="in",
                values=["east"],
                spoken_values=["华东"],
            ),
        ],
        comparison=comparison or ComparisonKind.NONE,
        sort=sort,
        confidence=FieldConfidence(overall=0.9),
        assumptions=assumptions or [],
        raw_question="本月销售额",
    )


# ---- build_plan tests ------------------------------------------------------


class TestBuildPlan:
    """Test build_plan() function."""

    def test_build_plan_constructs_canonical(self):
        """build_plan returns a valid CanonicalQueryPlan."""
        dataset = _make_dataset()
        intent = _make_intent()

        plan = build_plan(dataset, intent)

        assert plan.dataset == "orders"
        assert plan.plan_version == "1.0.0"
        assert len(plan.measures) == 1
        assert plan.measures[0].metric_name == "sales_amount"

    def test_build_plan_includes_time_basis(self):
        """Measure includes time_basis from the metric."""
        dataset = _make_dataset()
        intent = _make_intent()

        plan = build_plan(dataset, intent)

        assert plan.measures[0].time_basis == "order_date"

    def test_build_plan_collects_lineage(self):
        """Plan collects required_field_lineage from metric, dim, filter, time."""
        dataset = _make_dataset()
        intent = _make_intent()

        plan = build_plan(dataset, intent)

        # Should include: amount, order_date, region
        lineage = plan.required_field_lineage
        assert "sample.orders.amount" in lineage
        assert "sample.orders.order_date" in lineage
        assert "sample.orders.region" in lineage

    def test_build_plan_collects_typed_filters(self):
        """Plan includes resolved typed filters."""
        dataset = _make_dataset()
        intent = _make_intent()

        plan = build_plan(dataset, intent)

        assert len(plan.typed_filters) == 1
        assert plan.typed_filters[0].field == "region"
        assert plan.typed_filters[0].values == ("east",)

    def test_build_plan_preserves_time_expression(self):
        """Plan preserves original TimeExpression."""
        dataset = _make_dataset()
        intent = _make_intent()

        plan = build_plan(dataset, intent)

        assert plan.resolved_time_range is not None
        assert plan.resolved_time_range.expression.expression == "本月"
        assert plan.resolved_time_range.start == date(2026, 1, 1)
        assert plan.resolved_time_range.end == date(2026, 1, 31)

    def test_build_plan_carries_assumptions(self):
        """Plan carries through intent.assumptions."""
        dataset = _make_dataset()
        intent = _make_intent(assumptions=["「最近」按本月理解"])

        plan = build_plan(dataset, intent)

        assert "「最近」按本月理解" in plan.assumptions


class TestMultiMetricTimeBasis:
    """Test multi-metric time basis validation."""

    def test_same_time_basis_passes(self):
        """Multiple metrics with same time basis pass."""
        dataset = _make_dataset()
        intent = _make_intent(metrics=("sales_amount",))
        # Only one metric, should pass

        plan = build_plan(dataset, intent)

        assert len(plan.measures) == 1

    def test_inconsistent_time_basis_raises(self):
        """Multiple metrics with different time bases raise error."""
        dataset = _make_dataset()
        intent = _make_intent(metrics=("sales_amount", "refund_amount"))

        with pytest.raises(InconsistentTimeBasisError) as exc_info:
            build_plan(dataset, intent)

        assert "order_date" in str(exc_info.value)
        assert "refund_date" in str(exc_info.value)
        assert "sales_amount" in str(exc_info.value)
        assert "refund_amount" in str(exc_info.value)

    def test_error_carries_metric_names(self):
        """Error includes conflicting metric names."""
        dataset = _make_dataset()
        intent = _make_intent(metrics=("sales_amount", "refund_amount"))

        with pytest.raises(InconsistentTimeBasisError) as exc_info:
            build_plan(dataset, intent)

        assert exc_info.value.metric_a == "sales_amount"
        assert exc_info.value.metric_b == "refund_amount"


class TestBuildPlanEdgeCases:
    """Test edge cases of build_plan."""

    def test_no_dimensions(self):
        """Plan can have no dimensions."""
        from app.intent.schema import QueryIntent, TimeRange, TimeGrain, FieldConfidence, FilterCondition, IntentKind, ComparisonKind

        dataset = _make_dataset()
        intent = _make_intent()
        # Build a new intent with no dimensions
        intent = QueryIntent(
            kind=IntentKind.AGGREGATE,
            dataset="orders",
            metrics=list(intent.metrics),
            dimensions=(),
            time=TimeRange(
                start=date(2026, 1, 1),
                end=date(2026, 1, 31),
                grain=TimeGrain.MONTH,
                expression="本月",
            ),
            filters=list(intent.filters),
            comparison=ComparisonKind.NONE,
            confidence=FieldConfidence(overall=0.9),
            assumptions=[],
            raw_question="",
        )

        plan = build_plan(dataset, intent)

        assert plan.group_by == ()

    def test_with_comparison(self):
        """Plan preserves comparison setting."""
        from app.intent.schema import ComparisonKind

        dataset = _make_dataset()
        intent = _make_intent(comparison=ComparisonKind.MOM)

        plan = build_plan(dataset, intent)

        assert plan.comparison == "mom"

    def test_with_sort(self):
        """Plan preserves sort setting."""
        from app.intent.schema import SortSpec

        dataset = _make_dataset()
        intent = _make_intent(sort=SortSpec(
            by="sales_amount", descending=True, limit=10
        ))

        plan = build_plan(dataset, intent)

        assert plan.sort is not None
        assert plan.sort["by"] == "sales_amount"
        assert plan.sort["limit"] == 10


class TestCompilePlan:
    """Test compile_plan() function."""

    def test_compile_plan_emits_sql(self):
        """compile_plan returns a CompiledQuery with a SQL string."""
        dataset = _make_dataset()
        intent = _make_intent()

        plan = build_plan(dataset, intent)
        compiled = compile_plan(plan, dataset)

        assert compiled.sql is not None
        assert "SELECT" in compiled.sql.upper()
        assert "FROM" in compiled.sql.upper()

    def test_compile_plan_emits_stable_sql(self):
        """Same plan produces same SQL (deterministic)."""
        dataset = _make_dataset()
        intent = _make_intent()

        plan1 = build_plan(dataset, intent)
        plan2 = build_plan(dataset, intent)

        compiled1 = compile_plan(plan1, dataset)
        compiled2 = compile_plan(plan2, dataset)

        assert compiled1.sql == compiled2.sql

    def test_compile_plan_includes_metrics(self):
        """Compiled SQL includes the metric names."""
        dataset = _make_dataset()
        intent = _make_intent()

        plan = build_plan(dataset, intent)
        compiled = compile_plan(plan, dataset)

        assert "sales_amount" in compiled.metric_names

    def test_compile_plan_includes_sql_compact(self):
        """Compiled SQL also has compact form."""
        dataset = _make_dataset()
        intent = _make_intent()

        plan = build_plan(dataset, intent)
        compiled = compile_plan(plan, dataset)

        assert compiled.sql_compact is not None
        # Compact has no newlines
        assert "\n" not in compiled.sql_compact


class TestCompileIntentV2BackwardCompat:
    """Test backward-compatible wrapper."""

    def test_v2_matches_original_compile_intent(self):
        """compile_intent_v2 returns same SQL as compile_intent."""
        from app.compiler.query import compile_intent

        dataset = _make_dataset()
        intent = _make_intent()

        original = compile_intent(dataset, intent)
        v2 = compile_intent_v2(dataset, intent)

        # Both should produce equivalent SQL
        assert original.sql == v2.sql
        assert original.metric_names == v2.metric_names
        assert original.dimension_names == v2.dimension_names
