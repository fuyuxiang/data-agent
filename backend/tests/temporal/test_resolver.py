"""TimeResolver with boundary rules (S2 Task 1, Step 4-5).

Converts TimeExpression (relative time like "本月", "上周") to absolute
ResolvedTimeRange. Handles month-end clamping, leap years, DST, etc.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.temporal.clock import FixedClock
from app.temporal.fiscal import FiscalCalendar
from app.temporal.resolver import TimeResolver, TimeExpression, ResolvedTimeRange


class TestMonthBoundaries:
    """Test month-end clamping and leap year handling."""

    def test_month_end_clamping_jan31_plus_one_month(self):
        """Jan 31 + 1 month = Feb 28 (clamp to Feb length, non-leap year)."""
        now = FixedClock(datetime(2026, 1, 31, 10, 0, 0, tzinfo=timezone.utc))
        resolver = TimeResolver(now, ZoneInfo("Asia/Shanghai"), FiscalCalendar())

        # Offset +1 means "next month"
        expr = TimeExpression(
            kind="relative",
            text="下月",
            unit="month",
            offset=1,
            to_date=False,
        )

        result = resolver.resolve(expr, reference_date=date(2026, 1, 31))

        assert result.start == date(2026, 2, 1)
        assert result.end == date(2026, 2, 28)

    def test_month_end_clamping_leap_year(self):
        """Jan 31, 2024 + 1 month = Feb 29, 2024 (leap year)."""
        now = FixedClock(datetime(2024, 1, 31, 10, 0, 0, tzinfo=timezone.utc))
        resolver = TimeResolver(now, ZoneInfo("Asia/Shanghai"), FiscalCalendar())

        expr = TimeExpression(
            kind="relative",
            text="下月",
            unit="month",
            offset=1,
            to_date=False,
        )

        result = resolver.resolve(expr, reference_date=date(2024, 1, 31))

        assert result.start == date(2024, 2, 1)
        assert result.end == date(2024, 2, 29)

    def test_month_end_clamping_in_filter_context(self):
        """When computing filter predicates, Jan 31 + 1 month clamps to Feb 28."""
        # This tests the _month_offset helper used in filters/comparisons
        result = TimeResolver._month_offset(date(2026, 1, 31), 1)
        assert result == date(2026, 2, 28)

        result = TimeResolver._month_offset(date(2024, 1, 31), 1)
        assert result == date(2024, 2, 29)


class TestYearBoundaries:
    """Test year-end wrapping and full-year ranges."""

    def test_year_unit_returns_full_year(self):
        """Year unit returns Jan 1 to Dec 31, regardless of reference date."""
        now = FixedClock(datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc))
        resolver = TimeResolver(now, ZoneInfo("Asia/Shanghai"), FiscalCalendar())

        expr = TimeExpression(
            kind="relative",
            text="今年",
            unit="year",
            offset=0,
            to_date=False,
        )

        result = resolver.resolve(expr, reference_date=date(2026, 8, 15))

        assert result.start == date(2026, 1, 1)
        assert result.end == date(2026, 12, 31)

    def test_next_year_unit(self):
        """Next year (offset=1) returns full next year."""
        now = FixedClock(datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc))
        resolver = TimeResolver(now, ZoneInfo("Asia/Shanghai"), FiscalCalendar())

        expr = TimeExpression(
            kind="relative",
            text="明年",
            unit="year",
            offset=1,
            to_date=False,
        )

        result = resolver.resolve(expr, reference_date=date(2026, 8, 15))

        assert result.start == date(2027, 1, 1)
        assert result.end == date(2027, 12, 31)

    def test_last_year_unit(self):
        """Last year (offset=-1) returns full previous year."""
        now = FixedClock(datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc))
        resolver = TimeResolver(now, ZoneInfo("Asia/Shanghai"), FiscalCalendar())

        expr = TimeExpression(
            kind="relative",
            text="去年",
            unit="year",
            offset=-1,
            to_date=False,
        )

        result = resolver.resolve(expr, reference_date=date(2026, 8, 15))

        assert result.start == date(2025, 1, 1)
        assert result.end == date(2025, 12, 31)


class TestToDateRightEndpoint:
    """Test to_date=true: right endpoint should be yesterday, not today."""

    def test_this_month_to_date_ends_yesterday(self):
        """'本月至今' (this month to date) ends at yesterday, not today."""
        now = FixedClock(datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc))
        resolver = TimeResolver(now, ZoneInfo("Asia/Shanghai"), FiscalCalendar())

        expr = TimeExpression(
            kind="relative",
            text="本月至今",
            unit="month",
            offset=0,
            to_date=True,
        )

        result = resolver.resolve(expr, reference_date=date(2026, 8, 15))

        assert result.start == date(2026, 8, 1)
        assert result.end == date(2026, 8, 14)  # Yesterday
        assert len(result.assumptions) > 0
        assert "至今" in result.assumptions[0]

    def test_last_month_to_date_ends_previous_month_last_day(self):
        """'上月至今' ends at the last day of the previous month."""
        now = FixedClock(datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc))
        resolver = TimeResolver(now, ZoneInfo("Asia/Shanghai"), FiscalCalendar())

        expr = TimeExpression(
            kind="relative",
            text="上月至今",
            unit="month",
            offset=-1,
            to_date=True,
        )

        result = resolver.resolve(expr, reference_date=date(2026, 8, 15))

        assert result.start == date(2026, 7, 1)
        assert result.end == date(2026, 7, 31)  # Last day of previous month


class TestAbsoluteTimeExpression:
    """Test absolute time expressions (kind=absolute/range)."""

    def test_absolute_range_uses_provided_dates(self):
        """Absolute range with explicit start and end dates."""
        now = FixedClock(datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc))
        resolver = TimeResolver(now, ZoneInfo("Asia/Shanghai"), FiscalCalendar())

        expr = TimeExpression(
            kind="range",
            text="2026-08-01 到 2026-08-31",
            unit="day",
            offset=0,
            to_date=False,
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
        )

        result = resolver.resolve(expr)

        assert result.start == date(2026, 8, 1)
        assert result.end == date(2026, 8, 31)
