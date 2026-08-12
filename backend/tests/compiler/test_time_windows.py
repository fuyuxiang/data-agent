from datetime import date

import pytest

from app.compiler.errors import UnsupportedComparisonError
from app.compiler.time_windows import comparison_label, comparison_range
from app.intent.schema import ComparisonKind, TimeGrain, TimeRange


def _month_range() -> TimeRange:
    return TimeRange(
        start=date(2026, 8, 1), end=date(2026, 8, 31), grain=TimeGrain.MONTH, expression="本月"
    )


def test_mom_shifts_back_one_calendar_month():
    result = comparison_range(_month_range(), ComparisonKind.MOM)
    assert result.start == date(2026, 7, 1)
    assert result.end == date(2026, 7, 31)


def test_mom_handles_shorter_previous_month():
    # 2026-03-01..2026-03-31 compared month over month lands on February,
    # which has 28 days in 2026. The end must clamp, not overflow into March.
    current = TimeRange(
        start=date(2026, 3, 1), end=date(2026, 3, 31), grain=TimeGrain.MONTH, expression="本月"
    )
    result = comparison_range(current, ComparisonKind.MOM)
    assert result.start == date(2026, 2, 1)
    assert result.end == date(2026, 2, 28)


def test_yoy_shifts_back_one_year():
    result = comparison_range(_month_range(), ComparisonKind.YOY)
    assert result.start == date(2025, 8, 1)
    assert result.end == date(2025, 8, 31)


def test_yoy_handles_leap_day():
    current = TimeRange(
        start=date(2024, 2, 29), end=date(2024, 2, 29), grain=TimeGrain.DAY, expression="当天"
    )
    result = comparison_range(current, ComparisonKind.YOY)
    assert result.start == date(2023, 2, 28)
    assert result.end == date(2023, 2, 28)


def test_wow_shifts_back_seven_days():
    current = TimeRange(
        start=date(2026, 8, 10), end=date(2026, 8, 16), grain=TimeGrain.WEEK, expression="本周"
    )
    result = comparison_range(current, ComparisonKind.WOW)
    assert result.start == date(2026, 8, 3)
    assert result.end == date(2026, 8, 9)


def test_qoq_shifts_back_three_months():
    current = TimeRange(
        start=date(2026, 7, 1), end=date(2026, 9, 30), grain=TimeGrain.QUARTER, expression="本季"
    )
    result = comparison_range(current, ComparisonKind.QOQ)
    assert result.start == date(2026, 4, 1)
    assert result.end == date(2026, 6, 30)


def test_previous_period_uses_same_length_window():
    current = TimeRange(
        start=date(2026, 8, 1), end=date(2026, 8, 12), grain=TimeGrain.DAY, expression="本月至今"
    )
    result = comparison_range(current, ComparisonKind.PREVIOUS_PERIOD)
    assert result.start == date(2026, 7, 20)
    assert result.end == date(2026, 7, 31)


def test_ytd_compares_against_same_span_last_year():
    current = TimeRange(
        start=date(2026, 1, 1), end=date(2026, 8, 12), grain=TimeGrain.DAY, expression="年初至今"
    )
    result = comparison_range(current, ComparisonKind.YTD)
    assert result.start == date(2025, 1, 1)
    assert result.end == date(2025, 8, 12)


def test_mtd_compares_against_same_span_last_month():
    current = TimeRange(
        start=date(2026, 8, 1), end=date(2026, 8, 12), grain=TimeGrain.DAY, expression="本月至今"
    )
    result = comparison_range(current, ComparisonKind.MTD)
    assert result.start == date(2026, 7, 1)
    assert result.end == date(2026, 7, 12)


def test_qtd_compares_against_same_span_last_quarter():
    current = TimeRange(
        start=date(2026, 7, 1), end=date(2026, 8, 12), grain=TimeGrain.DAY, expression="本季至今"
    )
    result = comparison_range(current, ComparisonKind.QTD)
    assert result.start == date(2026, 4, 1)
    assert result.end == date(2026, 5, 12)


def test_comparison_none_is_rejected():
    with pytest.raises(UnsupportedComparisonError):
        comparison_range(_month_range(), ComparisonKind.NONE)


def test_labels_are_chinese():
    assert comparison_label(ComparisonKind.MOM) == "环比"
    assert comparison_label(ComparisonKind.YOY) == "同比"