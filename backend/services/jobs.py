from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from flask import Flask

from ..core.database import Database, utcnow
from .hooks import dispatch_hooks


class JobManager:
    def __init__(self, app: Flask, max_workers: int = 4):
        self.app = app
        self.db: Database = app.extensions["meridian_db"]
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="meridian-job")
        self.cancel_flags: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._recover_orphans()

    def _recover_orphans(self) -> None:
        """Convert process-owned in-flight jobs into explicit recoverable state after restart."""
        for job in self.db.list("jobs", workspace_id=None, limit=5000):
            if job.get("status") not in {"queued", "running"}:
                continue
            recovered = self.db.patch(
                "jobs", job["id"],
                {
                    "status": "failed", "message": "服务重启中断了执行",
                    "error": "service_restarted", "recoverable": job.get("kind", "").startswith("workflow"),
                    "finished_at": utcnow(),
                },
            )
            self.db.job_event(job["id"], "recovered_after_restart", recovered or job)
            for run in self.db.list("workflow_runs", workspace_id=job.get("workspace_id"), limit=5000):
                if run.get("job_id") != job["id"] or run.get("status") not in {"queued", "running"}:
                    continue
                self.db.patch(
                    "workflow_runs", run["id"],
                    {
                        "status": "paused", "pause_requested": False, "recoverable": True,
                        "paused_at": utcnow(), "recovery_reason": "service_restarted",
                    },
                )

    def submit(
        self,
        *,
        workspace_id: str,
        session_id: str | None,
        kind: str,
        title: str,
        work: Callable[[Callable[[float, str], None], threading.Event], dict],
    ) -> dict:
        job_id = self.db.new_id("job")
        cancel = threading.Event()
        job = self.db.put(
            "jobs",
            {
                "id": job_id,
                "workspace_id": workspace_id,
                "session_id": session_id,
                "kind": kind,
                "title": title,
                "status": "queued",
                "progress": 0,
                "message": "等待执行",
                "result": None,
                "error": None,
                "cancel_requested": False,
            },
            workspace_id=workspace_id,
        )
        with self._lock:
            self.cancel_flags[job_id] = cancel
        self.db.job_event(job_id, "queued", job)
        with self.app.app_context():
            dispatch_hooks("job.queued", job, workspace_id, database=self.db)
        self.executor.submit(self._run, job_id, work, cancel)
        return job

    def _run(self, job_id: str, work, cancel: threading.Event) -> None:
        with self.app.app_context():
            job = self.db.patch("jobs", job_id, {"status": "running", "started_at": utcnow(), "message": "正在执行"})
            self.db.job_event(job_id, "running", job or {})
            dispatch_hooks("job.started", job or {"id": job_id}, (job or {}).get("workspace_id", "default"), database=self.db)

            def progress(value: float, message: str) -> None:
                current = self.db.patch("jobs", job_id, {"progress": max(0, min(100, round(value, 1))), "message": message})
                self.db.job_event(job_id, "progress", current or {})

            try:
                result = work(progress, cancel)
                if cancel.is_set():
                    final = self.db.patch("jobs", job_id, {"status": "cancelled", "message": "已取消", "finished_at": utcnow()})
                else:
                    final = self.db.patch("jobs", job_id, {"status": "completed", "progress": 100, "message": "执行完成", "result": result, "finished_at": utcnow()})
                self.db.job_event(job_id, final["status"], final)
                dispatch_hooks(
                    f"job.{final['status']}", final,
                    final.get("workspace_id", "default"), database=self.db,
                )
            except Exception as exc:
                final = self.db.patch(
                    "jobs",
                    job_id,
                    {
                        "status": "failed",
                        "message": "执行失败",
                        "error": str(exc),
                        "trace": traceback.format_exc(limit=12),
                        "finished_at": utcnow(),
                    },
                )
                self.db.job_event(job_id, "failed", final or {})
                dispatch_hooks(
                    "job.failed", final or {"id": job_id, "error": str(exc)},
                    (final or {}).get("workspace_id", "default"), database=self.db,
                )
            finally:
                with self._lock:
                    self.cancel_flags.pop(job_id, None)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            flag = self.cancel_flags.get(job_id)
            if not flag:
                return False
            flag.set()
        self.db.patch("jobs", job_id, {"cancel_requested": True, "message": "正在取消"})
        self.db.job_event(job_id, "cancel_requested", {"id": job_id})
        return True


def get_job_manager(app: Flask) -> JobManager:
    manager = app.extensions.get("meridian_jobs")
    if manager is None:
        manager = JobManager(app)
        app.extensions["meridian_jobs"] = manager
    return manager
