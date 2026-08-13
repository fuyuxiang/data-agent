"""JIT user provisioning from verified OIDC claims.

The IdP is the source of truth for identity; the metadata DB is a local
cache that lets us hand out stable `user_id`s without an IdP round-trip on
every request. Subjects are unique — collisions are rejected by the DB, not
by us; this module's job is to look up, optionally create, and update.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.security.orm import UserRow


def provision_user(session: Session, claims: dict) -> UserRow:
    """Create or refresh the local user for a verified OIDC subject.

    The required claim keys (`sub`, `preferred_username`, `name`, `tenant_id`)
    come from the verified token, never from unverified request input. On a
    first login a row is created and flushed back to the caller; on every
    subsequent login we refresh only the columns that actually changed.
    """
    subject = claims["sub"]
    preferred_username = claims.get("preferred_username") or subject
    display_name = claims.get("name") or preferred_username
    tenant_id = claims.get("tenant_id") or "default"

    existing = session.execute(
        select(UserRow).where(UserRow.oidc_subject == subject)
    ).scalar_one_or_none()

    if existing is None:
        row = UserRow(
            username=preferred_username,
            oidc_subject=subject,
            display_name=display_name,
            tenant_id=tenant_id,
        )
        session.add(row)
        return row

    if existing.display_name != display_name:
        existing.display_name = display_name
    return existing