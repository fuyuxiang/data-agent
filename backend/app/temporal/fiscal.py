"""Fiscal calendar configuration and quarter/week boundary calculations."""

from __future__ import annotations

from datetime import date, timedelta


class FiscalCalendar:
    """Configurable fiscal year and week start for proper calendar boundaries.

    Parameters:
    - year_start_month: Month (1-12) when the fiscal year begins (default 1 = January)
    - week_start: Day of week (0=Monday, 6=Sunday) for week boundaries (default 0 = ISO 8601)
    """

    def __init__(self, year_start_month: int = 1, week_start: int = 0) -> None:
        if not 1 <= year_start_month <= 12:
            raise ValueError("year_start_month must be 1-12")
        if not 0 <= week_start <= 6:
            raise ValueError("week_start must be 0-6")

        self.year_start_month = year_start_month
        self.week_start = week_start

    def quarter_start(self, year: int, quarter: int) -> date:
        """Return the first day of the given fiscal quarter (1-4)."""
        if not 1 <= quarter <= 4:
            raise ValueError("quarter must be 1-4")

        # Calculate which month Q1 starts in
        q1_month = self.year_start_month

        # Month offset for the quarter (0, 3, 6, 9)
        month_offset = (quarter - 1) * 3

        # Calculate the target month, wrapping years as needed
        target_month = q1_month + month_offset

        target_year = year
        if target_month > 12:
            target_month -= 12
            target_year += 1

        return date(target_year, target_month, 1)

    def quarter_end(self, year: int, quarter: int) -> date:
        """Return the last day of the given fiscal quarter (1-4)."""
        # Get the start of the next quarter
        if quarter < 4:
            next_q_start = self.quarter_start(year, quarter + 1)
        else:
            next_q_start = self.quarter_start(year + 1, 1)

        # Last day of this quarter is the day before the next quarter starts
        return next_q_start - timedelta(days=1)

    def week_start_date(self, year: int, week: int) -> date:
        """Return the first day of the given week (1-53) in ISO-style weeks.

        The week_start configuration determines which weekday is considered
        the start of the week.
        """
        if not 1 <= week <= 53:
            raise ValueError("week must be 1-53")

        # For ISO 8601, week 1 is the week with Thursday in it
        # Start by finding Jan 4 (guaranteed to be in week 1)
        jan4 = date(year, 1, 4)

        # Find the Monday of the week containing Jan 4
        days_since_configured_week_start = (jan4.weekday() - self.week_start) % 7
        week1_start = jan4 - timedelta(days=days_since_configured_week_start)

        # Calculate the start date of the requested week
        week_offset = (week - 1) * 7
        return week1_start + timedelta(days=week_offset)
