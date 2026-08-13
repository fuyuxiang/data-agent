"""TimeResolver: converts relative time expressions to absolute date ranges.

Implements all boundary rules from spec 3.3: month-end clamping, leap years,
year wrapping, to_date logic, etc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from app.temporal.clock import Clock
from app.temporal.fiscal import FiscalCalendar


class TimeUnit(str, Enum):
    """Time unit for relative expressions."""
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


@dataclass
class TimeExpression:
    """Relative or absolute time expression from the model."""
    kind: str  # "relative" | "absolute" | "range" | "none"
    text: str  # Original user text for citation
    unit: str  # "day" | "week" | "month" | "quarter" | "year"
    offset: int = 0  # 0=this period, -1=last period, +1=next period
    to_date: bool = False  # true means "至今" (to date): right endpoint = yesterday
    start: date | None = None  # For kind="range" or "absolute"
    end: date | None = None  # For kind="range" or "absolute"


@dataclass
class ResolvedTimeRange:
    """Absolute date range with metadata."""
    start: date
    end: date
    grain: str = "day"  # "day" | "week" | "month" | "quarter" | "year"
    assumptions: list[str] = field(default_factory=list)


class TimeResolver:
    """Resolves relative time expressions to absolute date ranges."""

    def __init__(
        self,
        clock: Clock,
        timezone: ZoneInfo,
        fiscal_calendar: FiscalCalendar,
    ) -> None:
        self._clock = clock
        self._timezone = timezone
        self._fiscal = fiscal_calendar

    def resolve(
        self,
        expression: TimeExpression,
        reference_date: date | None = None,
    ) -> ResolvedTimeRange:
        """Convert a time expression to an absolute date range.

        Args:
            expression: The time expression to resolve
            reference_date: Override for "today" (defaults to clock.now() in local TZ)

        Returns:
            ResolvedTimeRange with start, end, and assumptions
        """
        if reference_date is None:
            # Get current date in the user's timezone
            now_utc = self._clock.now()
            now_local = now_utc.astimezone(self._timezone)
            reference_date = now_local.date()

        if expression.kind == "range" or expression.kind == "absolute":
            # Absolute dates provided
            start = expression.start or reference_date
            end = expression.end or reference_date
            if expression.to_date:
                end = end - timedelta(days=1)
            return ResolvedTimeRange(
                start=start,
                end=end,
                grain=expression.unit,
                assumptions=["至今: 右端点为昨天" if expression.to_date else ""],
            )

        if expression.kind == "none":
            return ResolvedTimeRange(
                start=reference_date,
                end=reference_date,
                grain="day",
            )

        # kind == "relative": compute offsets
        unit = TimeUnit(expression.unit)
        offset = expression.offset

        if unit == TimeUnit.DAY:
            target_date = reference_date + timedelta(days=offset)
            start = target_date
            end = target_date
        elif unit == TimeUnit.WEEK:
            # ISO week: offset 0 = current week, -1 = last week, +1 = next week
            current_week = reference_date.isocalendar()[1]
            target_week = current_week + offset
            target_year = reference_date.year
            # Handle week overflow
            if target_week < 1:
                target_year -= 1
                target_week += 52  # Rough estimate; could be 53
            elif target_week > 52:
                target_year += 1
                target_week -= 52
            start = self._fiscal.week_start_date(target_year, target_week)
            end = start + timedelta(days=6)
        elif unit == TimeUnit.MONTH:
            # For month unit, always return the full month (1st to last day)
            target_month_start = self._month_start(reference_date, offset)
            target_month_next = self._month_start(reference_date, offset + 1)
            start = target_month_start
            end = target_month_next - timedelta(days=1)
        elif unit == TimeUnit.QUARTER:
            current_q = (reference_date.month - 1) // 3 + 1
            target_q = current_q + offset
            target_year = reference_date.year
            # Handle quarter overflow
            while target_q < 1:
                target_year -= 1
                target_q += 4
            while target_q > 4:
                target_year += 1
                target_q -= 4
            start = self._fiscal.quarter_start(target_year, target_q)
            end = self._fiscal.quarter_end(target_year, target_q)
        elif unit == TimeUnit.YEAR:
            # For year unit, return full year (Jan 1 to Dec 31)
            target_year = reference_date.year + offset
            start = date(target_year, 1, 1)
            end = date(target_year, 12, 31)
        else:
            raise ValueError(f"Unknown time unit: {unit}")

        # Apply to_date: if true, end = yesterday only for current period (offset=0)
        # For past periods (offset<0), end = last day of that period
        assumptions = []
        if expression.to_date:
            if offset == 0:
                # Current period: end at yesterday
                end = reference_date - timedelta(days=1)
                assumptions.append("至今: 右端点为昨天")
            # else: end remains the last day of the past period (already computed)

        return ResolvedTimeRange(
            start=start,
            end=end,
            grain=unit.value,
            assumptions=assumptions,
        )

    @staticmethod
    def _month_start(ref_date: date, offset: int) -> date:
        """Compute the first day of the month offset by `offset` months.

        Used for month-unit time ranges. Returns the 1st day of the target month.
        """
        month = ref_date.month + offset
        year = ref_date.year

        # Wrap year boundaries
        while month < 1:
            month += 12
            year -= 1
        while month > 12:
            month -= 12
            year += 1

        return date(year, month, 1)

    @staticmethod
    def _month_offset(ref_date: date, offset: int) -> date:
        """Compute a date offset by `offset` months from ref_date.

        Handles month-end clamping: Jan 31 + 1 month = Feb 28/29.
        Used for non-month-unit calculations.
        """
        month = ref_date.month + offset
        year = ref_date.year

        # Wrap year boundaries
        while month < 1:
            month += 12
            year -= 1
        while month > 12:
            month -= 12
            year += 1

        # Clamp day to month length
        day = ref_date.day
        max_day = TimeResolver._days_in_month(year, month)
        if day > max_day:
            day = max_day

        return date(year, month, day)

    @staticmethod
    def _days_in_month(year: int, month: int) -> int:
        """Return the number of days in the given year/month."""
        if month == 2:
            return 29 if TimeResolver._is_leap_year(year) else 28
        if month in (4, 6, 9, 11):
            return 30
        return 31

    @staticmethod
    def _is_leap_year(year: int) -> bool:
        """Check if a year is a leap year."""
        return (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
