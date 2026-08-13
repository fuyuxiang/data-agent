"""Development-mode identity fallback.

Only mounted when `settings.auth_mode == "dev"`. Production deployments
must never expose this path — the start-up validation in S1 Task 8
explicitly rejects `auth_mode=dev` when `environment=production`.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.principal import PrincipalContext
from app.security.orm import UserRow


def dev_principal_dependency(request: Request, session: Session) -> PrincipalContext:
    """Resolve identity from the X-Username header for dev runs.

    Lazily creates a row for an unknown username so the workbench "switch
    user" feature keeps working without standing up an external IdP. Real
    code paths never see this dependency — `dependencies.get_principal`
    mounts it only when `auth_mode == "dev"`.
    """
    username = request.headers.get("X-Username", "").strip()
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少身份信息",
        )

    user = session.execute(
        select(UserRow).where(UserRow.username == username)
    ).scalar_one_or_none()
    if user is None:
        user = UserRow(
            username=username,
            display_name=username,
            oidc_subject=None,
        )
        session.add(user)
        session.flush()

    return PrincipalContext(
        user_id=user.id,
        tenant_id=user.tenant_id,
        subject=user.oidc_subject or "",
        username=user.username,
        display_name=user.display_name,
        roles=frozenset(role.name for role in user.roles),
        groups=frozenset(),
        attributes={},
        auth_time=0,
    )