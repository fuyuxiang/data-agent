"""
智能标注后台任务管理器。
"""
from __future__ import annotations

import asyncio

from app.core.config import settings
from app.core.logging import get_logger
from app.services.annotation_service import annotation_service
from app.services.annotation_store import ensure_annotation_dirs, iter_unfinished_sessions

logger = get_logger(__name__)


class AnnotationTaskManager:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._active: set[str] = set()
        self._queued: set[str] = set()
        self._started = False

    async def startup(self) -> None:
        if self._started:
            return
        ensure_annotation_dirs()
        self._started = True
        worker_count = max(int(settings.ANNOTATION_TASK_WORKERS), 1)
        self._workers = [asyncio.create_task(self._worker(index)) for index in range(worker_count)]
        for session_id in iter_unfinished_sessions():
            await self.enqueue(session_id)
        logger.info("智能标注任务管理器已启动，worker=%s", worker_count)

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
        self._active.clear()
        self._queued.clear()

    async def enqueue(self, session_id: str) -> None:
        if session_id in self._queued or session_id in self._active:
            return
        self._queued.add(session_id)
        await self.queue.put(session_id)

    async def _worker(self, worker_index: int) -> None:
        while True:
            session_id = await self.queue.get()
            self._queued.discard(session_id)
            self._active.add(session_id)
            try:
                await asyncio.to_thread(annotation_service.process_session, session_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("智能标注任务执行失败: worker=%s session=%s error=%s", worker_index, session_id, exc, exc_info=True)
            finally:
                self._active.discard(session_id)
                self.queue.task_done()


annotation_task_manager = AnnotationTaskManager()


def get_annotation_task_manager() -> AnnotationTaskManager:
    return annotation_task_manager
