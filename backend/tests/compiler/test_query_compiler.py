from datetime import date

import pytest

from app.compiler.errors import CompileError, FieldNotGroupableError
from app.compiler.query import compile_intent
from app.intent.schema import (
    ComparisonKind,
    FieldConfidence,
    FilterCondition,
    FilterOperator,
    IntentKind,
    QueryIntent,
    SortSpec,
    TimeGrain,
    TimeRange,
)
from app.semantic.loader import load_dataset
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def orders(meta_session):
    build_orders_dataset(meta_session)
    return load_dataset(meta_session, "orders")


def _august() -> TimeRange:
    return TimeRange(
        start=date(2026, 8, 1), end=date(2026, 8, 31), grain=TimeGrain.MONTH, expression="本月"
    )


def _intent(**overrides) -> QueryIntent:
    payload = {
        "kind": IntentKind.AGGREGATE,
        "dataset": "orders",
        "metrics": ["sales_revenue"],
        "time": _august(),
        "confidence": FieldConfidence(overall=0.9),
        "raw_question": "本月销售额",
    }
    payload.update(overrides)
    return QueryIntent(**payload)


def test_simple_aggregate_compiles(orders):
    compiled = compile_intent(orders, _intent())

    assert "SELECT" in compiled.sql.upper()
    assert "sample.orders" in compiled.sql
    assert "sales_revenue" in compiled.sql
    assert compiled.metric_names == ("sales_revenue",)


def test_time_window_uses_metric_time_field(orders):
    # sales_revenue is measured on completed_date, not created_date.
    compiled = compile_intent(orders, _intent())
    assert "completed_date" in compiled.sql
    assert "created_date" not in compiled.sql


def test_dimension_produces_group_by(orders):
    compiled = compile_intent(orders, _intent(dimensions=["province"]))

    assert "GROUP BY" in compiled.sql.upper()
    assert compiled.dimension_names == ("province",)


def test_filter_values_are_physical(orders):
    compiled = compile_intent(
        orders,
        _intent(
            filters=[
                FilterCondition(
                    field="region_code",
                    operator=FilterOperator.IN,
                    values=["EC"],
                    spoken_values=["华东"],
                )
            ]
        ),
    )
    assert "'EC'" in compiled.sql


def test_ranking_intent_applies_order_and_limit(orders):
    compiled = compile_intent(
        orders,
        _intent(
            kind=IntentKind.RANKING,
            dimensions=["province"],
            sort=SortSpec(by="sales_revenue", descending=True, limit=3),
        ),
    )
    upper = compiled.sql.upper()
    assert "ORDER BY" in upper
    assert "DESC" in upper
    assert "LIMIT 3" in upper


def test_comparison_produces_both_periods(orders):
    compiled = compile_intent(orders, _intent(comparison=ComparisonKind.MOM))

    # Current and baseline windows must both appear.
    assert "2026-08-01" in compiled.sql
    assert "2026-07-01" in compiled.sql
    assert compiled.comparison_metric_names == ("sales_revenue_comparison",)
    assert compiled.citation.comparison_label == "环比"


def test_citation_carries_metric_version_and_time_basis(orders):
    compiled = compile_intent(orders, _intent())

    assert compiled.citation.metric_name == "sales_revenue"
    assert compiled.citation.metric_version == 3
    assert compiled.citation.time_field_business_name == "完成日期"
    assert compiled.citation.time_start == date(2026, 8, 1)


def test_citation_renders_filters_in_business_terms(orders):
    compiled = compile_intent(
        orders,
        _intent(
            filters=[
                FilterCondition(
                    field="region_code",
                    operator=FilterOperator.IN,
                    values=["EC"],
                    spoken_values=["华东"],
                )
            ]
        ),
    )
    assert any("大区" in item and "华东" in item for item in compiled.citation.filters)


def test_ratio_metric_compiles_without_group_by_sum(orders):
    compiled = compile_intent(orders, _intent(metrics=["gross_margin_rate"]))
    assert "NULLIF" in compiled.sql.upper()


def test_multiple_metrics_compile_together(orders):
    compiled = compile_intent(orders, _intent(metrics=["sales_revenue", "order_count"]))
    assert compiled.metric_names == ("sales_revenue", "order_count")
    assert "order_count" in compiled.sql


def test_measure_as_dimension_is_rejected(orders):
    with pytest.raises(FieldNotGroupableError):
        compile_intent(orders, _intent(dimensions=["amount"]))


def test_unsupported_intent_is_rejected(orders):
    intent = QueryIntent(
        kind=IntentKind.UNSUPPORTED,
        dataset="orders",
        metrics=[],
        confidence=FieldConfidence(overall=0.2),
        raw_question="帮我下单",
    )
    with pytest.raises(CompileError):
        compile_intent(orders, intent)


def test_unpublished_dataset_is_rejected(meta_session):
    build_orders_dataset(meta_session, published=False)
    dataset = load_dataset(meta_session, "orders")

    with pytest.raises(CompileError):
        compile_intent(dataset, _intent())


def test_same_intent_compiles_to_identical_sql(orders):
    intent = _intent(dimensions=["province"], comparison=ComparisonKind.YOY)
    assert compile_intent(orders, intent).sql == compile_intent(orders, intent).sql


def test_compile_intent_rejects_aggregate_with_no_metrics(orders):
    # Schema validator now enforces metric requirement at construction time.
    # This test ensures the schema validator works (it's tested in test_schema.py).
    # Here we test that compile_intent would also reject such an intent if one
    # somehow bypassed the schema (e.g., during merge_followup with time=None).
    from app.compiler.errors import CompileError as _CompileError
    from app.intent.schema import IntentKind as _IntentKind, FieldConfidence

    # Create an intent with time=None (which passes the schema validator)
    # then manually set metrics=[] and time to a value to bypass validation
    intent = QueryIntent(
        kind=_IntentKind.AGGREGATE,
        dataset="orders",
        metrics=[],  # This would fail schema validation if time is set
        time=None,  # Bypass the validator
        confidence=FieldConfidence(overall=0.5),
        raw_question="随便",
    )

    # Manually verify this is what we intended to test
    assert intent.metrics == []
    assert intent.time is None
    with pytest.raises(_CompileError):
        compile_intent(orders, intent)
