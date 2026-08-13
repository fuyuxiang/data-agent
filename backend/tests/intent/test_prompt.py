"""Prompt builder for the LLM intent recognition stage."""

import pytest

from app.intent.prompt import build_intent_prompt
from app.semantic.loader import load_dataset
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def orders(meta_session):
    build_orders_dataset(meta_session)
    return load_dataset(meta_session, "orders")


def test_prompt_lists_metrics_with_definitions(orders):
    _, user = build_intent_prompt(orders, "本月销售额")

    assert "sales_revenue" in user
    assert "销售额" in user
    assert "已完成订单含税金额" in user


def test_prompt_lists_groupable_dimensions(orders):
    _, user = build_intent_prompt(orders, "各省销售额")

    assert "province" in user
    # amount is not groupable and must not be offered as a dimension.
    assert "维度" in user


def test_prompt_excludes_non_groupable_fields_from_dimensions(orders):
    _, user = build_intent_prompt(orders, "各省销售额")
    dimension_block = user.split("可用维度")[1].split("可用过滤字段")[0]

    assert "province" in dimension_block
    assert "amount" not in dimension_block


def test_prompt_does_not_enumerate_enum_values(orders):
    """Spec 4.3: value dictionaries are looked up, not stuffed into the prompt."""
    _, user = build_intent_prompt(orders, "华东销售额")

    # The user-question block legitimately contains the user's own words; the
    # catalogue (metrics, dimensions, filters) must not enumerate enum values.
    catalogue = user.rsplit("用户问题", 1)[0]

    assert "华东" not in catalogue
    assert "EC" not in catalogue


def test_prompt_states_forbidden_scenario(orders):
    _, user = build_intent_prompt(orders, "确认收入多少")
    assert "不可用于财务确认收入口径" in user


def test_prompt_forbids_sql(orders):
    system, _ = build_intent_prompt(orders, "本月销售额")
    assert "SQL" in system


def test_prompt_requires_per_field_confidence(orders):
    system, _ = build_intent_prompt(orders, "本月销售额")
    assert "confidence" in system


def test_prompt_carries_slot_state_for_followups(orders):
    _, user = build_intent_prompt(
        orders,
        "那华南呢",
        slot_state={"metrics": ["sales_revenue"], "filters": {"region_code": ["EC"]}},
    )
    assert "上一轮" in user
    assert "sales_revenue" in user


def test_prompt_omits_slot_block_on_first_turn(orders):
    _, user = build_intent_prompt(orders, "本月销售额")
    assert "上一轮" not in user


def test_prompt_is_deterministic(orders):
    assert build_intent_prompt(orders, "本月销售额") == build_intent_prompt(orders, "本月销售额")