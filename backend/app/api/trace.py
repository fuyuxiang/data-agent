from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import get_meta_session, get_sample_connection
from app.core.security import get_current_username
from app.observability.schemas import ReplayOut, TraceOut
from app.observability.service import (
    NotFoundError,
    NotReplayableError,
    get_trace,
    replay_turn,
)

router = APIRouter(prefix="/api/trace", tags=["trace"])


@router.get("/turns/{turn_id}", response_model=TraceOut)
def get_turn_trace(
    turn_id: int,
    username: str = Depends(get_current_username),
    session: Session = Depends(get_meta_session),
):
    try:
        return get_trace(session, username, turn_id)
    except NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")


@router.post("/turns/{turn_id}/replay", response_model=ReplayOut)
def post_turn_replay(
    turn_id: int,
    username: str = Depends(get_current_username),
    session: Session = Depends(get_meta_session),
    connection: Connection = Depends(get_sample_connection),
    settings: Settings = Depends(get_settings),
):
    try:
        return replay_turn(
            session, username, turn_id, connection=connection, settings=settings
        )
    except NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    except NotReplayableError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="该轮没有可重放的意图快照"
        )