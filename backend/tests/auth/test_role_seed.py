"""Role seed tests (S1 Task 1, Step 6).

The platform's RBAC references six role names by string. Their existence is
a precondition for `require_roles(...)` and for any audit/approval flow
that gates on a specific role, so the seed must be deterministic and
idempotent — the same call against a freshly built DB and a populated one
must leave the role table in the same state.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.security.orm import RoleRow
from scripts.seed_roles import ROLE_SEED, seed_roles


EXPECTED_ROLE_NAMES = {
    "semantic_viewer",
    "semantic_editor",
    "semantic_approver",
    "security_admin",
    "trace_auditor",
    "eval_operator",
}


def test_seed_inserts_all_six_roles(meta_session: Session) -> None:
    seed_roles(meta_session)

    names = {
        name
        for (name,) in meta_session.execute(select(RoleRow.name)).all()
    }
    assert EXPECTED_ROLE_NAMES.issubset(names)


def test_seed_is_idempotent(meta_session: Session) -> None:
    seed_roles(meta_session)
    first_count = meta_session.execute(
        select(RoleRow).where(RoleRow.name.in_(EXPECTED_ROLE_NAMES))
    ).all()
    assert len(first_count) == len(EXPECTED_ROLE_NAMES)

    # A second call must not add duplicates or overwrite existing rows.
    seed_roles(meta_session)
    second_count = meta_session.execute(
        select(RoleRow).where(RoleRow.name.in_(EXPECTED_ROLE_NAMES))
    ).all()
    assert len(second_count) == len(EXPECTED_ROLE_NAMES)


def test_seed_roles_table_matches_documented_set() -> None:
    """`ROLE_SEED` is the source of truth: every entry must be a stable name
    that the runtime references; tests pin down drift early."""
    assert {entry[0] for entry in ROLE_SEED} == EXPECTED_ROLE_NAMES


def test_seed_assigns_human_readable_business_name(meta_session: Session) -> None:
    seed_roles(meta_session)

    rows = {
        row.name: row.business_name
        for row in meta_session.execute(select(RoleRow)).scalars()
    }
    # The semantic management UI uses these labels verbatim — non-empty.
    for name in EXPECTED_ROLE_NAMES:
        assert rows.get(name), f"role {name} has no business_name"