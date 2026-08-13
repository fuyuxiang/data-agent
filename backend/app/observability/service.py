"""Read models and side effects for the chat/trace endpoints.

Ownership checks live here rather than in the routers: every lookup goes
through a caller-scoped query, so an unowned id is indistinguishable from a
missing one.

Callers pass a verified `PrincipalContext`. We never reach for username —
the only authorization key is `user_id`. When a function actually needs the
permission view (row policies, column policies, sensitivity ceiling), it
loads the `Principal` by `user_id` locally; that is a single, deterministic
fetch keyed on identity, not a username lookup that an attacker can spoof.
"""

from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.auth.principal import PrincipalContext
from app.compiler.errors import CompileError
from app.compiler.query import compile_intent
from app.core.config import Settings
from app.intent.schema import QueryIntent
from app.observability.orm import ConversationRow, FeedbackRow, TurnRow
from app.observability.schemas import (
    ConversationOut,
    FeedbackIn,
    ReplayOut,
    TraceOut,
    TraceStageOut,
    TurnOut,
)
from app.observability.trace import Stage, load_trace
from app.security.columns import visible_dataset
from app.security.pipeline import secure_compiled
from app.security.principal import load_principal
from app.semantic.loader import load_dataset
from app.semantic.model import SemanticError


class NotFoundError(Exception):
    """Missing or not owned by the caller — the endpoint answers 404 either way."""


class NotReplayableError(Exception):
    """No intent snapshot: clarifying, refused and failed turns cannot replay."""


def list_conversations(
    session: Session, principal: PrincipalContext
) -> list[ConversationOut]:
    statement = (
        select(ConversationRow)
        .where(ConversationRow.user_id == principal.user_id)
        .order_by(ConversationRow.updated_at.desc())
    )
    return [
        ConversationOut.model_validate(row)
        for row in session.execute(statement).scalars()
    ]


def list_turns(
    session: Session, principal: PrincipalContext, conversation_id: int
) -> list[TurnOut]:
    _owned_conversation(session, principal, conversation_id)
    statement = (
        select(TurnRow)
        .where(TurnRow.conversation_id == conversation_id)
        .order_by(TurnRow.id)
    )
    return [TurnOut.model_validate(row) for row in session.execute(statement).scalars()]


def save_feedback(
    session: Session,
    principal: PrincipalContext,
    turn_id: int,
    payload: FeedbackIn,
) -> None:
    _owned_turn(session, principal, turn_id)
    session.add(
        FeedbackRow(
            turn_id=turn_id,
            is_positive=payload.is_positive,
            category=payload.category,
            comment=payload.comment,
        )
    )
    session.flush()


def get_trace(
    session: Session, principal: PrincipalContext, turn_id: int
) -> TraceOut:
    turn = _owned_turn(session, principal, turn_id)
    return TraceOut(
        turn_id=turn.id,
        question=turn.question,
        status=turn.status,
        intent_snapshot=turn.intent_snapshot,
        stages=[TraceStageOut.model_validate(row) for row in load_trace(session, turn_id)],
    )


def replay_turn(
    session: Session,
    principal: PrincipalContext,
    turn_id: int,
    *,
    connection: Connection,
    settings: Settings,
) -> ReplayOut:
    """Recompile from the stored intent, no model call. Security rewriting runs
    again against current permissions, so replay can never widen access."""
    turn = _owned_turn(session, principal, turn_id)
    if not turn.intent_snapshot:
        raise NotReplayableError

    intent = QueryIntent.from_payload(turn.intent_snapshot)
    if intent.kind.value == "unsupported":
        raise NotReplayableError

    conversation = session.get(ConversationRow, turn.conversation_id)
    # The permission view is needed for visible_dataset / secure_compiled; load
    # it by `user_id` once per call rather than every nested step.
    principal_obj = load_principal(session, principal.user_id)
    dataset = visible_dataset(load_dataset(session, conversation.dataset_name), principal_obj)

    try:
        compiled = compile_intent(dataset, intent)
        secured = secure_compiled(compiled, dataset, principal_obj, connection, settings)
    except (CompileError, SemanticError):
        # The snapshot is not a queryable intent (refused, failed or stale
        # schema): treat replay as not available, not as a crash.
        raise NotReplayableError

    return ReplayOut(
        sql=secured.sql,
        display_sql=secured.display_sql,
        matches_original=secured.sql == _original_sql(session, turn_id),
        applied_row_filters=[
            item.field_business_name for item in secured.applied_row_filters
        ],
        masked_field_names=list(secured.masked_field_names),
    )


def _original_sql(session: Session, turn_id: int) -> str:
    for row in load_trace(session, turn_id):
        if row.stage == Stage.SECURITY.value and row.output_payload:
            return row.output_payload.get("sql", "")
    return ""


def _owned_conversation(
    session: Session, principal: PrincipalContext, conversation_id: int
) -> ConversationRow:
    row = session.get(ConversationRow, conversation_id)
    if row is None or row.user_id != principal.user_id:
        raise NotFoundError
    return row


def _owned_turn(
    session: Session, principal: PrincipalContext, turn_id: int
) -> TurnRow:
    turn = session.get(TurnRow, turn_id)
    if turn is None:
        raise NotFoundError
    _owned_conversation(session, principal, turn.conversation_id)
    return turn