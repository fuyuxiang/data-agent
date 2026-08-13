"""FastAPI dependencies that resolve the caller identity.

`get_principal` is the only entry point the rest of the API should use to
identify the caller. It caches the result on `request.state` so nested
dependencies get the same `PrincipalContext` instance for one request —
avoids duplicate token verification and lets every layer agree on who the
caller is.

`require_roles` is for *role-based* gates (publish, lint, audit). It returns
403 — "I know who you are and you may not" — distinct from the 404 emitted
by object-level checks ("I will not confirm whether this exists for you").
"""

from __future__ import annotations

from typing import Iterable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.dev import dev_principal_dependency
from app.auth.oidc import JwksClient, TokenError, verify_token
from app.auth.principal import PrincipalContext
from app.auth.provisioning import provision_user
from app.core.config import Settings, get_settings
from app.core.db import get_meta_session
from app.security.orm import UserRow


def get_principal(
    request: Request,
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_meta_session),
) -> PrincipalContext:
    """Verify the Bearer token (or dev header) and return the verified identity.

    The result is cached on `request.state.principal` so nested dependencies
    that also ask for the principal get the same instance.
    """
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached

    if settings.auth_mode == "dev":
        context = dev_principal_dependency(request, session)
    else:
        context = _resolve_oidc_principal(request, settings, session)

    request.state.principal = context
    return context


def _resolve_oidc_principal(
    request: Request,
    settings: Settings,
    session: Session,
) -> PrincipalContext:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少身份信息"
        )
    token = auth_header[len("Bearer ") :].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少身份信息"
        )

    jwks_client = JwksClient(settings=settings)
    try:
        claims = verify_token(token, settings, jwks_client=jwks_client)
    except TokenError:
        # Do not leak which step failed; the caller only needs to know that
        # the presented credential is not acceptable right now.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="身份验证失败"
        )

    user = provision_user(session, claims)
    session.flush()

    roles = frozenset(role.name for role in user.roles)
    return PrincipalContext(
        user_id=user.id,
        tenant_id=user.tenant_id,
        subject=user.oidc_subject or "",
        username=user.username,
        display_name=user.display_name,
        roles=roles,
        groups=frozenset(),
        attributes={},
        auth_time=int(claims.get("auth_time", 0) or 0),
    )


def require_roles(*names: str):
    """Build a dependency that enforces role membership.

    Returns 403 — known identity but insufficient privilege — distinct
    from the 404 emitted by object-level checks. Empty `names` means
    "any authenticated caller", which is rarely useful; callers should
    always name at least one role for clarity.
    """
    required: tuple[str, ...] = tuple(names)

    def _checker(
        principal: PrincipalContext = Depends(get_principal),
    ) -> PrincipalContext:
        if not required:
            return principal
        if not any(role in principal.roles for role in required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="权限不足"
            )
        return principal

    return _checker