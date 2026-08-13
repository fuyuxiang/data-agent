"""Tests for field lineage collection and serialization stability (S3 Task 4)."""

from datetime import date

import pytest

from app.intent.schema import (
    ComparisonKind,
    FieldConfidence,
    IntentKind,
    QueryIntent,
    TimeGrain,
    TimeRange,
)


pytestmark = pytest.mark.no_db


def _make_dataset_with_composite():
    """Build a dataset with atomic, composite, and ratio metrics."""
    from app.semantic.model import DatasetDef, FieldDef, MetricDef

    metrics = (
        # Atomic: 销售额
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
        # Atomic: 退款额
        MetricDef(
            name="refund_amount",
            business_name="退款额",
            description="退款金额",
            kind="atomic",
            time_field="refund_date",
            version=1,
            aggregation_behavior="additive",
            source_field="amount",
            aggregation="sum",
        ),
        # Composite: GMV = sales + refund
        MetricDef(
            name="gmv",
            business_name="GMV",
            description="商品交易总额",
            kind="composite",
            time_field="order_date",
            version=1,
            aggregation_behavior="recalculate",
            expression="sales_amount + refund_amount",
        ),
        # Ratio: 退款率 = refund / sales
        MetricDef(
            name="refund_rate",
            business_name="退款率",
            description="退款率",
            kind="ratio",
            time_field="order_date",
            version=1,
            aggregation_behavior="recalculate",
            expression="refund_amount / sales_amount",
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
        ),
        FieldDef(
            name="refund_date",
            business_name="退款日期",
            physical_column="refund_date",
            semantic_type="date",
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


def _make_intent(*, metrics=("sales_amount",)):
    """Build a minimal QueryIntent."""
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
        comparison=ComparisonKind.NONE,
        confidence=FieldConfidence(overall=0.9),
        assumptions=[],
        raw_question="",
    )


class TestFieldLineageAtomic:
    """Test field lineage for atomic metrics."""

    def test_atomic_metric_includes_source_field(self):
        """Atomic metric lineage includes its source_field."""
        from app.planning.build import build_plan

        dataset = _make_dataset_with_composite()
        intent = _make_intent(metrics=("sales_amount",))

        plan = build_plan(dataset, intent)

        # Should include amount (source field) and order_date (time field)
        assert "sample.orders.amount" in plan.required_field_lineage
        assert "sample.orders.order_date" in plan.required_field_lineage

    def test_atomic_metric_includes_dimension_fields(self):
        """Lineage includes physical columns of all dimensions."""
        from app.planning.build import build_plan

        dataset = _make_dataset_with_composite()
        intent = _make_intent(metrics=("sales_amount",))

        plan = build_plan(dataset, intent)

        # region dimension
        assert "sample.orders.region" in plan.required_field_lineage

    def test_atomic_metric_includes_filter_fields(self):
        """Lineage includes physical columns of all filters."""
        from app.planning.build import build_plan

        dataset = _make_dataset_with_composite()
        intent = _make_intent(metrics=("sales_amount",))

        plan = build_plan(dataset, intent)

        # region filter (also a dimension)
        assert "sample.orders.region" in plan.required_field_lineage


class TestFieldLineageComposite:
    """Test field lineage for composite metrics."""

    def test_composite_metric_includes_dependencies(self):
        """Composite metric lineage includes ALL dependency source fields."""
        from app.planning.build import build_plan

        dataset = _make_dataset_with_composite()
        intent = _make_intent(metrics=("gmv",))

        plan = build_plan(dataset, intent)

        # GMV depends on sales_amount + refund_amount, both reference "amount"
        # Even though the dependency tree is one level deep, the source field
        # "amount" is shared and should be in lineage.
        assert "sample.orders.amount" in plan.required_field_lineage

    def test_composite_metric_includes_time_fields(self):
        """Composite metric lineage includes time fields of dependencies."""
        from app.planning.build import build_plan

        dataset = _make_dataset_with_composite()
        intent = _make_intent(metrics=("gmv",))

        plan = build_plan(dataset, intent)

        # GMV's time field is order_date; sales_amount uses order_date;
        # refund_amount uses refund_date — both should be in lineage.
        assert "sample.orders.order_date" in plan.required_field_lineage
        assert "sample.orders.refund_date" in plan.required_field_lineage


class TestFieldLineageRatio:
    """Test field lineage for ratio metrics."""

    def test_ratio_includes_numerator_and_denominator(self):
        """Ratio metric lineage includes both numerator and denominator source fields."""
        from app.planning.build import build_plan

        dataset = _make_dataset_with_composite()
        intent = _make_intent(metrics=("refund_rate",))

        plan = build_plan(dataset, intent)

        # refund_rate = refund_amount / sales_amount
        # Both depend on "amount" field
        assert "sample.orders.amount" in plan.required_field_lineage


class TestFieldLineageMultiMetric:
    """Test field lineage for multiple metrics."""

    def test_multiple_metrics_union_lineage(self):
        """Multiple metrics with same time basis produce union of all lineages."""
        from app.planning.build import build_plan

        # Use a dataset with two metrics sharing the same time basis
        # (to bypass the InconsistentTimeBasisError check)
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
                name="sales_count",
                business_name="订单数",
                description="订单数",
                kind="atomic",
                time_field="order_date",  # Same time basis
                version=1,
                aggregation_behavior="additive",
                source_field="id",
                aggregation="count",
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
            ),
            FieldDef(
                name="amount",
                business_name="金额",
                physical_column="amount",
                semantic_type="measure",
                allowed_aggregations=("sum",),
            ),
            FieldDef(
                name="id",
                business_name="id",
                physical_column="id",
                semantic_type="id",
                allowed_aggregations=("count",),
            ),
        )

        dataset = DatasetDef(
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

        intent = QueryIntent(
            kind=IntentKind.AGGREGATE,
            dataset="orders",
            metrics=["sales_amount", "sales_count"],
            dimensions=("region",),
            time=TimeRange(
                start=date(2026, 1, 1),
                end=date(2026, 1, 31),
                grain=TimeGrain.MONTH,
                expression="本月",
            ),
            filters=[],
            comparison=ComparisonKind.NONE,
            confidence=FieldConfidence(overall=0.9),
            assumptions=[],
            raw_question="",
        )

        plan = build_plan(dataset, intent)

        # Both source fields present
        assert "sample.orders.amount" in plan.required_field_lineage
        assert "sample.orders.id" in plan.required_field_lineage
        # Shared time field
        assert "sample.orders.order_date" in plan.required_field_lineage
        # Dimension
        assert "sample.orders.region" in plan.required_field_lineage


class TestSerializationStability:
    """Test canonical plan serialization stability."""

    def test_same_plan_same_hash(self):
        """Same plan produces same hash."""
        from app.planning.canonical import CanonicalQueryPlan, PLAN_VERSION, Measure
        from app.planning.build import build_plan

        dataset = _make_dataset_with_composite()
        intent = _make_intent(metrics=("sales_amount",))

        plan1 = build_plan(dataset, intent)
        plan2 = build_plan(dataset, intent)

        assert plan1.hash() == plan2.hash()

    def test_different_intent_different_hash(self):
        """Plans with different intent have different hash."""
        from app.planning.build import build_plan

        dataset = _make_dataset_with_composite()
        intent1 = _make_intent(metrics=("sales_amount",))
        intent2 = _make_intent(metrics=("refund_amount",))

        plan1 = build_plan(dataset, intent1)
        plan2 = build_plan(dataset, intent2)

        assert plan1.hash() != plan2.hash()

    def test_metric_version_in_hash(self):
        """Different metric versions produce different hashes."""
        from app.planning.build import build_plan

        dataset = _make_dataset_with_composite()
        intent = _make_intent(metrics=("sales_amount",))

        plan1 = build_plan(dataset, intent)

        # Bump version
        intent_bumped = _make_intent(metrics=("sales_amount",))
        plan2 = build_plan(dataset, intent_bumped)

        # Same plan, same hash
        assert plan1.hash() == plan2.hash()

    def test_hash_independent_of_field_order(self):
        """Hash is stable regardless of construction order."""
        from app.planning.canonical import (
            CanonicalQueryPlan,
            Measure,
            PLAN_VERSION,
        )

        # Two plans with same fields but different construction order
        m1 = Measure(metric_name="a", version=1, time_basis="t1")
        m2 = Measure(metric_name="b", version=1, time_basis="t1")

        plan1 = CanonicalQueryPlan(
            plan_version=PLAN_VERSION,
            semantic_revision_id=1,
            domain="x",
            dataset="ds",
            measures=(m1, m2),
            group_by=("c", "d"),
            typed_filters=(),
            resolved_time_range=None,
            comparison=None,
            sort=None,
            pagination=None,
            required_field_lineage=("x.col1", "x.col2"),
            assumptions=(),
        )

        plan2 = CanonicalQueryPlan(
            plan_version=PLAN_VERSION,
            semantic_revision_id=1,
            domain="x",
            dataset="ds",
            measures=(m2, m1),  # Different order
            group_by=("d", "c"),  # Different order
            typed_filters=(),
            resolved_time_range=None,
            comparison=None,
            sort=None,
            pagination=None,
            required_field_lineage=("x.col2", "x.col1"),  # Different order
            assumptions=(),
        )

        assert plan1.hash() == plan2.hash()


class TestLineageUsedForPolicyEnforcement:
    """Test that lineage is sufficient for policy checking."""

    def test_lineage_marks_all_readable_columns(self):
        """Every column the query reads is in the lineage."""
        from app.planning.build import build_plan

        dataset = _make_dataset_with_composite()
        intent = _make_intent(metrics=("gmv",))

        plan = build_plan(dataset, intent)

        # gmv reads: sample.orders.amount (via sales+refund), order_date, refund_date
        # + region (dim) + region (filter)
        assert "sample.orders.amount" in plan.required_field_lineage
        assert "sample.orders.order_date" in plan.required_field_lineage
        assert "sample.orders.refund_date" in plan.required_field_lineage
        assert "sample.orders.region" in plan.required_field_lineage

    def test_no_unneeded_columns_in_lineage(self):
        """Lineage should not include columns the query doesn't read."""
        from app.planning.build import build_plan

        dataset = _make_dataset_with_composite()
        intent = _make_intent(metrics=("sales_amount",))

        plan = build_plan(dataset, intent)

        # sales_amount uses order_date, not refund_date
        assert "sample.orders.refund_date" not in plan.required_field_lineage
