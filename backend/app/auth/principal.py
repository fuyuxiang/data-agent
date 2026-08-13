"""The verified identity of the caller, post-token-verification.

`PrincipalContext` is frozen on purpose: anything downstream can cache it,
compare it, or include it in a log line without worrying that some
mid-pipeline middleware will mutate it underfoot. It carries what the IdP
delivered — display fields (`username`) are present but never used for
authorization. The sole authorization key is `user_id`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PrincipalContext:
    """Immutable verified identity for one request.

    `subject` is the OIDC `sub` claim — stable across sessions for the same
    human. `username` is purely for display (the workbench greeting) and
    must never participate in authorization. Roles and groups are joined
    from the IdP at provisioning time; `auth_time` records when the IdP
    last asserted the session.
    """

    user_id: int
    tenant_id: str
    subject: str
    username: str
    display_name: str
    roles: frozenset[str] = field(default_factory=frozenset)
    groups: frozenset[str] = field(default_factory=frozenset)
    attributes: dict[str, Any] = field(default_factory=dict)
    auth_time: int = 0

    def has_role(self, *names: str) -> bool:
        return any(role in self.roles for role in names)