from datetime import datetime

import pytest

from backend.services.followup import followup_payload


@pytest.mark.parametrize("question,expected", [
    ("改看上月销售额", {"start": "2025-12-01", "end": "2025-12-31"}),
    ("改看上个月销售额", {"start": "2025-12-01", "end": "2025-12-31"}),
    ("改看本月销售额", {"start": "2026-01-01", "end": "2026-01-10"}),
    ("改看2024年二月", {"start": "2024-02-01", "end": "2024-02-29"}),
    ("改看2026-02-01至2026-02-10", {"start": "2026-02-01", "end": "2026-02-10"}),
    ("比较去年2月", {}),
    ("比较2026年1月和2月", {}),
    ("改看2026年13月", {}),
    ("改看旺季", {}),
    ("改看最近三个月", {}),
    ("改看昨天", {"start": "2026-01-09", "end": "2026-01-09"}),
])
def test_followup_dates_are_explicit_or_require_a_choice(monkeypatch, question, expected):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 10, 12, tzinfo=tz)

    monkeypatch.setattr("backend.services.followup.datetime", FixedDatetime)
    payload, change = followup_payload({"objective": question}, {
        "time_range": {"start": "2026-01-01", "end": "2026-01-31"},
        "coverage": "华东一月记录", "timezone": "Asia/Shanghai",
    })
    assert payload["contract"]["time_range"] == expected
    assert payload["confirm_required"] is True
    assert change["requires_time_selection"] is (not bool(expected))


def test_same_period_followup_keeps_the_original_scope_and_can_start_normally():
    previous = {"time_range": {"start": "2026-01-01", "end": "2026-01-31"},
                "coverage": "一月华东", "metrics": ["销售额"], "dimensions": ["区域"]}
    payload, change = followup_payload({"objective": "请比较同一时间范围的地区差异"}, previous)
    assert payload["contract"]["time_range"] == previous["time_range"]
    assert payload["contract"]["coverage"] == previous["coverage"]
    assert change is None


def test_changed_business_metric_clears_old_metric_and_honors_explicit_fields():
    previous = {"time_range": {"start": "2026-01-01", "end": "2026-01-31"},
                "coverage": "一月华东销售记录", "metrics": ["销售额"], "dimensions": ["区域"]}
    payload, change = followup_payload({"objective": "改看利润", "contract": {
        "objective": "改看利润", "metrics": ["利润"], "dimensions": ["商品"], "coverage": "华东商品",
    }}, previous)
    assert payload["contract"]["metrics"] == ["利润"]
    assert payload["contract"]["dimensions"] == ["商品"]
    assert payload["contract"]["time_range"] == previous["time_range"]
    assert payload["contract"]["coverage"] == "华东商品"
    assert change["kind"] == "scope"


def test_changing_only_the_delivery_format_keeps_reusable_business_scope():
    previous = {"time_range": {"start": "2026-01-01", "end": "2026-01-31"},
                "coverage": "一月华东", "metrics": ["销售额"], "dimensions": ["区域"]}
    payload, change = followup_payload({"objective": "把原来的分析改成PPT"}, previous)
    assert payload["contract"]["coverage"] == previous["coverage"]
    assert payload["contract"]["time_range"] == previous["time_range"]
    assert change is None
