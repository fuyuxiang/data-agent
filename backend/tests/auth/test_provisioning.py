"""JIT user provisioning tests (S1 Task 1, Step 3).

Plan: the first time a verified OIDC subject arrives, a `UserRow` is created
and bound to that subject. Subsequent logins with the same subject must not
create duplicates; if the IdP updates the display name it must be reflected
without re-keying. The unique constraint on `oidc_subject` is the last line of
defence against a programmer error that would otherwise bind two humans to
the same row.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.provisioning import provision_user
from app.security.orm import UserRow


# --- claims factory ---------------------------------------------------------


def _claims(sub: str, **overrides) -> dict:
    """Build a minimal OIDC claim set; tests pass the overrides they care about."""
    base = {
        "sub": sub,
        "preferred_username": sub,
        "name": sub,
        "tenant_id": "default",
    }
    base.update(overrides)
    return base


# --- first login -------------------------------------------------------------


def test_first_login_creates_user_row_with_oidc_subject(meta_session: Session) -> None:
    user = provision_user(meta_session, _claims("user-001"))

    meta_session.flush()
    assert user.id is not None
    assert user.oidc_subject == "user-001"
    assert user.username == "user-001"
    assert user.display_name == "user-001"
    assert user.tenant_id == "default"
    assert user.is_active is True

    persisted = meta_session.execute(
        select(UserRow).where(UserRow.oidc_subject == "user-001")
    ).scalar_one()
    assert persisted.id == user.id


# --- idempotency -------------------------------------------------------------


def test_same_subject_does_not_create_a_second_row(meta_session: Session) -> None:
    first = provision_user(meta_session, _claims("user-002"))
    meta_session.flush()
    first_id = first.id

    second = provision_user(meta_session, _claims("user-002"))
    meta_session.flush()

    assert second.id == first_id
    assert meta_session.execute(
        select(UserRow).where(UserRow.oidc_subject == "user-002")
    ).scalars().all().__len__() == 1


def test_display_name_update_is_applied_on_relogin(meta_session: Session) -> None:
    user = provision_user(meta_session, _claims("user-003", name="Original"))
    meta_session.flush()
    assert user.display_name == "Original"

    updated = provision_user(meta_session, _claims("user-003", name="Renamed"))
    meta_session.flush()

    assert updated.id == user.id
    assert updated.display_name == "Renamed"


def test_display_name_unchanged_does_not_touch_other_columns(meta_session: Session) -> None:
    user = provision_user(
        meta_session,
        _claims("user-004", preferred_username="alice", name="Alice"),
    )
    meta_session.flush()

    again = provision_user(
        meta_session,
        _claims("user-004", preferred_username="alice", name="Alice"),
    )
    meta_session.flush()

    assert again.id == user.id
    assert again.display_name == "Alice"


# --- multi-user isolation ---------------------------------------------------


def test_two_different_subjects_get_two_distinct_rows(meta_session: Session) -> None:
    a = provision_user(meta_session, _claims("user-a"))
    b = provision_user(meta_session, _claims("user-b"))
    meta_session.flush()

    assert a.id != b.id
    assert a.oidc_subject == "user-a"
    assert b.oidc_subject == "user-b"


# --- unique-constraint safety net ------------------------------------------


def test_oidc_subject_unique_constraint_is_enforced(meta_session: Session) -> None:
    """Programmer-error safety net: even if `provision_user` were bypassed, the
    DB still rejects two rows claiming the same `oidc_subject`."""
    provision_user(meta_session, _claims("dup-user"))
    meta_session.flush()

    meta_session.add(
        UserRow(
            username="dup-user-other",
            oidc_subject="dup-user",
            display_name="Bypass attempt",
        )
    )
    with pytest.raises(IntegrityError):
        meta_session.flush()


# --- tenant propagation -----------------------------------------------------


def test_tenant_id_is_taken_from_claims(meta_session: Session) -> None:
    user = provision_user(
        meta_session, _claims("tenant-user", tenant_id="acme-corp")
    )
    meta_session.flush()

    assert user.tenant_id == "acme-corp"