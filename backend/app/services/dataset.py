"""
数据集服务 - 语义层与媒体接入管理。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.models import (
    Dataset,
    DatasetMediaResource,
    DatasetStatus,
    MediaResourceType,
    MediaSourceType,
    ProcessingStatus,
)
from app.services.datasource import DataSourceService
from app.services.media_tasks import get_media_task_manager
from app.services.media_utils import (
    build_dedupe_key,
    guess_media_type,
    is_csv_path,
    iter_supported_files,
    save_upload_file,
)

logger = get_logger(__name__)


class DatasetService:
    """数据集服务"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.data_source_service = DataSourceService(db)

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
        status: str = "draft",
        processing_status: str = "ready",
        progress: float = 100.0,
        media_count: int = 0,
    ) -> Dataset:
        """创建基础数据集记录。"""
        data_source_ids = self._merge_data_source_ids(data_source_ids, data_source_id)
        dataset = Dataset(
            workspace_id=workspace_id,
            data_source_id=data_source_ids[0] if data_source_ids else None,
            data_source_ids=data_source_ids,
            name=name,
            description=description,
            metrics=metrics,
            dimensions=dimensions,
            aliases=aliases,
            business_rules=business_rules,
            status=DatasetStatus(status) if status else DatasetStatus.DRAFT,
            processing_status=ProcessingStatus(processing_status),
            progress=progress,
            media_count=media_count,
            processed_count=0,
            failed_count=0,
        )

        self.db.add(dataset)
        await self.db.commit()
        await self.db.refresh(dataset)
        logger.info(f"创建数据集: {name} (ID: {dataset.id})")
        return dataset

    async def create_dataset_with_inputs(
        self,
        *,
        workspace_id: int,
        data_source_ids: Optional[List[int]] = None,
        data_source_id: Optional[int] = None,
        csv_uploads: Optional[List[UploadFile]] = None,
        image_uploads: Optional[List[UploadFile]] = None,
        video_uploads: Optional[List[UploadFile]] = None,
        file_paths: Optional[List[str]] = None,
        name: str,
        description: Optional[str] = None,
        metrics: Optional[List[dict]] = None,
        dimensions: Optional[List[dict]] = None,
        aliases: Optional[List[dict]] = None,
        business_rules: Optional[str] = None,
        status: str = "draft",
    ) -> Dataset:
        """创建数据集并接收 CSV/图片/视频/路径输入。"""
        merged_data_source_ids = self._merge_data_source_ids(data_source_ids, data_source_id)

        csv_uploads = csv_uploads or []
        image_uploads = image_uploads or []
        video_uploads = video_uploads or []
        file_paths = [path.strip() for path in (file_paths or []) if path and path.strip()]

        for upload in csv_uploads:
            data_source = await self.data_source_service.create_csv_data_source_from_upload(
                workspace_id=workspace_id,
                upload=upload,
            )
            if data_source.id not in merged_data_source_ids:
                merged_data_source_ids.append(data_source.id)

        media_inputs: list[dict] = []
        media_inputs.extend(await self._prepare_upload_media_inputs(image_uploads, MediaResourceType.IMAGE, workspace_id))
        media_inputs.extend(await self._prepare_upload_media_inputs(video_uploads, MediaResourceType.VIDEO, workspace_id))

        for path in file_paths:
            if is_csv_path(path):
                data_source = await self.data_source_service.create_csv_data_source_from_path(
                    workspace_id=workspace_id,
                    file_path=path,
                )
                if data_source.id not in merged_data_source_ids:
                    merged_data_source_ids.append(data_source.id)

        media_inputs.extend(self._prepare_path_media_inputs(file_paths))

        if not merged_data_source_ids and not media_inputs:
            raise ValueError("请至少提供一个 CSV、图片、视频或可访问路径")

        dataset = Dataset(
            workspace_id=workspace_id,
            data_source_id=merged_data_source_ids[0] if merged_data_source_ids else None,
            data_source_ids=merged_data_source_ids or None,
            name=name,
            description=description,
            metrics=metrics,
            dimensions=dimensions,
            aliases=aliases,
            business_rules=business_rules,
            status=DatasetStatus(status) if status else DatasetStatus.DRAFT,
            processing_status=ProcessingStatus.PENDING if media_inputs else ProcessingStatus.READY,
            progress=0.0 if media_inputs else 100.0,
            media_count=len(media_inputs),
            processed_count=0,
            failed_count=0,
        )
        self.db.add(dataset)
        await self.db.flush()

        seen_keys: set[str] = set()
        for item in media_inputs:
            if item["dedupe_key"] in seen_keys:
                continue
            seen_keys.add(item["dedupe_key"])
            self.db.add(
                DatasetMediaResource(
                    dataset_id=dataset.id,
                    resource_type=item["resource_type"],
                    source_type=item["source_type"],
                    original_path=item["original_path"],
                    stored_path=item["stored_path"],
                    file_name=item["file_name"],
                    mime_type=item["mime_type"],
                    file_size=item["file_size"],
                    checksum=item["checksum"],
                    dedupe_key=item["dedupe_key"],
                    status=ProcessingStatus.PENDING,
                )
            )

        await self.db.commit()
        await self.db.refresh(dataset)

        if media_inputs:
            await get_media_task_manager().enqueue_dataset(dataset.id)

        logger.info(
            "创建数据集完成: id=%s, data_sources=%s, media_count=%s",
            dataset.id,
            merged_data_source_ids,
            len(media_inputs),
        )
        return dataset

    def _merge_data_source_ids(
        self,
        data_source_ids: Optional[List[int]],
        data_source_id: Optional[int],
    ) -> List[int]:
        merged = list(dict.fromkeys(data_source_ids or []))
        if data_source_id and data_source_id not in merged:
            merged.append(data_source_id)
        return merged

    async def _prepare_upload_media_inputs(
        self,
        uploads: Iterable[UploadFile],
        expected_type: MediaResourceType,
        workspace_id: int,
    ) -> list[dict]:
        prepared: list[dict] = []
        for upload in uploads:
            if not upload.filename:
                continue
            actual_type = guess_media_type(upload.filename)
            if actual_type != expected_type:
                raise ValueError(f"{upload.filename} 文件类型不合法")
            stored_path, file_size, mime_type, checksum = await save_upload_file(
                upload,
                sub_dir=f"workspace_{workspace_id}",
            )
            prepared.append(
                {
                    "resource_type": expected_type,
                    "source_type": MediaSourceType.UPLOAD,
                    "original_path": upload.filename,
                    "stored_path": stored_path,
                    "file_name": Path(upload.filename).name,
                    "mime_type": mime_type,
                    "file_size": file_size,
                    "checksum": checksum,
                    "dedupe_key": build_dedupe_key(upload.filename, checksum, expected_type.value),
                }
            )
        return prepared

    def _prepare_path_media_inputs(self, file_paths: Iterable[str]) -> list[dict]:
        prepared: list[dict] = []
        for item in iter_supported_files(file_paths):
            prepared.append(
                {
                    "resource_type": item.resource_type,
                    "source_type": MediaSourceType.PATH,
                    "original_path": item.path,
                    "stored_path": item.path,
                    "file_name": item.file_name,
                    "mime_type": item.mime_type,
                    "file_size": item.file_size,
                    "checksum": item.checksum,
                    "dedupe_key": item.dedupe_key,
                }
            )
        return prepared
