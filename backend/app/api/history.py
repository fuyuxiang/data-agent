"""
查询历史 API
"""
from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.core.database import get_db
from app.api.auth import get_current_user
from app.models.models import User, QueryHistory
from app.schemas.schemas import QueryHistoryResponse

router = APIRouter()


@router.get("", response_model=List[QueryHistoryResponse])
async def list_query_history(
    workspace_id: int,
    dataset_id: int = None,
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取查询历史列表"""
    query = (
        select(QueryHistory)
        .where(QueryHistory.workspace_id == workspace_id)
        .where(QueryHistory.user_id == current_user.id)
    )

    if dataset_id:
        query = query.where(QueryHistory.dataset_id == dataset_id)

    query = query.order_by(desc(QueryHistory.created_at)).offset(offset).limit(limit)

    result = await db.execute(query)
    histories = result.scalars().all()

    return histories


@router.get("/{history_id}", response_model=QueryHistoryResponse)
async def get_query_history(
    history_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取查询历史详情"""
    result = await db.execute(
        select(QueryHistory).where(QueryHistory.id == history_id)
    )
    history = result.scalar_one_or_none()

    if not history:
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="查询历史不存在",
        )

    return history
