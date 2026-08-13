"""Time handling with no module-level mutable state.

The Clock protocol replaces the broken app/core/clock.py design which had
module-level _frozen + freeze()/unfreeze() causing test pollution and a bug
where now() always returned 09:00.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    """Time provider protocol. Implementations must return tz-aware UTC datetimes."""

    def now(self) -> datetime:
        """Return the current time as a tz-aware datetime in UTC."""
        ...


class SystemClock:
    """Returns the actual current time (UTC)."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """Returns a fixed instant (for testing). No shared state between instances."""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant
