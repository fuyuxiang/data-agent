from dataclasses import asdict, is_dataclass

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.auth.dependencies import get_principal
from app.auth.principal import PrincipalContext
from app.core.config import Settings, get_settings
from app.core.db import get_meta_session, get_sample_connection
from app.intent.recognizer import LlmClient, OpenAiCompatClient
from app.observability.schemas import (
    AnswerOut,
    AskIn,
    AskOut,
    ClarifyOut,
    ConversationOut,
    FeedbackIn,
    TurnOut,
)
from app.observability.service import (
    NotFoundError,
    list_conversations,
    list_turns,
    save_feedback,
)
from app.pipeline.orchestrator import QueryOrchestrator, TurnOutcome
from app.semantic.model import SemanticError

router = APIRouter(prefix="/api/chat", tags=["chat"])


def get_llm_client(settings: Settings = Depends(get_settings)) -> LlmClient:
    """Separate provider so tests can substitute a stub without patching."""
    return OpenAiCompatClient(settings)


@router.post("/ask", response_model=AskOut)
def post_ask(
    payload: AskIn,
    principal: PrincipalContext = Depends(get_principal),
    session: Session = Depends(get_meta_session),
    connection: Connection = Depends(get_sample_connection),
    client: LlmClient = Depends(get_llm_client),
    settings: Settings = Depends(get_settings),
):
    orchestrator = QueryOrchestrator(
        meta_session=session,
        sample_connection=connection,
        llm_client=client,
        settings=settings,
    )
    try:
        outcome = orchestrator.ask(
            user_id=principal.user_id,
            question=payload.question,
            dataset_name=payload.dataset_name,
            conversation_id=payload.conversation_id,
        )
    except SemanticError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="数据集不存在")
    return _to_ask_out(outcome)


def _to_ask_out(outcome: TurnOutcome) -> AskOut:
    return AskOut(
        status=outcome.status.value,
        conversation_id=outcome.conversation_id,
        turn_id=outcome.turn_id,
        answer=AnswerOut.model_validate(_plain(outcome.answer)) if outcome.answer else None,
        clarifications=[
            ClarifyOut.model_validate(_plain(item)) for item in outcome.clarifications
        ],
        refusal_reason=outcome.refusal_reason,
        slot_state=outcome.slot_state,
    )


def _plain(value):
    """Dataclasses (including nested ones) into dicts pydantic can validate."""
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if hasattr(value, "value") and hasattr(value, "name"):
        return value.value
    return value


@router.get("/conversations", response_model=list[ConversationOut])
def get_conversations(
    principal: PrincipalContext = Depends(get_principal),
    session: Session = Depends(get_meta_session),
):
    return list_conversations(session, principal)


@router.get("/conversations/{conversation_id}/turns", response_model=list[TurnOut])
def get_conversation_turns(
    conversation_id: int,
    principal: PrincipalContext = Depends(get_principal),
    session: Session = Depends(get_meta_session),
):
    try:
        return list_turns(session, principal, conversation_id)
    except NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")


@router.post("/turns/{turn_id}/feedback", status_code=status.HTTP_201_CREATED)
def post_feedback(
    turn_id: int,
    payload: FeedbackIn,
    principal: PrincipalContext = Depends(get_principal),
    session: Session = Depends(get_meta_session),
):
    try:
        save_feedback(session, principal, turn_id, payload)
    except NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    return {"ok": True}