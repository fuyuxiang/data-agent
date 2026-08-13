"""Read models and side effects for the chat/trace endpoints.

Ownership checks live here rather than in the routers: every lookup goes
through a caller-scoped query, so an unowned id is indistinguishable from a
missing one.
"""

from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

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


def list_conversations(session: Session, username: str) -> list[ConversationOut]:
    principal = load_principal(session, username)
    statement = (
        select(ConversationRow)
        .where(ConversationRow.user_id == principal.user_id)
        .order_by(ConversationRow.updated_at.desc())
    )
    return [
        ConversationOut.model_validate(row)
        for row in session.execute(statement).scalars()
    ]


def list_turns(session: Session, username: str, conversation_id: int) -> list[TurnOut]:
    _owned_conversation(session, username, conversation_id)
    statement = (
        select(TurnRow)
        .where(TurnRow.conversation_id == conversation_id)
        .order_by(TurnRow.id)
    )
    return [TurnOut.model_validate(row) for row in session.execute(statement).scalars()]


def save_feedback(session: Session, username: str, turn_id: int, payload: FeedbackIn) -> None:
    _owned_turn(session, username, turn_id)
    session.add(
        FeedbackRow(
            turn_id=turn_id,
            is_positive=payload.is_positive,
            category=payload.category,
            comment=payload.comment,
        )
    )
    session.flush()


def get_trace(session: Session, username: str, turn_id: int) -> TraceOut:
    turn = _owned_turn(session, username, turn_id)
    return TraceOut(
        turn_id=turn.id,
        question=turn.question,
        status=turn.status,
        intent_snapshot=turn.intent_snapshot,
        stages=[TraceStageOut.model_validate(row) for row in load_trace(session, turn_id)],
    )


def replay_turn(
    session: Session,
    username: str,
    turn_id: int,
    *,
    connection: Connection,
    settings: Settings,
) -> ReplayOut:
    """Recompile from the stored intent, no model call. Security rewriting runs
    again against current permissions, so replay can never widen access."""
    turn = _owned_turn(session, username, turn_id)
    if not turn.intent_snapshot:
        raise NotReplayableError

    intent = QueryIntent.from_payload(turn.intent_snapshot)
    if intent.kind.value == "unsupported":
        raise NotReplayableError

    conversation = session.get(ConversationRow, turn.conversation_id)
    principal = load_principal(session, username)
    dataset = visible_dataset(load_dataset(session, conversation.dataset_name), principal)

    try:
        compiled = compile_intent(dataset, intent)
        secured = secure_compiled(compiled, dataset, principal, connection, settings)
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
    session: Session, username: str, conversation_id: int
) -> ConversationRow:
    principal = load_principal(session, username)
    row = session.get(ConversationRow, conversation_id)
    if row is None or row.user_id != principal.user_id:
        raise NotFoundError
    return row


def _owned_turn(session: Session, username: str, turn_id: int) -> TurnRow:
    turn = session.get(TurnRow, turn_id)
    if turn is None:
        raise NotFoundError
    _owned_conversation(session, username, turn.conversation_id)
    return turn