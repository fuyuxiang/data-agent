"""Principals used across the security tests.

- admin: unrestricted
- east_manager: rows scoped to EC, cost column explicitly denied
- analyst: PUBLIC ceiling only, so sensitive columns get masked
- multi_role: analyst + east_sales, to test role union
- retired_user: inactive
"""

from sqlalchemy.orm import Session

from app.security.orm import ColumnPolicyRow, RoleRow, RowPolicyRow, UserRow
from app.semantic.enums import Sensitivity


def build_principals(session: Session) -> dict[str, UserRow]:
    admin_role = RoleRow(
        name="admin",
        business_name="管理员",
        max_sensitivity=Sensitivity.SENSITIVE.value,
    )
    east_role = RoleRow(
        name="east_sales",
        business_name="华东销售",
        max_sensitivity=Sensitivity.SENSITIVE.value,
        row_policies=[
            RowPolicyRow(
                dataset_name="orders", field_name="region_code", operator="in", values=["EC"]
            )
        ],
        column_policies=[
            ColumnPolicyRow(dataset_name="orders", field_name="cost", access="deny")
        ],
    )
    analyst_role = RoleRow(
        name="analyst",
        business_name="分析师",
        max_sensitivity=Sensitivity.PUBLIC.value,
        row_policies=[
            RowPolicyRow(
                dataset_name="orders", field_name="channel", operator="in", values=["online"]
            )
        ],
    )

    users = {
        "admin": UserRow(username="admin", display_name="管理员", roles=[admin_role]),
        "east_manager": UserRow(
            username="east_manager", display_name="华东负责人", roles=[east_role]
        ),
        "analyst": UserRow(username="analyst", display_name="分析师", roles=[analyst_role]),
        "multi_role": UserRow(
            username="multi_role", display_name="双角色", roles=[analyst_role, east_role]
        ),
        "retired_user": UserRow(
            username="retired_user", display_name="已离职", is_active=False, roles=[analyst_role]
        ),
    }

    session.add_all(users.values())
    session.flush()
    return users