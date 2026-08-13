"""Idempotent role seeding.

The six roles are referenced by name across the runtime — `require_roles`
depends on them existing, the audit trace gates them by name, and the
semantic management UI lists them. Tests construct their own roles for
isolation, so this seed never overwrites an existing row; it only adds
missing ones.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.semantic.enums import Sensitivity
from app.security.orm import RoleRow


# (name, business_name, max_sensitivity)
# max_sensitivity stays at PUBLIC for the seed: per-column grants are added
# later via column_policies when an actual dataset is wired up. Adjusting
# these values changes the global ceiling, which is too blunt for a seed.
ROLE_SEED: tuple[tuple[str, str, str], ...] = (
    ("semantic_viewer", "语义查看者", Sensitivity.PUBLIC.value),
    ("semantic_editor", "语义编辑者", Sensitivity.PUBLIC.value),
    ("semantic_approver", "语义审批者", Sensitivity.PUBLIC.value),
    ("security_admin", "安全管理员", Sensitivity.PUBLIC.value),
    ("trace_auditor", "追踪审计员", Sensitivity.PUBLIC.value),
    ("eval_operator", "评测运营", Sensitivity.PUBLIC.value),
)


def seed_roles(session: Session) -> None:
    """Insert the six platform roles when missing.

    Idempotent: rows that already exist keep their current state. The
    session is flushed so the caller can read the rows immediately.
    """
    for name, business_name, max_sensitivity in ROLE_SEED:
        existing = session.execute(
            select(RoleRow).where(RoleRow.name == name)
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            RoleRow(
                name=name,
                business_name=business_name,
                max_sensitivity=max_sensitivity,
            )
        )
    session.flush()