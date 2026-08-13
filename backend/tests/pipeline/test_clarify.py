"""Clarification request models + default-assumption fallback."""

import pytest

from app.pipeline.clarify import (
    ClarifyKind,
    ClarifyOption,
    ClarifyRequest,
    default_assumption,
)


def _request(**overrides) -> ClarifyRequest:
    payload = {
        "kind": ClarifyKind.METRIC,
        "target": "metrics",
        "question": "你指的是哪个销售额？",
        "options": (
            ClarifyOption(value="sales_revenue", label="销售额", hint="已完成订单含税金额"),
            ClarifyOption(value="new_customer_revenue", label="新客销售额", hint="新客的已完成订单金额"),
        ),
    }
    payload.update(overrides)
    return ClarifyRequest(**payload)


def test_default_assumption_takes_the_first_option():
    target, message = default_assumption(_request())

    assert target == "metrics"
    assert "销售额" in message


def test_default_assumption_message_says_it_is_a_default():
    _, message = default_assumption(_request())
    # The user must be able to see this was assumed, not asked.
    assert "默认" in message


def test_default_assumption_without_options_is_none():
    assert default_assumption(_request(options=())) is None


def test_clarify_request_is_immutable():
    request = _request()
    with pytest.raises(Exception):
        request.question = "改掉"  # type: ignore[misc]


def test_time_clarification_carries_concrete_ranges():
    request = _request(
        kind=ClarifyKind.TIME,
        target="time",
        question="「最近」指的是哪个范围？",
        options=(
            ClarifyOption(value="last_7_days", label="最近 7 天", hint=""),
            ClarifyOption(value="this_month", label="本月", hint=""),
        ),
    )
    target, message = default_assumption(request)

    assert target == "time"
    assert "最近 7 天" in message