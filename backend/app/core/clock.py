"""Single source of truth for wall clock. Tests freeze it via frozen_clock fixture."""

from datetime import date, datetime
from threading import Lock

_lock = Lock()
_frozen: date | None = None


def today() -> date:
    """Return the process's notion of today. Frozen by frozen_clock fixture."""
    with _lock:
        if _frozen is not None:
            return _frozen
    return date.today()


def now() -> datetime:
    """Wall clock as datetime. Frozen to today's 09:00 to match the sample data_updated_at."""
    d = today()
    return datetime(d.year, d.month, d.day, 9, 0)


def freeze(d: date) -> None:
    """Set today's date for the process. Used by frozen_clock fixture."""
    global _frozen
    with _lock:
        _frozen = d


def unfreeze() -> None:
    global _frozen
    with _lock:
        _frozen = None