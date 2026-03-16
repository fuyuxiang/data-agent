"""
数据集管理 API
"""
import json
from typing import List, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from app.core.database import get_db
from app.api.auth import get_current_user
from app.models.models import User, Dataset, DataSource, DatasetStatus, UserWorkspace
from app.schemas.schemas import (
    DatasetCreate,
    DatasetUpdate,
    DatasetResponse,
)
from app.services.dataset import DatasetService

router = APIRouter()


def _dump_json_models(items):
    if not items:
        return None
    return [item.model_dump() if hasattr(item, "model_dump") else item for item in items]


def _parse_form_list(raw_value) -> List[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return [str(item).strip() for item in raw_value if str(item).strip()]
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass
        return [line.strip() for line in text.splitlines() if line.strip()]
    return [str(raw_value).strip()]


async def _parse_dataset_create_request(
    request: Request,
) -> Tuple[DatasetCreate, List[UploadFile], List[UploadFile], List[UploadFile]]:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        payload = await request.json()
        return DatasetCreate.model_validate(payload), [], [], []

    form = await request.form()
    raw_payload = form.get("payload")
    payload_data = json.loads(raw_payload) if raw_payload else {}

    if "file_paths" not in payload_data:
        payload_data["file_paths"] = _parse_form_list(form.get("file_paths"))
    if "data_source_ids" in payload_data and isinstance(payload_data["data_source_ids"], str):
        payload_data["data_source_ids"] = _parse_form_list(payload_data["data_source_ids"])
    dataset_payload = DatasetCreate.model_validate(payload_data)

    def _extract_uploads(field_name: str) -> List[UploadFile]:
        return [item for item in form.getlist(field_name) if getattr(item, "filename", None)]

    return (
        dataset_payload,
        _extract_uploads("csv_files"),
        _extract_uploads("image_files"),
        _extract_uploads("video_files"),
    )


async def verify_workspace_access(
    workspace_id: int,
    current_user: User,
    db: AsyncSession,
) -> bool:
    """验证用户是否有权限访问指定工作空间"""
    result = await db.execute(
        select(UserWorkspace).where(
            and_(
                UserWorkspace.user_id == current_user.id,
                UserWorkspace.workspace_id == workspace_id,
            )
        )
    )
    return result.scalar_one_or_none() is not None


@router.post("", response_model=DatasetResponse)
async def create_dataset(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建数据集"""
    data, csv_files, image_files, video_files = await _parse_dataset_create_request(request)

    data_source_ids = data.data_source_ids or []
    if data.data_source_id and data.data_source_id not in data_source_ids:
        data_source_ids.append(data.data_source_id)

    workspace_id = data.workspace_id
    data_sources = []
    if data_source_ids:
        result = await db.execute(select(DataSource).where(DataSource.id.in_(data_source_ids)))
        data_sources = result.scalars().all()
        if len(data_sources) != len(data_source_ids):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="部分数据源不存在",
            )
        workspace_id = workspace_id or data_sources[0].workspace_id
        if any(data_source.workspace_id != workspace_id for data_source in data_sources):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="数据源必须属于同一工作空间",
            )

    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少 workspace_id",
        )

    if not await verify_workspace_access(workspace_id, current_user, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权在该工作空间创建数据集",
        )

    service = DatasetService(db)
    try:
        dataset = await service.create_dataset_with_inputs(
            workspace_id=workspace_id,
            data_source_ids=data_source_ids,
            data_source_id=data.data_source_id,
            csv_uploads=csv_files,
            image_uploads=image_files,
            video_uploads=video_files,
            file_paths=data.file_paths,
            name=data.name,
            description=data.description,
            metrics=_dump_json_models(data.metrics),
            dimensions=_dump_json_models(data.dimensions),
            aliases=_dump_json_models(data.aliases),
            business_rules=data.business_rules,
            status=data.status or "draft",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return dataset


@router.get("", response_model=List[DatasetResponse])
async def list_datasets(
    workspace_id: int,
    data_source_id: int = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取数据集列表"""
    # 验证用户是否有权限访问该工作空间
    if not await verify_workspace_access(workspace_id, current_user, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问该工作空间",
        )

    query = select(Dataset).where(
        Dataset.workspace_id == workspace_id,
        Dataset.status != DatasetStatus.DEPRECATED,  # 过滤已废弃的数据集
    )

    if data_source_id:
        query = query.where(Dataset.data_source_id == data_source_id)

    result = await db.execute(query)
    datasets = result.scalars().all()

    return datasets


@router.get("/{dataset_id}", response_model=DatasetResponse)
async def get_dataset(
    dataset_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取数据集详情"""
    result = await db.execute(
        select(Dataset).where(Dataset.id == dataset_id)
    )
    dataset = result.scalar_one_or_none()

    if not dataset:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="数据集不存在",
        )

    # 验证用户是否有权限访问该数据集所在的工作空间
    if not await verify_workspace_access(dataset.workspace_id, current_user, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问该数据集",
        )

    return dataset


@router.patch("/{dataset_id}", response_model=DatasetResponse)
async def update_dataset(
    dataset_id: int,
    data: DatasetUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新数据集"""
    from app.core.logging import get_logger
    logger = get_logger(__name__)

    logger.info(f"[UPDATE_DATASET] 接收到的更新数据: {data.model_dump()}")

    result = await db.execute(
        select(Dataset).where(Dataset.id == dataset_id)
    )
    dataset = result.scalar_one_or_none()

    if not dataset:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="数据集不存在",
        )

    # 验证用户是否有权限访问该数据集所在的工作空间
    if not await verify_workspace_access(dataset.workspace_id, current_user, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权操作该数据集",
        )

    # 更新字段
    update_data = data.model_dump(exclude_unset=True)
    logger.info(f"[UPDATE_DATASET] 更新数据(exclude_unset): {update_data}")

    # 处理 JSON 字段
    if "metrics" in update_data:
        update_data["metrics"] = [m.model_dump() for m in update_data["metrics"]] if update_data["metrics"] else None
    if "dimensions" in update_data:
        update_data["dimensions"] = [d.model_dump() for d in update_data["dimensions"]] if update_data["dimensions"] else None
    if "aliases" in update_data:
        update_data["aliases"] = [a.model_dump() for a in update_data["aliases"]] if update_data["aliases"] else None

    for key, value in update_data.items():
        setattr(dataset, key, value)

    logger.info(f"[UPDATE_DATASET] 更新后的 dataset.data_source_ids: {dataset.data_source_ids}")

    # 版本号递增
    dataset.version += 1

    await db.commit()
    await db.refresh(dataset)

    logger.info(f"[UPDATE_DATASET] 提交后 dataset.data_source_ids: {dataset.data_source_ids}")

    return dataset


@router.delete("/{dataset_id}")
async def delete_dataset(
    dataset_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除数据集"""
    result = await db.execute(
        select(Dataset).where(Dataset.id == dataset_id)
    )
    dataset = result.scalar_one_or_none()

    if not dataset:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="数据集不存在",
        )

    # 验证用户是否有权限访问该数据集所在的工作空间
    if not await verify_workspace_access(dataset.workspace_id, current_user, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权删除该数据集",
        )

    # 软删除
    dataset.status = DatasetStatus.DEPRECATED
    await db.commit()

    return {"message": "数据集已删除"}
