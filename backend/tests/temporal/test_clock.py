"""Clock protocol and implementations (S2 Task 1, Step 1-2).

The old app/core/clock.py had module-level mutable state (_frozen, freeze(),
unfreeze()) that polluted tests and carried a bug where now() always returned
09:00. This module replaces it with a Clock protocol and two implementations:
SystemClock (real time) and FixedClock (for tests, zero shared state).
"""

from datetime import datetime, timezone

from app.temporal.clock import Clock, FixedClock, SystemClock


def test_system_clock_returns_utc_datetime():
    """SystemClock.now() returns a tz-aware datetime in UTC."""
    clock = SystemClock()
    now = clock.now()

    assert isinstance(now, datetime)
    assert now.tzinfo is not None
    assert now.tzinfo == timezone.utc


def test_system_clock_returns_current_time():
    """SystemClock.now() returns approximately the current time (within 10s)."""
    import time
    before = datetime.now(timezone.utc)
    time.sleep(0.01)  # 10ms to ensure clock advances
    clock = SystemClock()
    result = clock.now()
    time.sleep(0.01)
    after = datetime.now(timezone.utc)

    assert before <= result <= after


def test_fixed_clock_returns_injected_instant():
    """FixedClock returns the exact instant it was given."""
    instant = datetime(2026, 8, 13, 10, 30, 45, tzinfo=timezone.utc)
    clock = FixedClock(instant)

    assert clock.now() == instant


def test_fixed_clock_returns_same_instant_on_repeated_calls():
    """FixedClock.now() is deterministic: always returns the same instant."""
    instant = datetime(2026, 8, 13, 10, 30, 45, tzinfo=timezone.utc)
    clock = FixedClock(instant)

    result1 = clock.now()
    result2 = clock.now()
    result3 = clock.now()

    assert result1 == result2 == result3 == instant


def test_fixed_clock_instances_are_independent():
    """Two FixedClock instances with different instants have no shared state."""
    instant1 = datetime(2026, 8, 13, 10, 30, 45, tzinfo=timezone.utc)
    instant2 = datetime(2026, 8, 14, 14, 30, 45, tzinfo=timezone.utc)

    clock1 = FixedClock(instant1)
    clock2 = FixedClock(instant2)

    assert clock1.now() == instant1
    assert clock2.now() == instant2
    assert clock1.now() == instant1  # clock1 still returns its instant


def test_clock_is_a_protocol():
    """Both SystemClock and FixedClock implement the Clock protocol."""
    system = SystemClock()
    fixed = FixedClock(datetime(2026, 8, 13, tzinfo=timezone.utc))

    # Both should have a now() method that returns a datetime
    assert hasattr(system, 'now')
    assert callable(system.now)
    assert hasattr(fixed, 'now')
    assert callable(fixed.now)

    assert isinstance(system.now(), datetime)
    assert isinstance(fixed.now(), datetime)


def test_no_module_level_mutable_state():
    """The temporal module has no freeze/unfreeze functions or module-level _frozen variable."""
    import app.temporal.clock as clock_module

    assert not hasattr(clock_module, '_frozen')
    assert not hasattr(clock_module, 'freeze')
    assert not hasattr(clock_module, 'unfreeze')
