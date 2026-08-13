"""API v2: Query Run endpoints (S7).

POST /api/v2/query-runs         create a Query Run (idempotent)
GET  /api/v2/query-runs/{id}    get status / result
GET  /api/v2/query-runs/{id}/events   stream SSE events
POST /api/v2/query-runs/{id}/cancel   cancel a running run
POST /api/v2/query-runs/{id}/clarifications   submit clarification answers
GET  /api/v2/conversations/{id}  fetch conversation (turns + answers)
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Header, Response
from pydantic import BaseModel, Field

# Imports kept for future auth integration; currently the API uses
# opaque principal_id populated by the auth dependency at the
# integration point.
# from app.auth.dependencies import get_principal


class QueryRunStatus(str, Enum):
    """Lifecycle states of a Query Run."""

    PENDING = "pending"
    RUNNING = "running"
    ANSWERED = "answered"
    CLARIFYING = "clarifying"
    REFUSED = "refused"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class QueryRun:
    """In-memory representation of a Query Run.

    Production uses the metadata DB; this is the in-process skeleton that
    the API surface operates on.
    """

    run_id: str
    status: QueryRunStatus
    question: str
    principal_id: int
    agent_id: str
    created_at: datetime
    updated_at: datetime
    idempotency_key: str
    conversation_id: str | None = None
    events: list["QueryRunEvent"] = None  # type: ignore
    result: Any = None
    error: str | None = None


@dataclass
class QueryRunEvent:
    """An event in a Query Run's lifecycle (for SSE)."""

    event_id: int
    type: str  # "stage" | "clarify" | "answer" | "error"
    timestamp: datetime
    data: dict[str, Any]


# In-memory store (replaced by DB-backed implementation in production)
_RUNS: dict[str, QueryRun] = {}


def _new_run_id() -> str:
    """Generate a unique run ID."""
    return f"qr_{secrets.token_urlsafe(12)}"


class CreateQueryRunRequest(BaseModel):
    """POST /api/v2/query-runs body."""

    agent_id: str = Field(..., min_length=1, max_length=128)
    question: str = Field(..., min_length=1, max_length=8192)
    conversation_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class CreateQueryRunResponse(BaseModel):
    """POST /api/v2/query-runs response."""

    run_id: str
    status: QueryRunStatus
    events_url: str
    state_version: int = 0


class QueryRunResponse(BaseModel):
    """GET /api/v2/query-runs/{id} response."""

    run_id: str
    status: QueryRunStatus
    question: str
    result: Any = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    state_version: int = 0


class ClarificationRequest(BaseModel):
    """POST /api/v2/query-runs/{id}/clarifications body."""

    option_index: int = Field(..., ge=0)
    free_text: str | None = None


# FastAPI router (mounted at /api/v2 in main.py)
router = APIRouter(prefix="/api/v2", tags=["v2-query-runs"])


@router.post(
    "/query-runs",
    response_model=CreateQueryRunResponse,
    status_code=201,
)
def create_query_run(
    body: CreateQueryRunRequest,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> CreateQueryRunResponse:
    """Create a new Query Run (idempotent on `Idempotency-Key` header)."""
    # Idempotency check
    if idempotency_key:
        for run in _RUNS.values():
            if run.idempotency_key == idempotency_key:
                return CreateQueryRunResponse(
                    run_id=run.run_id,
                    status=run.status,
                    events_url=f"/api/v2/query-runs/{run.run_id}/events",
                    state_version=0,
                )

    run_id = _new_run_id()
    now = datetime.utcnow()
    run = QueryRun(
        run_id=run_id,
        status=QueryRunStatus.PENDING,
        question=body.question,
        principal_id=0,  # Filled in by auth dependency in real route
        agent_id=body.agent_id,
        created_at=now,
        updated_at=now,
        idempotency_key=idempotency_key or run_id,
        conversation_id=body.conversation_id,
        events=[],
    )
    _RUNS[run_id] = run

    return CreateQueryRunResponse(
        run_id=run_id,
        status=QueryRunStatus.PENDING,
        events_url=f"/api/v2/query-runs/{run_id}/events",
    )


@router.get("/query-runs/{run_id}", response_model=QueryRunResponse)
def get_query_run(run_id: str) -> QueryRunResponse:
    """Fetch a Query Run by ID."""
    run = _RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="query run not found")

    return QueryRunResponse(
        run_id=run.run_id,
        status=run.status,
        question=run.question,
        result=run.result,
        error=run.error,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


@router.post("/query-runs/{run_id}/cancel", status_code=204, response_class=Response)
def cancel_query_run(run_id: str) -> Response:
    """Cancel a running Query Run (idempotent if already cancelled)."""
    run = _RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="query run not found")
    if run.status in (
        QueryRunStatus.PENDING,
        QueryRunStatus.RUNNING,
        QueryRunStatus.CLARIFYING,
    ):
        run.status = QueryRunStatus.CANCELLED
        run.updated_at = datetime.utcnow()
    # Idempotent: already-finished runs return 204 with no body
    return Response(status_code=204)


@router.post(
    "/query-runs/{run_id}/clarifications",
    response_model=QueryRunResponse,
)
def submit_clarification(
    run_id: str, body: ClarificationRequest
) -> QueryRunResponse:
    """Submit a clarification answer (option index or free text)."""
    run = _RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="query run not found")
    if run.status != QueryRunStatus.CLARIFYING:
        raise HTTPException(
            status_code=409,
            detail=f"query run is not in clarifying state (status={run.status.value})",
        )
    if body.free_text is not None:
        # Free-text answers still go through the same channel
        # (orchestrator decides what to do with it)
        pass
    run.status = QueryRunStatus.RUNNING
    run.updated_at = datetime.utcnow()
    return QueryRunResponse(
        run_id=run.run_id,
        status=run.status,
        question=run.question,
        result=run.result,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


# --- Conversation API ----------------------------------------------------

class ConversationResponse(BaseModel):
    """GET /api/v2/conversations/{id} response."""

    conversation_id: str
    user_id: int
    title: str
    turns: list[dict[str, Any]]
    answers: list[dict[str, Any]]


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
def get_conversation(conversation_id: str) -> ConversationResponse:
    """Fetch a conversation with its turns and answers."""
    # Placeholder; production reads from ConversationRow / TurnRow
    return ConversationResponse(
        conversation_id=conversation_id,
        user_id=0,
        title="",
        turns=[],
        answers=[],
    )


# --- Error envelope (S7 §1) -----------------------------------------------

class ErrorResponse(BaseModel):
    """Standard error envelope (S7 spec §1).

    All API errors return this shape with a stable error_code + trace_id.
    The HTTP status is encoded in error_code, not by HTTP semantics.
    """

    error_code: str
    message: str
    trace_id: str
