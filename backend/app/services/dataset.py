"""
数据集服务 - 语义层管理
"""
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.models import Dataset, DatasetStatus

logger = get_logger(__name__)


class DatasetService:
    """数据集服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_dataset(
        self,
        workspace_id: int,
        data_source_ids: Optional[List[int]] = None,
        data_source_id: Optional[int] = None,
        name: str = None,
        description: Optional[str] = None,
        metrics: Optional[List[dict]] = None,
        dimensions: Optional[List[dict]] = None,
        aliases: Optional[List[dict]] = None,
        business_rules: Optional[str] = None,
        status: str = 'draft',
    ) -> Dataset:
        """创建数据集"""
        # 处理多个数据源
        if data_source_ids is None:
            data_source_ids = []
        if data_source_id and data_source_id not in data_source_ids:
            data_source_ids.append(data_source_id)

        dataset = Dataset(
            workspace_id=workspace_id,
            data_source_id=data_source_ids[0] if data_source_ids else None,  # 兼容旧版
            data_source_ids=data_source_ids,
            name=name,
            description=description,
            metrics=metrics,
            dimensions=dimensions,
            aliases=aliases,
            business_rules=business_rules,
            status=DatasetStatus(status) if status else DatasetStatus.DRAFT,
        )

        self.db.add(dataset)
        await self.db.commit()
        await self.db.refresh(dataset)

        logger.info(f"创建数据集: {name} (ID: {dataset.id})")

        return dataset
