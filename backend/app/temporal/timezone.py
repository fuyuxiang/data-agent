"""Timezone resolution from principal attributes (S2 Task 1, Step 2).

Users may work in different timezones. The principal carries timezone in
attributes['timezone']; we resolve it to a ZoneInfo object. Default is
Asia/Shanghai.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from app.auth.principal import PrincipalContext


def resolve_timezone(principal: PrincipalContext) -> ZoneInfo:
    """Resolve the principal's timezone to a ZoneInfo object.

    Reads from principal.attributes['timezone']; defaults to Asia/Shanghai.
    If the timezone string is empty or missing, uses the default.

    Raises ZoneInfoNotFoundError if the timezone is invalid.
    """
    tz_name = principal.attributes.get("timezone", "").strip()
    if not tz_name:
        tz_name = "Asia/Shanghai"
    return ZoneInfo(tz_name)
