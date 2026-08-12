from datetime import date

import pytest

from app.compiler.errors import FieldNotFilterableError, FieldNotGroupableError
from app.compiler.predicates import (
    build_dimension_projection,
    build_filter_predicate,
    build_time_predicate,
    combine_predicates,
)
from app.intent.schema import FilterCondition, FilterOperator, TimeGrain, TimeRange
from app.semantic.loader import load_dataset
from tests.semantic.factories import build_orders_dataset


def _sql(expression) -> str:
    return expression.sql(dialect="postgres")


@pytest.fixture
def orders(meta_session):
    build_orders_dataset(meta_session)
    return load_dataset(meta_session, "orders")


def test_in_filter_uses_physical_values(orders):
    condition = FilterCondition(
        field="region_code",
        operator=FilterOperator.IN,
        values=["EC", "SC"],
        spoken_values=["华东", "华南"],
    )
    sql = _sql(build_filter_predicate(orders, condition))
    assert "region_code" in sql
    assert "'EC'" in sql and "'SC'" in sql
    # The spoken form must never reach SQL.
    assert "华东" not in sql


def test_eq_filter_compiles_to_equality(orders):
    condition = FilterCondition(
        field="channel", operator=FilterOperator.EQ, values=["online"], spoken_values=["线上"]
    )
    assert "=" in _sql(build_filter_predicate(orders, condition))


def test_between_filter_compiles_to_between(orders):
    condition = FilterCondition(
        field="created_date",
        operator=FilterOperator.BETWEEN,
        values=["2026-08-01", "2026-08-31"],
        spoken_values=["八月"],
    )
    assert "BETWEEN" in _sql(build_filter_predicate(orders, condition)).upper()


def test_comparison_operators_compile(orders):
    for operator, token in (
        (FilterOperator.GT, ">"),
        (FilterOperator.GTE, ">="),
        (FilterOperator.LT, "<"),
        (FilterOperator.LTE, "<="),
    ):
        condition = FilterCondition(
            field="amount", operator=operator, values=["1000"], spoken_values=["一千"]
        )
        assert token in _sql(build_filter_predicate(orders, condition))


def test_not_in_filter_compiles(orders):
    condition = FilterCondition(
        field="status", operator=FilterOperator.NOT_IN, values=["cancelled"], spoken_values=["已取消"]
    )
    sql = _sql(build_filter_predicate(orders, condition)).upper()
    assert "NOT" in sql and "IN" in sql


def test_filter_on_non_filterable_field_is_rejected(meta_session):
    from dataclasses import replace

    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    locked = tuple(
        replace(item, is_filterable=False) if item.name == "cost" else item
        for item in dataset.fields
    )
    dataset = replace(dataset, fields=locked)

    condition = FilterCondition(
        field="cost", operator=FilterOperator.GT, values=["1"], spoken_values=["一"]
    )
    with pytest.raises(FieldNotFilterableError):
        build_filter_predicate(dataset, condition)


def test_time_predicate_is_inclusive_between(orders):
    window = TimeRange(
        start=date(2026, 8, 1), end=date(2026, 8, 12), grain=TimeGrain.MONTH, expression="本月"
    )
    sql = _sql(build_time_predicate(orders, "completed_date", window)).upper()
    assert "BETWEEN" in sql
    assert "2026-08-01" in sql and "2026-08-12" in sql


def test_dimension_projection_uses_business_alias(orders):
    sql = _sql(build_dimension_projection(orders, "province"))
    assert "province" in sql


def test_non_groupable_field_cannot_be_a_dimension(orders):
    # amount is a measure, not a dimension.
    with pytest.raises(FieldNotGroupableError):
        build_dimension_projection(orders, "amount")


def test_combine_predicates_joins_with_and(orders):
    left = build_filter_predicate(
        orders,
        FilterCondition(
            field="region_code", operator=FilterOperator.IN, values=["EC"], spoken_values=["华东"]
        ),
    )
    right = build_filter_predicate(
        orders,
        FilterCondition(
            field="channel", operator=FilterOperator.EQ, values=["online"], spoken_values=["线上"]
        ),
    )
    assert "AND" in _sql(combine_predicates([left, right])).upper()


def test_combine_predicates_returns_none_when_empty():
    assert combine_predicates([]) is None
