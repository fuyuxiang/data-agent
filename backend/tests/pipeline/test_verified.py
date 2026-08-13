"""Verified Query dual-path recall."""

from datetime import date

import pytest

from app.intent.schema import (
    FieldConfidence,
    IntentKind,
    QueryIntent,
    TimeGrain,
    TimeRange,
)
from app.pipeline.verified import normalize_question, recall, register


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
        "confidence": FieldConfidence(overall=0.9),
        "raw_question": "本月销售额",
    }
    payload.update(overrides)
    return QueryIntent(**payload)


@pytest.fixture
def registered(meta_session):
    register(
        meta_session,
        dataset_name="orders",
        question="本月销售额是多少",
        fixed_sql="SELECT SUM(amount) AS sales_revenue FROM sample.orders",
        intent=_intent(),
        created_by="admin",
    )
    meta_session.flush()
    return meta_session


def test_exact_question_hits(registered):
    hit = recall(registered, "orders", "本月销售额是多少")

    assert hit is not None
    assert hit.match_kind == "question"
    assert "sales_revenue" in hit.fixed_sql


def test_whitespace_and_punctuation_differences_still_hit(registered):
    assert recall(registered, "orders", " 本月销售额是多少？ ") is not None


def test_different_question_misses(registered):
    assert recall(registered, "orders", "上月客单价") is None


def test_same_slots_different_wording_hits(registered):
    """Slot signature recall: phrasing varies, the query does not."""
    hit = recall(
        registered,
        "orders",
        "这个月卖了多少钱",
        intent=_intent(raw_question="这个月卖了多少钱"),
    )

    assert hit is not None
    assert hit.match_kind == "slots"


def test_different_slots_miss(registered):
    other = _intent(metrics=["order_count"], raw_question="本月订单量")
    assert recall(registered, "orders", "本月订单量", intent=other) is None


def test_recall_is_scoped_to_dataset(registered):
    assert recall(registered, "other_dataset", "本月销售额是多少") is None


def test_inactive_entry_is_not_recalled(registered):
    from app.observability.orm import VerifiedQueryRow

    row = registered.query(VerifiedQueryRow).one()
    row.is_active = False
    registered.flush()

    assert recall(registered, "orders", "本月销售额是多少") is None


def test_hit_count_increases(registered):
    from app.observability.orm import VerifiedQueryRow

    recall(registered, "orders", "本月销售额是多少")
    recall(registered, "orders", "本月销售额是多少")
    registered.flush()

    assert registered.query(VerifiedQueryRow).one().hit_count == 2


def test_normalize_strips_punctuation_and_case():
    assert normalize_question(" 本月 GMV 是多少？ ") == normalize_question("本月gmv是多少")


def test_register_stores_the_slot_signature(registered):
    from app.observability.orm import VerifiedQueryRow

    row = registered.query(VerifiedQueryRow).one()
    assert row.slot_signature == _intent().slot_signature()
    assert row.intent_snapshot["metrics"] == ["sales_revenue"]


def test_question_match_wins_over_slot_match(meta_session):
    register(
        meta_session,
        dataset_name="orders",
        question="本月销售额是多少",
        fixed_sql="SELECT 1 AS a",
        intent=_intent(),
        created_by="admin",
    )
    register(
        meta_session,
        dataset_name="orders",
        question="完全不同的问法",
        fixed_sql="SELECT 2 AS b",
        intent=_intent(),
        created_by="admin",
    )
    meta_session.flush()

    hit = recall(meta_session, "orders", "本月销售额是多少", intent=_intent())
    assert hit.match_kind == "question"
    assert "1 AS a" in hit.fixed_sql