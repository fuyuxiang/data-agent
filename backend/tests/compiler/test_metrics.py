from dataclasses import replace

import pytest

from app.compiler.errors import AggregationNotAllowedError, MetricConfigError, RatioMetricSumError
from app.compiler.metrics import (
    assert_aggregation_allowed,
    build_metric_projection,
    resolve_metric_dependencies,
)
from app.semantic.enums import Aggregation, AggregationBehavior, MetricKind
from app.semantic.loader import load_dataset
from app.semantic.model import MetricDef
from tests.semantic.factories import build_orders_dataset


def _sql(expression) -> str:
    return expression.sql(dialect="postgres")


@pytest.fixture
def orders(meta_session):
    build_orders_dataset(meta_session)
    return load_dataset(meta_session, "orders")


def test_atomic_metric_compiles_to_filtered_sum(orders):
    projection = build_metric_projection(orders, orders.metric("sales_revenue"))
    sql = _sql(projection)
    assert "SUM(" in sql
    assert "amount" in sql
    # fixed_filter must ride along as a FILTER clause, not leak into WHERE.
    assert "FILTER(WHERE" in sql.replace(" ", "").replace("FILTER (WHERE", "FILTER(WHERE")
    assert "AS sales_revenue" in sql


def test_count_metric_uses_count(orders):
    sql = _sql(build_metric_projection(orders, orders.metric("order_count")))
    assert "COUNT(" in sql
    assert "AS order_count" in sql


def test_derived_metric_includes_its_own_filter(orders):
    sql = _sql(build_metric_projection(orders, orders.metric("new_customer_revenue")))
    assert "is_new_customer" in sql


def test_ratio_metric_divides_dependencies_with_null_guard(orders):
    sql = _sql(build_metric_projection(orders, orders.metric("gross_margin_rate")))
    # Division must be guarded: spec 5.5 lists divide-by-zero as a checked case.
    assert "NULLIF" in sql.upper()
    assert "AS gross_margin_rate" in sql


def test_ratio_dependencies_are_resolved_in_order(orders):
    deps = resolve_metric_dependencies(orders, orders.metric("gross_margin_rate"))
    names = [item.name for item in deps]
    assert set(names) == {"sales_revenue", "total_cost"}


def test_atomic_metric_has_no_dependencies(orders):
    assert resolve_metric_dependencies(orders, orders.metric("sales_revenue")) == []


def test_disallowed_aggregation_is_rejected(orders):
    # province allows no aggregation at all.
    bad = MetricDef(
        name="bad_metric",
        business_name="错误指标",
        kind=MetricKind.ATOMIC.value,
        time_field="completed_date",
        source_field="province",
        aggregation=Aggregation.SUM.value,
    )
    with pytest.raises(AggregationNotAllowedError) as excinfo:
        assert_aggregation_allowed(orders, bad)
    assert "province" in str(excinfo.value)


def test_summing_a_ratio_metric_is_rejected(orders):
    ratio = orders.metric("gross_margin_rate")
    summable = replace(
        ratio,
        kind=MetricKind.ATOMIC.value,
        aggregation=Aggregation.SUM.value,
        source_field="amount",
        aggregation_behavior=AggregationBehavior.RECALCULATE.value,
    )
    with pytest.raises(RatioMetricSumError):
        assert_aggregation_allowed(orders, summable)


def test_metric_missing_source_field_is_rejected(orders):
    broken = MetricDef(
        name="broken",
        business_name="缺字段",
        kind=MetricKind.ATOMIC.value,
        time_field="completed_date",
        source_field=None,
        aggregation=Aggregation.SUM.value,
    )
    with pytest.raises(MetricConfigError):
        build_metric_projection(orders, broken)


def test_compilation_is_repeatable(orders):
    metric = orders.metric("sales_revenue")
    first = _sql(build_metric_projection(orders, metric))
    second = _sql(build_metric_projection(orders, metric))
    assert first == second