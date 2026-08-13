"""Timezone resolution from PrincipalContext (S2 Task 1, Step 2).

Users may work in different timezones. The principal carries a timezone
attribute; we resolve it to a ZoneInfo object. Default is Asia/Shanghai.
"""

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.auth.principal import PrincipalContext
from app.temporal.timezone import resolve_timezone


def test_default_timezone_is_asia_shanghai():
    """When principal has no timezone or empty, default to Asia/Shanghai."""
    principal = PrincipalContext(
        user_id=1,
        tenant_id="default",
        subject="user@example.com",
        username="alice",
        display_name="Alice",
        roles=frozenset(),
        groups=frozenset(),
        attributes={},
        auth_time=0,
    )

    tz = resolve_timezone(principal)

    assert tz == ZoneInfo("Asia/Shanghai")


def test_explicit_timezone_from_attributes():
    """When principal.attributes['timezone'] is set, use that."""
    principal = PrincipalContext(
        user_id=2,
        tenant_id="default",
        subject="user@example.com",
        username="bob",
        display_name="Bob",
        roles=frozenset(),
        groups=frozenset(),
        attributes={"timezone": "America/New_York"},
        auth_time=0,
    )

    tz = resolve_timezone(principal)

    assert tz == ZoneInfo("America/New_York")


def test_timezone_conversion_sydney_to_utc():
    """A datetime in Sydney timezone is correctly converted to UTC."""
    principal = PrincipalContext(
        user_id=3,
        tenant_id="default",
        subject="user@example.com",
        username="charlie",
        display_name="Charlie",
        roles=frozenset(),
        groups=frozenset(),
        attributes={"timezone": "Australia/Sydney"},
        auth_time=0,
    )

    tz = resolve_timezone(principal)

    # 2026-08-13 10:00:00 in Sydney is 2026-08-13 02:00:00 in UTC (AEST is UTC+10 or UTC+11)
    sydney_time = datetime(2026, 8, 13, 10, 0, 0, tzinfo=tz)
    utc_time = sydney_time.astimezone(timezone.utc)

    # Sydney in August is AEST (UTC+10)
    assert utc_time == datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc)


def test_timezone_object_has_correct_name():
    """The returned ZoneInfo object's key matches the requested timezone."""
    principal = PrincipalContext(
        user_id=4,
        tenant_id="default",
        subject="user@example.com",
        username="diana",
        display_name="Diana",
        roles=frozenset(),
        groups=frozenset(),
        attributes={"timezone": "Europe/London"},
        auth_time=0,
    )

    tz = resolve_timezone(principal)

    assert str(tz) == "Europe/London"


def test_invalid_timezone_raises_error():
    """Invalid timezone string raises an error."""
    principal = PrincipalContext(
        user_id=5,
        tenant_id="default",
        subject="user@example.com",
        username="eve",
        display_name="Eve",
        roles=frozenset(),
        groups=frozenset(),
        attributes={"timezone": "Invalid/Zone"},
        auth_time=0,
    )

    with pytest.raises(Exception):  # ZoneInfo raises ZoneInfoNotFoundError
        resolve_timezone(principal)


def test_empty_timezone_attribute_uses_default():
    """When timezone attribute is empty string, use default."""
    principal = PrincipalContext(
        user_id=6,
        tenant_id="default",
        subject="user@example.com",
        username="frank",
        display_name="Frank",
        roles=frozenset(),
        groups=frozenset(),
        attributes={"timezone": ""},
        auth_time=0,
    )

    tz = resolve_timezone(principal)

    assert tz == ZoneInfo("Asia/Shanghai")
