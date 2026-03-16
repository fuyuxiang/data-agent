"""
轻量媒体处理任务管理器。

当前项目没有现成 MQ / Celery，这里使用进程内 asyncio worker，
并在服务启动时扫描 pending/processing 数据集进行恢复。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logging import get_logger
from app.models.models import Dataset, ProcessingStatus
from app.services.media_processing import process_dataset_resources

logger = get_logger(__name__)


class MediaTaskManager:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._queued: set[int] = set()
        self._active: set[int] = set()
        self._started = False

    async def startup(self) -> None:
        if self._started:
            return
        self._started = True
        worker_count = max(int(settings.MEDIA_TASK_WORKERS), 1)
        self._workers = [asyncio.create_task(self._worker(idx)) for idx in range(worker_count)]
        await self.recover_unfinished_datasets()
        logger.info(f"媒体任务管理器已启动，worker={worker_count}")

    async def shutdown(self) -> None:
        if not self._started:
            return
        self._started = False
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        self._queued.clear()
        self._active.clear()

    async def enqueue_dataset(self, dataset_id: int) -> None:
        if dataset_id in self._queued or dataset_id in self._active:
            return
        self._queued.add(dataset_id)
        await self.queue.put(dataset_id)

    async def recover_unfinished_datasets(self) -> None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Dataset.id).where(
                    Dataset.processing_status.in_([ProcessingStatus.PENDING, ProcessingStatus.PROCESSING])
                )
            )
            dataset_ids = [row[0] for row in result.all()]

        for dataset_id in dataset_ids:
            await self.enqueue_dataset(dataset_id)

    async def _worker(self, worker_index: int) -> None:
        while True:
            dataset_id = await self.queue.get()
            self._queued.discard(dataset_id)
            self._active.add(dataset_id)
            try:
                async with AsyncSessionLocal() as db:
                    await process_dataset_resources(db, dataset_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"媒体任务执行失败: worker={worker_index}, dataset={dataset_id}, error={exc}", exc_info=True)
            finally:
                self._active.discard(dataset_id)
                self.queue.task_done()


media_task_manager = MediaTaskManager()


def get_media_task_manager() -> MediaTaskManager:
    return media_task_manager
