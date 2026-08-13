"""Fiscal calendar support (S2 Task 1, Step 3-4).

FiscalCalendar handles year start month and week start day configuration,
needed for proper quarter and week boundary calculations.
"""

from datetime import date

import pytest

from app.temporal.fiscal import FiscalCalendar


def test_default_fiscal_calendar_is_natural_year():
    """Default FiscalCalendar uses January as year start and Monday as week start."""
    cal = FiscalCalendar()

    assert cal.year_start_month == 1
    assert cal.week_start == 0  # ISO 8601: Monday


def test_custom_fiscal_year_start():
    """FiscalCalendar accepts custom year_start_month (e.g., April for UK fiscal year)."""
    cal = FiscalCalendar(year_start_month=4)

    assert cal.year_start_month == 4


def test_custom_week_start():
    """FiscalCalendar accepts custom week_start (0=Monday, 6=Sunday)."""
    cal = FiscalCalendar(week_start=6)

    assert cal.week_start == 6


def test_quarter_boundaries_natural_year():
    """Q1/Q2/Q3/Q4 boundaries for natural year (Jan-Mar, Apr-Jun, Jul-Sep, Oct-Dec)."""
    cal = FiscalCalendar(year_start_month=1)

    q1_start = cal.quarter_start(year=2026, quarter=1)
    q1_end = cal.quarter_end(year=2026, quarter=1)
    q2_start = cal.quarter_start(year=2026, quarter=2)

    assert q1_start == date(2026, 1, 1)
    assert q1_end == date(2026, 3, 31)
    assert q2_start == date(2026, 4, 1)


def test_quarter_boundaries_custom_fiscal_year():
    """Q1/Q2/Q3/Q4 boundaries for UK fiscal year (Apr-Jun, Jul-Sep, Oct-Dec, Jan-Mar)."""
    cal = FiscalCalendar(year_start_month=4)

    q1_start = cal.quarter_start(year=2026, quarter=1)
    q1_end = cal.quarter_end(year=2026, quarter=1)
    q4_end = cal.quarter_end(year=2026, quarter=4)

    assert q1_start == date(2026, 4, 1)
    assert q1_end == date(2026, 6, 30)
    assert q4_end == date(2027, 3, 31)  # FY 2026 ends in Mar 2027


def test_week_start_iso_monday():
    """Week 1 of ISO calendar starts on Monday."""
    cal = FiscalCalendar(week_start=0)  # Monday

    # 2026-01-01 is a Thursday; week 1 starts on the previous Monday (2025-12-29)
    week1_start = cal.week_start_date(year=2026, week=1)

    assert week1_start.weekday() == 0  # Monday
    assert week1_start == date(2025, 12, 29)


def test_week_start_sunday():
    """When week_start=6 (Sunday), week boundaries shift accordingly."""
    cal = FiscalCalendar(week_start=6)  # Sunday

    # First Sunday of 2026 is 2026-01-04
    week1_start = cal.week_start_date(year=2026, week=1)

    assert week1_start.weekday() == 6  # Sunday
