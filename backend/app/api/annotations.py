"""
智能标注 API。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.api.datasets import verify_workspace_access
from app.core.database import get_db
from app.models.models import User
from app.schemas.schemas import (
    AnnotationAnnotationsUpdateRequest,
    AnnotationCursorUpdateRequest,
    AnnotationOperationResponse,
    AnnotationSessionRequest,
)
from app.services.annotation_service import annotation_service
from app.services.annotation_tasks import get_annotation_task_manager

router = APIRouter()


@router.get("/meta")
async def get_annotation_meta(
    current_user: User = Depends(get_current_user),
):
    return annotation_service.get_meta()


@router.get("/restore")
async def restore_annotation_session(
    workspace_id: int,
    media_type: str,
    source_dir: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await verify_workspace_access(workspace_id, current_user, db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该工作空间")
    session = annotation_service.restore_session(workspace_id, media_type, source_dir)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到可恢复的标注状态")
    return session


@router.post("/scan")
async def create_annotation_session(
    data: AnnotationSessionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await verify_workspace_access(data.workspace_id, current_user, db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该工作空间")
    try:
        session, restored = annotation_service.prepare_session(
            workspace_id=data.workspace_id,
            media_type=data.media_type,
            source_dir=data.source_dir,
            output_dir=data.output_dir,
            use_tracking=data.use_tracking,
            frame_interval=data.frame_interval,
            detect_size=data.detect_size,
            force_reprocess=data.force_reprocess,
        )
        if not restored and session.get("status") in {"pending", "processing"}:
            await get_annotation_task_manager().enqueue(session["id"])
        session["restored"] = restored
        return session
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/sessions/{session_id}")
async def get_annotation_session(
    session_id: str,
    workspace_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await verify_workspace_access(workspace_id, current_user, db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该工作空间")
    try:
        session = annotation_service.get_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if int(session.get("workspace_id", -1)) != workspace_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="会话与工作空间不匹配")
    return session


@router.patch("/sessions/{session_id}/cursor")
async def update_annotation_cursor(
    session_id: str,
    data: AnnotationCursorUpdateRequest,
    workspace_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await verify_workspace_access(workspace_id, current_user, db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该工作空间")
    try:
        session = annotation_service.update_cursor(session_id, data.current_index)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if int(session.get("workspace_id", -1)) != workspace_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="会话与工作空间不匹配")
    return session


@router.patch("/sessions/{session_id}/items/{item_id}/annotations")
async def update_annotation_item_annotations(
    session_id: str,
    item_id: str,
    data: AnnotationAnnotationsUpdateRequest,
    workspace_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await verify_workspace_access(workspace_id, current_user, db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该工作空间")
    try:
        session = annotation_service.get_session(session_id)
        if int(session.get("workspace_id", -1)) != workspace_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="会话与工作空间不匹配")
        return annotation_service.update_annotations(session_id, item_id, data.annotations)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/items/{item_id}/save", response_model=AnnotationOperationResponse)
async def save_annotation_item(
    session_id: str,
    item_id: str,
    workspace_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await verify_workspace_access(workspace_id, current_user, db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该工作空间")
    try:
        session = annotation_service.get_session(session_id)
        if int(session.get("workspace_id", -1)) != workspace_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="会话与工作空间不匹配")
        return annotation_service.export_item(session_id, item_id)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/export", response_model=AnnotationOperationResponse)
async def export_annotation_session(
    session_id: str,
    workspace_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await verify_workspace_access(workspace_id, current_user, db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该工作空间")
    try:
        session = annotation_service.get_session(session_id)
        if int(session.get("workspace_id", -1)) != workspace_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="会话与工作空间不匹配")
        return annotation_service.export_session(session_id)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/items/{item_id}/file")
async def get_annotation_item_file(
    session_id: str,
    item_id: str,
    workspace_id: int = Query(...),
    kind: str = Query("source"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await verify_workspace_access(workspace_id, current_user, db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该工作空间")
    try:
        session = annotation_service.get_session(session_id)
        if int(session.get("workspace_id", -1)) != workspace_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="会话与工作空间不匹配")
        path, filename = annotation_service.get_item_file_path(session_id, item_id, kind)
        return FileResponse(path=path, filename=filename)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
