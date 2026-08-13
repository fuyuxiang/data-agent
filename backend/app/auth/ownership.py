"""Object-level ownership checks.

A ConversationRow is a private workspace: one user, one dataset. Crossing
either boundary must look the same as the id never having existed —
otherwise an attacker can probe which ids are valid by comparing the 404
they get from a real id against the 404 they get from a random one.

The function intentionally raises a single exception type with no detail
about *why* the lookup failed. Mapping that to HTTP 404 is the caller's job.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth.principal import PrincipalContext
from app.observability.orm import ConversationRow


class ConversationNotVisibleError(Exception):
    """The conversation does not exist for this principal — for any reason.

    The exception carries no detail so the API layer cannot accidentally
    leak which axis (existence, ownership, dataset mismatch) triggered
    the rejection.
    """


def owned_conversation(
    session: Session,
    principal: PrincipalContext,
    conversation_id: int,
    *,
    dataset_name: str,
) -> ConversationRow:
    """Return the conversation if it belongs to the principal and dataset.

    Raises `ConversationNotVisibleError` when any of the following holds:
    - the id does not exist;
    - it belongs to a different user;
    - it belongs to the principal but to a different dataset (this prevents
      accidentally inheriting slot_state from an unrelated conversation when
      a follow-up drifts into a new domain).
    """
    row = session.get(ConversationRow, conversation_id)
    if (
        row is None
        or row.user_id != principal.user_id
        or row.dataset_name != dataset_name
    ):
        raise ConversationNotVisibleError(conversation_id)
    return row