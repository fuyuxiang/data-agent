"""Stage 3: value mapping + ambiguity → clarification requests."""

from datetime import date

import pytest

from app.core.config import Settings
from app.intent.schema import (
    ComparisonKind,
    FieldConfidence,
    FilterCondition,
    FilterOperator,
    IntentKind,
    QueryIntent,
    TimeGrain,
    TimeRange,
)
from app.pipeline.clarify import ClarifyKind
from app.pipeline.resolve import resolve_intent
from app.semantic.loader import load_dataset
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def orders(meta_session):
    build_orders_dataset(meta_session)
    return load_dataset(meta_session, "orders")


def _settings(**overrides) -> Settings:
    base = {"clarify_confidence_threshold": 0.7, "clarify_max_rounds": 2}
    base.update(overrides)
    return Settings(**base)


def _intent(**overrides) -> QueryIntent:
    payload = {
        "kind": IntentKind.AGGREGATE,
        "dataset": "orders",
        "metrics": ["sales_revenue"],
        "time": TimeRange(
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            grain=TimeGrain.MONTH,
            expression="本月",
        ),
        "confidence": FieldConfidence(overall=0.9, metric=0.95, time=0.9),
        "raw_question": "本月销售额",
    }
    payload.update(overrides)
    return QueryIntent(**payload)


def _kinds(outcome):
    return {item.kind for item in outcome.clarifications}


def test_clear_intent_resolves_without_clarification(orders):
    outcome = resolve_intent(orders, _intent(), _settings())

    assert outcome.clarifications == ()
    assert outcome.intent.metrics == ["sales_revenue"]


def test_spoken_values_are_mapped_to_physical_values(orders):
    intent = _intent(
        filters=[
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=[],
                spoken_values=["华东", "华南"],
            )
        ]
    )
    outcome = resolve_intent(orders, intent, _settings())

    assert outcome.clarifications == ()
    assert outcome.intent.filters[0].values == ["EC", "SC"]
    # The spoken form is kept for the citation block.
    assert outcome.intent.filters[0].spoken_values == ["华东", "华南"]


def test_alias_is_mapped(orders):
    intent = _intent(
        filters=[
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=[],
                spoken_values=["东区"],
            )
        ]
    )
    outcome = resolve_intent(orders, intent, _settings())
    assert outcome.intent.filters[0].values == ["EC"]


def test_unmappable_value_triggers_entity_clarification(orders):
    """Spec 4.3: mapping failure clarifies, never queries an empty set."""
    intent = _intent(
        filters=[
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=[],
                spoken_values=["华中"],
            )
        ]
    )
    outcome = resolve_intent(orders, intent, _settings())

    assert ClarifyKind.ENTITY in _kinds(outcome)
    request = next(item for item in outcome.clarifications if item.kind == ClarifyKind.ENTITY)
    assert {option.value for option in request.options} == {"EC", "SC", "NC"}


def test_non_enum_filter_values_pass_through(orders):
    intent = _intent(
        filters=[
            FilterCondition(
                field="amount",
                operator=FilterOperator.GT,
                values=[],
                spoken_values=["1000"],
            )
        ]
    )
    outcome = resolve_intent(orders, intent, _settings())

    assert outcome.clarifications == ()
    assert outcome.intent.filters[0].values == ["1000"]


def test_low_metric_confidence_triggers_metric_clarification(orders):
    intent = _intent(confidence=FieldConfidence(overall=0.5, metric=0.4, time=0.9))
    outcome = resolve_intent(orders, intent, _settings())

    assert ClarifyKind.METRIC in _kinds(outcome)
    request = next(item for item in outcome.clarifications if item.kind == ClarifyKind.METRIC)
    # Options carry the definition so the user picks a caliber, not a name.
    assert any("已完成订单含税金额" in option.hint for option in request.options)


def test_low_time_confidence_triggers_time_clarification(orders):
    intent = _intent(confidence=FieldConfidence(overall=0.6, metric=0.95, time=0.3))
    assert ClarifyKind.TIME in _kinds(resolve_intent(orders, intent, _settings()))


def test_missing_time_triggers_time_clarification(orders):
    intent = _intent(time=None)
    assert ClarifyKind.TIME in _kinds(resolve_intent(orders, intent, _settings()))


def test_low_dimension_confidence_triggers_dimension_clarification(orders):
    intent = _intent(
        dimensions=["province"],
        confidence=FieldConfidence(overall=0.6, metric=0.95, time=0.9, dimension=0.3),
    )
    assert ClarifyKind.DIMENSION in _kinds(resolve_intent(orders, intent, _settings()))


def test_no_metric_recognized_is_reported_not_guessed(orders):
    """Spec 5.1: never fall back to a nearby field."""
    intent = _intent(metrics=[], confidence=FieldConfidence(overall=0.3, metric=0.2))
    outcome = resolve_intent(orders, intent, _settings())

    assert ClarifyKind.METRIC in _kinds(outcome)
    assert outcome.intent.metrics == []


def test_multiple_ambiguities_are_all_reported(orders):
    intent = _intent(
        time=None,
        confidence=FieldConfidence(overall=0.3, metric=0.2, time=0.2),
    )
    assert {ClarifyKind.METRIC, ClarifyKind.TIME} <= _kinds(
        resolve_intent(orders, intent, _settings())
    )


def test_max_rounds_exceeded_uses_defaults_and_records_assumptions(orders):
    intent = _intent(confidence=FieldConfidence(overall=0.4, metric=0.3, time=0.9))
    outcome = resolve_intent(orders, intent, _settings(), round_index=2)

    # Spec 5.2: answer with a default, but the assumption must be stated.
    assert outcome.clarifications == ()
    assert outcome.assumptions
    assert any("默认" in item for item in outcome.assumptions)


def test_defaults_fill_the_slot_they_were_chosen_for(orders):
    intent = _intent(metrics=[], confidence=FieldConfidence(overall=0.3, metric=0.2))
    outcome = resolve_intent(orders, intent, _settings(), round_index=2)

    assert outcome.intent.metrics
    assert outcome.intent.metrics[0] in {metric.name for metric in orders.metrics}


def test_model_assumptions_are_preserved(orders):
    intent = _intent(assumptions=["「最近」按本月理解"])
    outcome = resolve_intent(orders, intent, _settings())
    assert "「最近」按本月理解" in outcome.assumptions


def test_unsupported_intent_is_not_clarified(orders):
    intent = _intent(
        kind=IntentKind.UNSUPPORTED, metrics=[], confidence=FieldConfidence(overall=0.1)
    )
    outcome = resolve_intent(orders, intent, _settings())

    # Out-of-scope questions are refused upstream, not clarified into shape.
    assert outcome.clarifications == ()
    assert outcome.intent.kind == IntentKind.UNSUPPORTED


def test_comparison_without_time_still_clarifies_time(orders):
    intent = _intent(time=None, comparison=ComparisonKind.MOM)
    assert ClarifyKind.TIME in _kinds(resolve_intent(orders, intent, _settings()))