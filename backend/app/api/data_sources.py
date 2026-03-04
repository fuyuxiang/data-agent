"""
数据源管理 API - 支持文件上传
"""
import os
import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from app.core.database import get_db
from app.api.auth import get_current_user
from app.core.logging import get_logger
from app.models.models import User, DataSource, CSVFile, DataSourceSchema, DataSourceType
from app.schemas.schemas import (
    DataSourceCreate,
    DataSourceUpdate,
    DataSourceResponse,
    DataSourceTestRequest,
    DataSourceTestResponse,
    SchemaColumnResponse,
    SchemaRefreshResponse,
)
from app.services.datasource import DataSourceService

logger = get_logger(__name__)
router = APIRouter()

# CSV 文件存储目录
BACKEND_DIR = Path(__file__).resolve().parents[2]
UPLOAD_DIR = BACKEND_DIR / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@router.post("", response_model=DataSourceResponse)
async def create_data_source(
    data: DataSourceCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建数据源"""
    service = DataSourceService(db)

    data_source = await service.create_data_source(
        workspace_id=data.workspace_id,
        name=data.name,
        ds_type=data.type,
        host=data.host,
        port=data.port,
        database=data.database,
        username=data.username,
        password=data.password,
        connection_string=data.connection_string,
    )

    return data_source


@router.post("/upload-csv", response_model=DataSourceResponse)
async def upload_csv(
    file: UploadFile = File(...),
    workspace_id: int = 1,
    data_source_id: int = None,  # 可选：上传到已存在的数据源
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """上传 CSV 文件，可创建新数据源或上传到已有数据源"""
    if not file.filename.endswith('.csv'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只支持 CSV 文件",
        )

    # 生成唯一文件名
    file_id = uuid.uuid4().hex[:8]
    filename = f"{file_id}_{file.filename}"
    file_path = str(UPLOAD_DIR / filename)

    # 保存文件
    content = await file.read()
    with open(file_path, 'wb') as f:
        f.write(content)

    file_size = len(content)
    logger.info(f"CSV 文件上传成功: {file_path}, 大小: {file_size} bytes")

    # 尝试解析 CSV 获取基本信息
    row_count = None
    column_count = None
    try:
        import pandas as pd
        df = pd.read_csv(file_path)
        row_count = len(df)
        column_count = len(df.columns)
    except Exception as e:
        logger.warning(f"解析 CSV 文件失败: {e}")

    # 确定数据源
    if data_source_id:
        # 上传到已存在的数据源
        result = await db.execute(
            select(DataSource).where(DataSource.id == data_source_id)
        )
        data_source = result.scalar_one_or_none()
        if not data_source:
            # 文件已保存，删除它
            os.remove(file_path)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="数据源不存在",
            )
        if data_source.type != DataSourceType.CSV:
            os.remove(file_path)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="只能上传 CSV 文件到 CSV 类型的数据源",
            )
    else:
        # 创建新数据源
        data_source = DataSource(
            workspace_id=workspace_id,
            name=file.filename.replace('.csv', ''),
            type=DataSourceType.CSV,
            is_active=True,
        )
        db.add(data_source)
        await db.commit()
        await db.refresh(data_source)

    # 创建 CSVFile 记录
    csv_file = CSVFile(
        data_source_id=data_source.id,
        filename=file.filename,
        file_path=file_path,
        file_size=file_size,
        row_count=row_count,
        column_count=column_count,
    )
    db.add(csv_file)
    await db.commit()

    # 提前提取所需数据
    ds_id = data_source.id
    ds_workspace_id = data_source.workspace_id
    ds_name = data_source.name
    ds_type = data_source.type
    ds_is_active = data_source.is_active
    ds_created_at = data_source.created_at
    ds_updated_at = data_source.updated_at

    # 自动刷新 Schema
    try:
        service = DataSourceService(db)
        await service.refresh_schema(data_source)
    except Exception as e:
        logger.warning(f"自动刷新 Schema 失败: {e}")

    # 返回数据
    from app.schemas.schemas import DataSourceResponse as ResponseSchema
    return ResponseSchema(
        id=ds_id,
        workspace_id=ds_workspace_id,
        name=ds_name,
        type=ds_type.value,
        host=None,
        port=None,
        database=None,
        username=None,
        is_active=ds_is_active,
        created_at=ds_created_at,
        updated_at=ds_updated_at,
    )


@router.get("", response_model=List[DataSourceResponse])
async def list_data_sources(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取数据源列表"""
    from app.models.models import UserWorkspace
    result = await db.execute(
        select(UserWorkspace).where(
            and_(
                UserWorkspace.workspace_id == workspace_id,
                UserWorkspace.user_id == current_user.id,
            )
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问该工作空间",
        )

    result = await db.execute(
        select(DataSource).where(
            DataSource.workspace_id == workspace_id,
            DataSource.is_active == True,
        )
    )
    data_sources = result.scalars().all()

    return data_sources


@router.get("/{data_source_id}", response_model=DataSourceResponse)
async def get_data_source(
    data_source_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取数据源详情"""
    result = await db.execute(
        select(DataSource).where(DataSource.id == data_source_id)
    )
    data_source = result.scalar_one_or_none()

    if not data_source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="数据源不存在",
        )

    return data_source


@router.patch("/{data_source_id}", response_model=DataSourceResponse)
async def update_data_source(
    data_source_id: int,
    data: DataSourceUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新数据源"""
    result = await db.execute(
        select(DataSource).where(DataSource.id == data_source_id)
    )
    data_source = result.scalar_one_or_none()

    if not data_source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="数据源不存在",
        )

    update_data = data.model_dump(exclude_unset=True)
    if "password" in update_data and update_data["password"]:
        from app.services.datasource import encrypt_password
        update_data["password_encrypted"] = encrypt_password(update_data.pop("password"))

    for key, value in update_data.items():
        setattr(data_source, key, value)

    await db.commit()
    await db.refresh(data_source)

    return data_source


@router.delete("/{data_source_id}")
async def delete_data_source(
    data_source_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除数据源"""
    result = await db.execute(
        select(DataSource).where(DataSource.id == data_source_id)
    )
    data_source = result.scalar_one_or_none()

    if not data_source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="数据源不存在",
        )

    # 如果是 CSV 文件，删除所有关联的 CSV 文件
    if data_source.type == DataSourceType.CSV:
        result = await db.execute(
            select(CSVFile).where(CSVFile.data_source_id == data_source_id)
        )
        csv_files = result.scalars().all()
        for csv_file in csv_files:
            try:
                if os.path.exists(csv_file.file_path):
                    os.remove(csv_file.file_path)
            except Exception as e:
                logger.warning(f"删除 CSV 文件失败: {e}")
            await db.delete(csv_file)

    # 软删除
    data_source.is_active = False
    await db.commit()

    return {"message": "数据源已删除"}


@router.post("/test", response_model=DataSourceTestResponse)
async def test_connection(
    test_request: DataSourceTestRequest,
):
    """测试数据源连接"""
    service = DataSourceService(None)

    try:
        success, message, table_count = await service.test_connection(
            ds_type=test_request.type,
            host=test_request.host,
            port=test_request.port,
            database=test_request.database,
            username=test_request.username,
            password=test_request.password,
            connection_string=test_request.connection_string,
        )

        return DataSourceTestResponse(
            success=success,
            message=message,
            table_count=table_count,
        )
    except Exception as e:
        logger.error(f"连接测试失败: {e}")
        return DataSourceTestResponse(
            success=False,
            message=f"连接失败: {str(e)}",
        )


@router.get("/{data_source_id}/schema", response_model=List[SchemaColumnResponse])
async def get_schema(
    data_source_id: int,
    table_name: str = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取数据源 Schema"""
    result = await db.execute(
        select(DataSourceSchema).where(
            DataSourceSchema.data_source_id == data_source_id
        )
    )
    schemas = result.scalars().all()

    if table_name:
        schemas = [s for s in schemas if s.table_name == table_name]

    return schemas


@router.post("/{data_source_id}/refresh-schema", response_model=SchemaRefreshResponse)
async def refresh_schema(
    data_source_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """刷新数据源 Schema"""
    result = await db.execute(
        select(DataSource).where(DataSource.id == data_source_id)
    )
    data_source = result.scalar_one_or_none()

    if not data_source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="数据源不存在",
        )

    service = DataSourceService(db)
    try:
        table_count, column_count = await service.refresh_schema(data_source)
    except NotImplementedError as e:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Schema 刷新失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Schema 刷新失败: {str(e)}",
        )

    return SchemaRefreshResponse(
        success=True,
        table_count=table_count,
        column_count=column_count,
        message="Schema 刷新成功",
    )


# ── CSV 文件管理 ───────────────────────────────────────────────────────────

@router.get("/{data_source_id}/csv-files", response_model=List[dict])
async def list_csv_files(
    data_source_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取数据源下的 CSV 文件列表"""
    result = await db.execute(
        select(CSVFile).where(CSVFile.data_source_id == data_source_id)
    )
    csv_files = result.scalars().all()

    return [
        {
            "id": f.id,
            "filename": f.filename,
            "file_size": f.file_size,
            "row_count": f.row_count,
            "column_count": f.column_count,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        }
        for f in csv_files
    ]


@router.delete("/{data_source_id}/csv-files/{csv_file_id}")
async def delete_csv_file(
    data_source_id: int,
    csv_file_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除 CSV 文件"""
    result = await db.execute(
        select(CSVFile).where(
            and_(
                CSVFile.id == csv_file_id,
                CSVFile.data_source_id == data_source_id,
            )
        )
    )
    csv_file = result.scalar_one_or_none()

    if not csv_file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="CSV 文件不存在",
        )

    # 删除物理文件
    try:
        if os.path.exists(csv_file.file_path):
            os.remove(csv_file.file_path)
    except Exception as e:
        logger.warning(f"删除 CSV 物理文件失败: {e}")

    # 删除数据库记录
    await db.delete(csv_file)
    await db.commit()

    # 刷新 Schema
    result = await db.execute(
        select(DataSource).where(DataSource.id == data_source_id)
    )
    data_source = result.scalar_one_or_none()
    if data_source:
        try:
            service = DataSourceService(db)
            await service.refresh_schema(data_source)
        except Exception as e:
            logger.warning(f"刷新 Schema 失败: {e}")

    return {"message": "CSV 文件已删除"}
