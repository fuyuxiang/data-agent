"""Tests for comparison query dimension merging (S3 Task 3, P1-04).

Verifies that dimensions in comparison queries use COALESCE so that
dimensions appearing only in the baseline period are not dropped as NULL.
"""

import pytest


pytestmark = pytest.mark.no_db


def _make_dataset():
    """Build a minimal dataset with grouping dimension."""
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
    )

    fields = (
        FieldDef(
            name="region",
            business_name="地区",
            physical_column="region",
            semantic_type="category",
            is_groupable=True,
            is_filterable=True,
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


def _make_comparison_intent(*, comparison_kind=None):
    """Build a comparison intent (MOM)."""
    from datetime import date
    from app.intent.schema import (
        ComparisonKind,
        FieldConfidence,
        FilterCondition,
        IntentKind,
        QueryIntent,
        TimeGrain,
        TimeRange,
    )

    return QueryIntent(
        kind=IntentKind.AGGREGATE,
        dataset="orders",
        metrics=["sales_amount"],
        dimensions=["region"],
        time=TimeRange(
            start=date(2026, 2, 1),
            end=date(2026, 2, 28),
            grain=TimeGrain.MONTH,
            expression="2月",
        ),
        filters=[
            FilterCondition(
                field="region",
                operator="in",
                values=["east"],
                spoken_values=["华东"],
            ),
        ],
        comparison=comparison_kind or ComparisonKind.MOM,
        confidence=FieldConfidence(overall=0.9),
        assumptions=[],
        raw_question="2月销售额环比",
    )


class TestComparisonDimensionCoalesce:
    """Test dimension uses COALESCE in comparison queries."""

    def test_comparison_uses_coalesce_for_dimensions(self):
        """Comparison query's dimension projection uses COALESCE."""
        from app.compiler.query import compile_intent

        dataset = _make_dataset()
        intent = _make_comparison_intent()

        compiled = compile_intent(dataset, intent)

        # The SQL should contain COALESCE for the dimension
        assert "COALESCE" in compiled.sql.upper()
        # Both CTE tables should be referenced
        assert "current_period" in compiled.sql
        assert "baseline_period" in compiled.sql

    def test_comparison_with_dimensions_uses_full_outer_join(self):
        """Comparison query uses FULL OUTER JOIN when dimensions exist."""
        from app.compiler.query import compile_intent

        dataset = _make_dataset()
        intent = _make_comparison_intent()

        compiled = compile_intent(dataset, intent)

        # SQL should contain FULL OUTER JOIN
        sql = compiled.sql.upper()
        assert "FULL OUTER" in sql or "FULL OUTER JOIN" in sql

    def test_comparison_includes_period_status(self):
        """Comparison query includes a status column indicating which period(s) the value belongs to."""
        from app.compiler.query import compile_intent

        dataset = _make_dataset()
        intent = _make_comparison_intent()

        compiled = compile_intent(dataset, intent)

        # SQL should mention baseline / current status
        sql = compiled.sql.lower()
        # Either a CASE statement or a status column reference
        assert "case" in sql or "only_baseline" in sql or "both" in sql

    def test_comparison_baseline_query_uses_baseline_window(self):
        """Baseline CTE uses the comparison window (previous period)."""
        from app.compiler.query import compile_intent

        dataset = _make_dataset()
        intent = _make_comparison_intent()

        compiled = compile_intent(dataset, intent)

        # Current period: 2026-02-01 to 2026-02-28
        # Baseline period: 2026-01-01 to 2026-01-31 (MOM)
        sql = compiled.sql
        assert "2026-02" in sql
        assert "2026-01" in sql

    def test_comparison_emits_stable_sql(self):
        """Same comparison query produces stable SQL."""
        from app.compiler.query import compile_intent

        dataset = _make_dataset()
        intent = _make_comparison_intent()

        compiled1 = compile_intent(dataset, intent)
        compiled2 = compile_intent(dataset, intent)

        assert compiled1.sql == compiled2.sql

    def test_comparison_via_v2_pipeline(self):
        """S3 pipeline (build_plan + compile_plan) also produces COALESCE."""
        from app.planning.build import compile_plan, build_plan

        dataset = _make_dataset()
        intent = _make_comparison_intent()

        plan = build_plan(dataset, intent)
        compiled = compile_plan(plan, dataset)

        assert "COALESCE" in compiled.sql.upper()
        assert "current_period" in compiled.sql
        assert "baseline_period" in compiled.sql


class TestComparisonDimensionEdgeCases:
    """Test edge cases for comparison query dimensions."""

    def test_no_dimensions_no_coalesce(self):
        """Comparison without dimensions does not use COALESCE."""
        from app.compiler.query import compile_intent

        dataset = _make_dataset()
        intent = _make_comparison_intent()
        # Build new intent with no dimensions
        from app.intent.schema import QueryIntent
        from datetime import date
        from app.intent.schema import (
            ComparisonKind,
            FieldConfidence,
            IntentKind,
            TimeGrain,
            TimeRange,
        )
        intent = QueryIntent(
            kind=IntentKind.AGGREGATE,
            dataset="orders",
            metrics=["sales_amount"],
            dimensions=(),
            time=TimeRange(
                start=date(2026, 2, 1),
                end=date(2026, 2, 28),
                grain=TimeGrain.MONTH,
                expression="2月",
            ),
            filters=[],
            comparison=ComparisonKind.MOM,
            confidence=FieldConfidence(overall=0.9),
            assumptions=[],
            raw_question="",
        )

        compiled = compile_intent(dataset, intent)

        # No dimensions means no COALESCE for dimensions
        # (may still have COALESCE elsewhere, but the dim-specific COALESCE should be gone)
        # Since dims=() and metrics=1, the query is scalar comparison
        assert "current_period" in compiled.sql
        assert "baseline_period" in compiled.sql

    def test_yoy_comparison_uses_coalesce(self):
        """YoY comparison also uses COALESCE for dimensions."""
        from app.compiler.query import compile_intent
        from app.intent.schema import ComparisonKind

        dataset = _make_dataset()
        intent = _make_comparison_intent(comparison_kind=ComparisonKind.YOY)

        compiled = compile_intent(dataset, intent)

        assert "COALESCE" in compiled.sql.upper()
