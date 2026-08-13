"""The resolved permission view for one user.

Roles are unioned: the widest sensitivity ceiling wins, while row rules from
every role are collected and later ANDed — a user who is scoped to East China
by one role does not escape that scope by holding a second role.
"""

from dataclasses import dataclass, field as dataclass_field
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.security.orm import RoleRow, UserRow
from app.semantic.enums import Sensitivity
from app.semantic.model import FieldDef

_SENSITIVITY_ORDER = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.INTERNAL: 1,
    Sensitivity.SENSITIVE: 2,
}


class PrincipalNotFoundError(Exception):
    """Raised when no active user matches. Carries no metadata by design."""


class ColumnAccess(str, Enum):
    ALLOW = "allow"
    MASK = "mask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class RowRule:
    dataset_name: str
    field_name: str
    operator: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: int
    username: str
    role_names: tuple[str, ...] = ()
    max_sensitivity: Sensitivity = Sensitivity.PUBLIC
    row_rules: tuple[RowRule, ...] = ()
    # (dataset_name, field_name) -> explicit decision
    column_overrides: dict[tuple[str, str], ColumnAccess] = dataclass_field(default_factory=dict)

    def row_rules_for(self, dataset_name: str) -> tuple[RowRule, ...]:
        return tuple(rule for rule in self.row_rules if rule.dataset_name == dataset_name)

    def column_access(self, field: FieldDef, dataset_name: str) -> ColumnAccess:
        """Explicit policy first, sensitivity ceiling second.

        ``dataset_name`` is required: an override scoped to one dataset must
        not bleed across datasets with a same-named field, or a user could
        read columns they were never granted.
        """
        override = self.column_overrides.get((dataset_name, field.name))
        if override is not None:
            return override

        if _SENSITIVITY_ORDER[field.sensitivity] <= _SENSITIVITY_ORDER[self.max_sensitivity]:
            return ColumnAccess.ALLOW
        return ColumnAccess.MASK


def _widest(values: list[Sensitivity]) -> Sensitivity:
    return max(values, key=lambda item: _SENSITIVITY_ORDER[item], default=Sensitivity.PUBLIC)


def load_principal(session: Session, user_id: int) -> Principal:
    """Resolve the permission view for a user by `user_id`.

    `user_id` is the only authorization key; callers obtain it from a
    `PrincipalContext` (after OIDC verification) or from a database
    identifier. Username is a display field only.
    """
    statement = (
        select(UserRow)
        .where(UserRow.id == user_id, UserRow.is_active.is_(True))
        .options(
            selectinload(UserRow.roles).selectinload(RoleRow.row_policies),
            selectinload(UserRow.roles).selectinload(RoleRow.column_policies),
        )
    )
    user = session.execute(statement).scalar_one_or_none()
    if user is None:
        raise PrincipalNotFoundError("用户不存在或已停用")

    rules: list[RowRule] = []
    overrides: dict[tuple[str, str], ColumnAccess] = {}
    for role in user.roles:
        for policy in role.row_policies:
            rules.append(
                RowRule(
                    dataset_name=policy.dataset_name,
                    field_name=policy.field_name,
                    operator=policy.operator,
                    values=tuple(policy.values),
                )
            )
        for policy in role.column_policies:
            key = (policy.dataset_name, policy.field_name)
            access = ColumnAccess(policy.access)
            existing = overrides.get(key)
            # DENY is not escapable by holding another role.
            if existing is None or access == ColumnAccess.DENY:
                overrides[key] = access

    return Principal(
        user_id=user.id,
        username=user.username,
        role_names=tuple(role.name for role in user.roles),
        max_sensitivity=_widest([Sensitivity(role.max_sensitivity) for role in user.roles]),
        row_rules=tuple(rules),
        column_overrides=overrides,
    )