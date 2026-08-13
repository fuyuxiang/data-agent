"""Tests for unified metric/field lineage (S4 P1-05)."""

import pytest


pytestmark = pytest.mark.no_db


def _make_dataset():
    """Build a minimal dataset with atomic, composite, ratio metrics."""
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
            time_field="refund_date",
            version=1,
            aggregation_behavior="additive",
            source_field="amount",
            aggregation="sum",
        ),
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
        ),
        FieldDef(
            name="amount",
            business_name="金额",
            physical_column="amount",
            semantic_type="measure",
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


class TestMetricDag:
    """Test metric DAG construction."""

    def test_dag_includes_all_metrics(self):
        """DAG includes a node for every metric."""
        from app.semantic.lineage import metric_dag

        dataset = _make_dataset()
        dag = metric_dag(dataset)

        names = {n.name for n in dag.nodes}
        assert names == {"sales_amount", "refund_amount", "gmv", "refund_rate"}

    def test_atomic_metric_has_no_dependencies(self):
        """Atomic metric has empty dependencies."""
        from app.semantic.lineage import metric_dag

        dataset = _make_dataset()
        dag = metric_dag(dataset)

        assert dag.dependencies("sales_amount") == ()
        assert dag.dependencies("refund_amount") == ()

    def test_composite_metric_has_expression_dependencies(self):
        """Composite metric lists its expression references as dependencies."""
        from app.semantic.lineage import metric_dag

        dataset = _make_dataset()
        dag = metric_dag(dataset)

        deps = dag.dependencies("gmv")
        assert set(deps) == {"sales_amount", "refund_amount"}

    def test_ratio_metric_has_dependencies(self):
        """Ratio metric lists its numerator/denominator as dependencies."""
        from app.semantic.lineage import metric_dag

        dataset = _make_dataset()
        dag = metric_dag(dataset)

        deps = dag.dependencies("refund_rate")
        assert set(deps) == {"refund_amount", "sales_amount"}

    def test_self_dependency_filtered(self):
        """Self-reference (a → a) is filtered out."""
        from app.semantic.model import DatasetDef, FieldDef, MetricDef
        from app.semantic.lineage import metric_dag

        metrics = (
            MetricDef(
                name="weird",
                business_name="weird",
                description="self-referential",
                kind="composite",
                time_field="order_date",
                version=1,
                expression="weird + 1",
            ),
        )
        fields = (FieldDef(name="x", business_name="x", physical_column="x", semantic_type="text"),)
        dataset = DatasetDef(
            name="test", business_name="test", grain="day",
            applicable_scenario="", forbidden_scenario="",
            physical_table="t", is_published=True,
            metrics=metrics, fields=fields,
        )

        dag = metric_dag(dataset)

        # Self-reference is filtered
        assert dag.dependencies("weird") == ()


class TestAllDependencies:
    """Test transitive dependency resolution."""

    def test_transitive_dependencies(self):
        """BFS finds all transitive dependencies."""
        from app.semantic.model import DatasetDef, FieldDef, MetricDef
        from app.semantic.lineage import metric_dag

        metrics = (
            MetricDef(name="a", business_name="a", description="",
                      kind="atomic", time_field="t", source_field="a_col",
                      aggregation_behavior="additive", aggregation="sum"),
            MetricDef(name="b", business_name="b", description="",
                      kind="composite", time_field="t",
                      expression="a", aggregation_behavior="recalculate"),
            MetricDef(name="c", business_name="c", description="",
                      kind="composite", time_field="t",
                      expression="b + a", aggregation_behavior="recalculate"),
        )
        fields = (FieldDef(name="a_col", business_name="a", physical_column="a_col", semantic_type="text"),)
        dataset = DatasetDef(
            name="t", business_name="t", grain="day",
            applicable_scenario="", forbidden_scenario="",
            physical_table="t", is_published=True,
            metrics=metrics, fields=fields,
        )

        dag = metric_dag(dataset)

        deps = dag.all_dependencies("c")
        # c depends on b and a; b depends on a; transitive includes both
        assert "a" in deps
        assert "b" in deps

    def test_no_dependencies_for_atomic(self):
        """Atomic metric has no transitive dependencies."""
        from app.semantic.lineage import metric_dag

        dataset = _make_dataset()
        dag = metric_dag(dataset)

        assert dag.all_dependencies("sales_amount") == frozenset()


class TestFieldLineage:
    """Test field lineage resolution."""

    def test_atomic_metric_returns_source_field(self):
        """Atomic metric lineage includes its source_field."""
        from app.semantic.lineage import field_lineage

        dataset = _make_dataset()
        lineage = field_lineage(dataset, "sales_amount")

        assert "amount" in lineage

    def test_composite_metric_includes_dependency_fields(self):
        """Composite metric lineage includes all dependency source fields."""
        from app.semantic.lineage import field_lineage

        dataset = _make_dataset()
        # gmv = sales + refund; both share source_field "amount"
        lineage = field_lineage(dataset, "gmv")

        assert "amount" in lineage

    def test_unknown_metric_returns_empty(self):
        """Unknown metric returns empty lineage."""
        from app.semantic.lineage import field_lineage

        dataset = _make_dataset()
        lineage = field_lineage(dataset, "nonexistent")

        assert lineage == frozenset()


class TestBackwardsCompatibility:
    """Test that compiler/metrics.resolve_metric_dependencies wrapper works."""

    def test_resolve_dependencies_returns_metricdefs(self):
        """resolve_metric_dependencies returns list of MetricDef."""
        from app.semantic.lineage import resolve_metric_dependencies

        dataset = _make_dataset()
        gmv = dataset.metric("gmv")

        deps = resolve_metric_dependencies(dataset, gmv)

        # Should return sales_amount and refund_amount
        dep_names = {d.name for d in deps}
        assert dep_names == {"sales_amount", "refund_amount"}

    def test_atomic_metric_returns_empty(self):
        """Atomic metric has no dependencies."""
        from app.semantic.lineage import resolve_metric_dependencies

        dataset = _make_dataset()
        sales = dataset.metric("sales_amount")

        deps = resolve_metric_dependencies(dataset, sales)

        assert deps == []


class TestLineageUsedBySecurity:
    """Test that security/columns can use unified lineage."""

    def test_field_lineage_matches_security_expectation(self):
        """field_lineage() returns same result security/columns expects."""
        from app.semantic.lineage import field_lineage

        dataset = _make_dataset()

        # For atomic metric: only its source_field
        assert field_lineage(dataset, "sales_amount") == frozenset({"amount"})

        # For composite: union of all dependency source_fields
        gmv_lineage = field_lineage(dataset, "gmv")
        assert "amount" in gmv_lineage
