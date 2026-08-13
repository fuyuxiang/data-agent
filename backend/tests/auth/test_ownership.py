"""Object-level ownership tests (S1 Task 3, Step 1).

A ConversationRow belongs to one user and one dataset. The orchestrator must
never cross those boundaries: another user's id, a non-existent id, or even
your own conversation pointed at the wrong dataset must all collapse into
the same 404. As a side effect of this rule, a 404 must never produce a
Turn row — leaking a write into the metadata DB would tell an attacker
that they guessed a valid id.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.ownership import ConversationNotVisibleError, owned_conversation
from app.auth.principal import PrincipalContext
from app.observability.orm import ConversationRow, TurnRow
from app.security.orm import UserRow
from tests.security.factories import build_principals


@pytest.fixture
def env(meta_session: Session) -> Session:
    build_principals(meta_session)
    return meta_session


def _principal(session: Session, username: str) -> PrincipalContext:
    user = session.execute(
        select(UserRow).where(UserRow.username == username)
    ).scalar_one()
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


def _seed_conversation(session: Session, *, user_id: int, dataset_name: str) -> ConversationRow:
    row = ConversationRow(
        user_id=user_id,
        title="会话",
        dataset_name=dataset_name,
        slot_state={},
    )
    session.add(row)
    session.flush()
    return row


def test_owner_can_load_their_own_conversation(env: Session) -> None:
    alice = _principal(env, "admin")
    conv = _seed_conversation(env, user_id=alice.user_id, dataset_name="orders")

    row = owned_conversation(env, alice, conv.id, dataset_name="orders")

    assert row.id == conv.id


def test_other_users_conversation_is_invisible(env: Session) -> None:
    alice = _principal(env, "admin")
    mallory = _principal(env, "east_manager")
    conv = _seed_conversation(env, user_id=alice.user_id, dataset_name="orders")

    with pytest.raises(ConversationNotVisibleError):
        owned_conversation(env, mallory, conv.id, dataset_name="orders")

    # Side effect rule: a 404 must not create a Turn row.
    turns = env.execute(select(func.count()).select_from(TurnRow)).scalar_one()
    assert turns == 0


def test_unknown_conversation_id_is_invisible(env: Session) -> None:
    alice = _principal(env, "admin")

    with pytest.raises(ConversationNotVisibleError):
        owned_conversation(env, alice, 999_999, dataset_name="orders")


def test_cross_dataset_conversation_is_invisible(env: Session) -> None:
    """An id that *is* yours but belongs to a different dataset must not
    silently inherit its slot_state — that is exactly the leak we want to
    prevent when a follow-up question drifts into a new domain."""
    alice = _principal(env, "admin")
    conv = _seed_conversation(env, user_id=alice.user_id, dataset_name="refunds")

    with pytest.raises(ConversationNotVisibleError):
        owned_conversation(env, alice, conv.id, dataset_name="orders")