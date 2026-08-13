"""Tests for Canonical Query Plan (S3 Task 1)."""

import json
from datetime import date

import pytest

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


pytestmark = pytest.mark.no_db


def make_sample_plan(
    *,
    measures: tuple[Measure, ...] = (
        Measure(metric_name="销售额", version=1, time_basis="order_date"),
    ),
    group_by: tuple[str, ...] = ("region",),
    typed_filters: tuple[TypedFilter, ...] = (),
    time_range: ResolvedTimeRange | None = None,
    required_lineage: tuple[str, ...] = ("sales.amount", "orders.order_date"),
    **kwargs,
) -> CanonicalQueryPlan:
    """Build a sample canonical plan for testing."""
    return CanonicalQueryPlan(
        plan_version=PLAN_VERSION,
        semantic_revision_id=1,
        domain="sales",
        dataset="orders",
        measures=measures,
        group_by=group_by,
        typed_filters=typed_filters,
        resolved_time_range=time_range,
        comparison=None,
        sort=None,
        pagination=None,
        required_field_lineage=required_lineage,
        assumptions=kwargs.get("assumptions", ()),
        clarifications=kwargs.get("clarifications", ()),
    )


class TestCanonicalQueryPlan:
    """Test Canonical Query Plan structure."""

    def test_minimal_plan_constructs(self):
        """Minimal plan with required fields constructs."""
        plan = make_sample_plan()

        assert plan.plan_version == PLAN_VERSION
        assert plan.semantic_revision_id == 1
        assert plan.dataset == "orders"
        assert len(plan.measures) == 1
        assert plan.measures[0].metric_name == "销售额"

    def test_plan_is_frozen(self):
        """Plan is immutable (frozen dataclass)."""
        plan = make_sample_plan()

        with pytest.raises(Exception):  # FrozenInstanceError
            plan.dataset = "different"  # type: ignore

    def test_plan_is_hashable(self):
        """Plan is hashable (frozen dataclass)."""
        plan = make_sample_plan()

        # Can be used in sets / dict keys
        plan_set = {plan}
        assert plan in plan_set

    def test_plan_equality(self):
        """Plans with same fields are equal."""
        plan1 = make_sample_plan()
        plan2 = make_sample_plan()

        assert plan1 == plan2

    def test_plan_inequality(self):
        """Plans with different fields are not equal."""
        plan1 = make_sample_plan()
        plan2 = make_sample_plan(
            measures=(Measure(metric_name="退款额", version=1, time_basis="refund_date"),)
        )

        assert plan1 != plan2


class TestCanonicalSerialization:
    """Test deterministic serialisation."""

    def test_to_dict_has_stable_structure(self):
        """to_dict produces expected structure."""
        plan = make_sample_plan()
        d = plan.to_dict()

        assert d["plan_version"] == PLAN_VERSION
        assert d["semantic_revision_id"] == 1
        assert d["dataset"] == "orders"
        assert "measures" in d
        assert "group_by" in d
        assert "required_field_lineage" in d

    def test_to_dict_sorts_group_by(self):
        """Group by is sorted for stability."""
        plan = make_sample_plan(group_by=("z_dim", "a_dim", "m_dim"))
        d = plan.to_dict()

        assert d["group_by"] == ["a_dim", "m_dim", "z_dim"]

    def test_to_dict_sorts_filters(self):
        """Filters are sorted by field, operator."""
        plan = make_sample_plan(
            typed_filters=(
                TypedFilter(field="region", operator="in", values=("east", "north")),
                TypedFilter(field="status", operator="eq", values=("active",)),
            )
        )
        d = plan.to_dict()

        # First filter alphabetically by field
        assert d["typed_filters"][0]["field"] == "region"
        assert d["typed_filters"][1]["field"] == "status"

    def test_to_dict_sorts_lineage(self):
        """Field lineage is sorted."""
        plan = make_sample_plan(
            required_lineage=("z.col", "a.col", "m.col")
        )
        d = plan.to_dict()

        assert d["required_field_lineage"] == ["a.col", "m.col", "z.col"]

    def test_to_dict_is_json_serializable(self):
        """to_dict is JSON-serializable."""
        plan = make_sample_plan()
        d = plan.to_dict()

        # Should not raise
        json_str = json.dumps(d, ensure_ascii=False)
        assert isinstance(json_str, str)


class TestCanonicalHash:
    """Test deterministic hashing."""

    def test_same_plan_same_hash(self):
        """Same plan produces same hash."""
        plan1 = make_sample_plan()
        plan2 = make_sample_plan()

        assert plan1.hash() == plan2.hash()

    def test_different_plan_different_hash(self):
        """Different plan produces different hash."""
        plan1 = make_sample_plan()
        plan2 = make_sample_plan(group_by=("region", "product"))

        assert plan1.hash() != plan2.hash()

    def test_hash_is_deterministic(self):
        """Same hash regardless of dict iteration order."""
        plan = make_sample_plan()
        # Multiple calls should return same hash
        hashes = [plan.hash() for _ in range(3)]
        assert len(set(hashes)) == 1

    def test_hash_changes_with_required_lineage(self):
        """Hash changes when required_field_lineage changes."""
        plan1 = make_sample_plan(required_lineage=("sales.amount",))
        plan2 = make_sample_plan(required_lineage=("sales.amount", "extra.col"))

        assert plan1.hash() != plan2.hash()


class TestTimeExpression:
    """Test TimeExpression structure."""

    def test_relative_time_constructs(self):
        """Relative time (e.g., '本月') constructs."""
        expr = TimeExpression(
            kind="relative",
            expression="本月",
            unit="month",
            offset=0,
        )

        assert expr.kind == "relative"
        assert expr.unit == "month"
        assert expr.offset == 0

    def test_absolute_time_constructs(self):
        """Absolute time (specific dates) constructs."""
        expr = TimeExpression(
            kind="absolute",
            expression="2026-01-15",
            start_date=date(2026, 1, 15),
            end_date=date(2026, 1, 15),
        )

        assert expr.kind == "absolute"
        assert expr.start_date == date(2026, 1, 15)


class TestResolvedTimeRange:
    """Test ResolvedTimeRange structure."""

    def test_resolved_range_preserves_expression(self):
        """ResolvedTimeRange preserves original TimeExpression."""
        expr = TimeExpression(kind="relative", expression="本月", unit="month", offset=0)
        rng = ResolvedTimeRange(
            expression=expr,
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            grain=TimeBasisKind.MONTH,
        )

        assert rng.expression == expr
        assert rng.start == date(2026, 1, 1)
        assert rng.end == date(2026, 1, 31)
        assert rng.grain == TimeBasisKind.MONTH


class TestInconsistentTimeBasisError:
    """Test multi-metric time basis consistency validation."""

    def test_error_carries_metric_names(self):
        """Error includes the two conflicting metrics."""
        err = InconsistentTimeBasisError(
            metric_a="销售额",
            basis_a="order_date",
            metric_b="退款额",
            basis_b="refund_date",
        )

        assert "销售额" in str(err)
        assert "退款额" in str(err)
        assert "order_date" in str(err)
        assert "refund_date" in str(err)

    def test_error_suggests_split_query(self):
        """Error message guides user to split the query."""
        err = InconsistentTimeBasisError(
            metric_a="a", basis_a="x", metric_b="b", basis_b="y"
        )

        # Should mention "分别统计" or similar
        assert "分别" in str(err) or "separate" in str(err).lower()
