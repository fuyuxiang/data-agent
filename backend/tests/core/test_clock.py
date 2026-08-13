"""Tests for the wall-clock module."""

from datetime import date, datetime

import pytest

from app.core import clock


@pytest.fixture(autouse=True)
def _reset_clock():
    """Ensure each test starts and ends with no frozen state."""
    clock.unfreeze()
    yield
    clock.unfreeze()


def test_today_returns_real_date_when_unfrozen():
    result = clock.today()
    assert isinstance(result, date)
    assert result == date.today()


def test_now_returns_datetime_when_unfrozen():
    result = clock.now()
    assert isinstance(result, datetime)
    assert result.year == date.today().year


def test_freeze_changes_today():
    frozen = date(2026, 8, 12)
    clock.freeze(frozen)
    assert clock.today() == frozen


def test_freeze_changes_now_to_today_09_00():
    frozen = date(2026, 8, 12)
    clock.freeze(frozen)
    assert clock.now() == datetime(2026, 8, 12, 9, 0)


def test_unfreeze_restores_real_date():
    clock.freeze(date(2026, 8, 12))
    clock.unfreeze()
    assert clock.today() == date.today()


def test_consecutive_freezes_override():
    """Last freeze wins; freeze is a setter, not a stack."""
    clock.freeze(date(2026, 8, 12))
    clock.freeze(date(2026, 1, 1))
    assert clock.today() == date(2026, 1, 1)


def test_today_is_thread_safe_under_freeze():
    """freeze() must be atomic for the today's read in concurrent tests."""
    import threading

    clock.freeze(date(2026, 8, 12))

    results = []

    def reader():
        results.append(clock.today())

    threads = [threading.Thread(target=reader) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r == date(2026, 8, 12) for r in results)
    assert len(results) == 20