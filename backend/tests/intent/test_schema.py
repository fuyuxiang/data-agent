from datetime import date

import pytest
from pydantic import ValidationError

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


def _intent(**overrides) -> QueryIntent:
    payload = {
        "kind": IntentKind.AGGREGATE,
        "dataset": "orders",
        "metrics": ["sales_revenue"],
        "time": TimeRange(
            start=date(2026, 8, 1), end=date(2026, 8, 12), grain=TimeGrain.MONTH, expression="本月"
        ),
        "dimensions": [],
        "filters": [
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=["EC"],
                spoken_values=["华东"],
            )
        ],
        "comparison": ComparisonKind.MOM,
        "confidence": FieldConfidence(metric=0.95, time=0.9, dimension=1.0, filter=0.92, overall=0.9),
        "raw_question": "华东本月销售额环比",
    }
    payload.update(overrides)
    return QueryIntent(**payload)


def test_intent_round_trips_through_json():
    intent = _intent()
    restored = QueryIntent.model_validate_json(intent.model_dump_json())
    assert restored == intent


def test_time_range_rejects_end_before_start():
    with pytest.raises(ValidationError):
        TimeRange(start=date(2026, 8, 12), end=date(2026, 8, 1), grain=TimeGrain.MONTH, expression="")


def test_aggregate_intent_requires_at_least_one_metric():
    with pytest.raises(ValidationError):
        _intent(metrics=[])


def test_slot_signature_is_stable_and_order_insensitive():
    left = _intent(dimensions=["province", "channel"])
    right = _intent(dimensions=["channel", "province"])
    assert left.slot_signature() == right.slot_signature()


def test_slot_signature_changes_with_filter_value():
    base = _intent()
    other = _intent(
        filters=[
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=["SC"],
                spoken_values=["华南"],
            )
        ]
    )
    assert base.slot_signature() != other.slot_signature()


def test_signature_excludes_confidence_and_raw_question():
    # Confidence varies run to run; it must not affect cache identity.
    base = _intent()
    other = _intent(
        confidence=FieldConfidence(metric=0.5, time=0.5, dimension=0.5, filter=0.5, overall=0.5),
        raw_question="换个说法问同一件事",
    )
    assert base.slot_signature() == other.slot_signature()


def test_merge_followup_replaces_only_provided_slots():
    # "那华南呢" changes the filter slot and nothing else (spec M-19).
    base = _intent(dimensions=["province"])
    followup = QueryIntent(
        kind=IntentKind.AGGREGATE,
        dataset="orders",
        metrics=[],
        time=None,
        dimensions=[],
        filters=[
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=["SC"],
                spoken_values=["华南"],
            )
        ],
        comparison=ComparisonKind.NONE,
        confidence=FieldConfidence(metric=0.0, time=0.0, dimension=0.0, filter=0.9, overall=0.9),
        raw_question="那华南呢",
    )

    merged = base.merge_followup(followup)

    assert merged.metrics == ["sales_revenue"]
    assert merged.dimensions == ["province"]
    assert merged.time == base.time
    assert merged.filters[0].values == ["SC"]
    assert merged.raw_question == "那华南呢"


def test_merge_followup_can_add_dimension_without_losing_filter():
    base = _intent()
    followup = QueryIntent(
        kind=IntentKind.AGGREGATE,
        dataset="orders",
        metrics=[],
        time=None,
        dimensions=["province"],
        filters=[],
        comparison=ComparisonKind.NONE,
        confidence=FieldConfidence(metric=0.0, time=0.0, dimension=0.9, filter=0.0, overall=0.9),
        raw_question="按省拆一下",
    )

    merged = base.merge_followup(followup)

    assert merged.dimensions == ["province"]
    assert merged.filters[0].values == ["EC"]
    assert merged.comparison == ComparisonKind.MOM