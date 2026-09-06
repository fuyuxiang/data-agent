from __future__ import annotations

import os
import hashlib
import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from flask import Flask

from ..core.database import Database, utcnow
from .hooks import dispatch_hooks


JobHandler = Callable[[Flask, dict[str, Any], Callable[[float, str], None], threading.Event], dict]
_HANDLERS: dict[str, JobHandler] = {}


def register_job_handler(job_type: str, handler: JobHandler) -> None:
    """Register a restart-safe handler. The durable job stores only its typed spec."""
    name = str(job_type).strip()
    if not name or not callable(handler):
        raise ValueError("invalid job handler")
    previous = _HANDLERS.get(name)
    if previous is not None and previous is not handler:
        raise RuntimeError(f"duplicate job handler: {name}")
    _HANDLERS[name] = handler


class JobManager:
    def __init__(self, app: Flask, max_workers: int = 4, max_pending: int | None = None):
        self.app = app
        self.db: Database = app.extensions["meridian_db"]
        max_workers = max(1, min(int(max_workers), 32))
        max_pending = max_pending if max_pending is not None else int(os.getenv("MERIDIAN_MAX_PENDING_JOBS", "100"))
        self.max_outstanding = max_workers + max(0, min(int(max_pending), 10_000))
        self.max_per_workspace = max(1, min(int(os.getenv("MERIDIAN_MAX_WORKSPACE_JOBS", "25")), self.max_outstanding))
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="meridian-job")
        self.cancel_flags: dict[str, threading.Event] = {}
        self._workspace_outstanding: dict[str, int] = {}
        self._outstanding = 0
        self._lock = threading.RLock()
        self._recover_orphans()

    def _reserve(self, workspace_id: str) -> None:
        with self._lock:
            workspace_count = self._workspace_outstanding.get(workspace_id, 0)
            if self._outstanding >= self.max_outstanding:
                raise ValueError("后台任务队列已满，请稍后重试")
            if workspace_count >= self.max_per_workspace:
                raise ValueError("当前工作空间的后台任务过多，请稍后重试")
            self._outstanding += 1
            self._workspace_outstanding[workspace_id] = workspace_count + 1

    def _release(self, workspace_id: str) -> None:
        with self._lock:
            self._outstanding = max(0, self._outstanding - 1)
            remaining = self._workspace_outstanding.get(workspace_id, 1) - 1
            if remaining > 0:
                self._workspace_outstanding[workspace_id] = remaining
            else:
                self._workspace_outstanding.pop(workspace_id, None)

    def _recover_orphans(self) -> None:
        """Requeue durable specs; external handlers must reconcile before resubmitting work."""
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM typed_jobs WHERE status IN ('queued','running','waiting_external') "
                "ORDER BY created_at LIMIT 5000",
            ).fetchall()
            for row in rows:
                status = "cancelled" if row["cancel_requested"] else "queued" if row["job_type"] in _HANDLERS else "blocked"
                error = None if status == "queued" else "handler_unavailable"
                connection.execute(
                    "UPDATE typed_jobs SET status=?, error_code=?, lease_owner=NULL, "
                    "lease_expires_at=NULL, updated_at=? WHERE id=?",
                    (status, error, utcnow(), row["id"]),
                )
        for row in rows:
            payload = dict(row)
            if payload.get("cancel_requested"):
                self._mirror(payload["id"], status="cancelled", message="已取消")
            elif payload["job_type"] in _HANDLERS:
                self._reserve(payload["workspace_id"])
                cancel = threading.Event()
                with self._lock:
                    self.cancel_flags[payload["id"]] = cancel
                self.executor.submit(self._run_spec, payload["id"], payload["workspace_id"], cancel, True)
            else:
                self._mirror(payload["id"], status="blocked", message="任务处理器不可用", error="handler_unavailable")

    def submit_spec(
        self,
        *,
        workspace_id: str,
        session_id: str | None,
        job_type: str,
        title: str,
        spec: dict[str, Any],
        run_id: str | None = None,
    ) -> dict:
        if job_type not in _HANDLERS:
            raise ValueError(f"unregistered job type: {job_type}")
        if not isinstance(spec, dict):
            raise ValueError("job spec must be an object")
        encoded = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 256_000:
            raise ValueError("job spec exceeds 256KB")
        self._reserve(workspace_id)
        job_id = self.db.new_id("job")
        now = utcnow()
        cancel = threading.Event()
        try:
            with self.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO typed_jobs(id,workspace_id,run_id,job_type,spec,spec_hash,status,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (job_id, workspace_id, run_id, job_type, encoded,
                     hashlib.sha256(encoded.encode("utf-8")).hexdigest(), "queued", now, now),
                )
            job = self.db.put("jobs", {
                "id": job_id, "workspace_id": workspace_id, "session_id": session_id,
                "run_id": run_id, "kind": job_type, "title": title, "status": "queued",
                "progress": 0, "message": "等待执行", "result": None, "error": None,
                "cancel_requested": False, "typed": True,
            }, workspace_id=workspace_id)
            with self._lock:
                self.cancel_flags[job_id] = cancel
            self.db.job_event(job_id, "queued", job)
            with self.app.app_context():
                dispatch_hooks("job.queued", job, workspace_id, database=self.db)
            self.executor.submit(self._run_spec, job_id, workspace_id, cancel, False)
            return job
        except Exception:
            with self._lock:
                self.cancel_flags.pop(job_id, None)
            self._release(workspace_id)
            raise

    def _typed_job(self, job_id: str) -> dict[str, Any] | None:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM typed_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["spec"] = json.loads(result["spec"])
        result["result"] = json.loads(result["result"]) if result.get("result") else None
        return result

    def _mirror(self, job_id: str, **values: Any) -> dict | None:
        mapping = {"error_code": "error"}
        payload = {mapping.get(key, key): value for key, value in values.items()}
        return self.db.patch("jobs", job_id, payload)

    def _run_spec(self, job_id: str, workspace_id: str, cancel: threading.Event, recovered: bool) -> None:
        with self.app.app_context():
            typed = self._typed_job(job_id)
            if not typed:
                self._release(workspace_id)
                return
            if typed.get("cancel_requested"):
                cancel.set()
            handler = _HANDLERS.get(typed["job_type"])
            if handler is None:
                self._finish_typed(job_id, "blocked", None, "handler_unavailable")
                self._release(workspace_id)
                return
            with self.db.transaction() as connection:
                row = connection.execute("SELECT lease_epoch FROM typed_jobs WHERE id=?", (job_id,)).fetchone()
                epoch = int(row["lease_epoch"]) + 1
                connection.execute(
                    "UPDATE typed_jobs SET status='running',lease_owner=?,lease_epoch=?,updated_at=? WHERE id=?",
                    (f"pid-{os.getpid()}", epoch, utcnow(), job_id),
                )
            job = self._mirror(job_id, status="running", started_at=utcnow(),
                               message="恢复并校验外部状态" if recovered else "正在执行")
            self.db.job_event(job_id, "running", job or {})
            try:
                dispatch_hooks("job.started", job or {"id": job_id}, workspace_id, database=self.db)
            except Exception as exc:
                self._finish_typed(job_id, "failed", None, type(exc).__name__)
                failed = self._mirror(
                    job_id, status="failed", message="启动 Hook 执行失败", error=str(exc),
                    trace=traceback.format_exc(limit=12), finished_at=utcnow(),
                )
                self.db.job_event(job_id, "failed", failed or {})
                with self._lock:
                    self.cancel_flags.pop(job_id, None)
                self._release(workspace_id)
                return

            def progress(value: float, message: str) -> None:
                current = self._mirror(job_id, progress=max(0, min(100, round(value, 1))), message=message)
                self.db.job_event(job_id, "progress", current or {})

            try:
                result = handler(self.app, typed["spec"], progress, cancel)
                status = "cancelled" if cancel.is_set() else "completed"
                self._finish_typed(job_id, status, result, None)
                final = self._mirror(
                    job_id, status=status, progress=100 if status == "completed" else 0,
                    message="执行完成" if status == "completed" else "已取消",
                    result=result, finished_at=utcnow(),
                )
                self.db.job_event(job_id, status, final or {})
                dispatch_hooks(f"job.{status}", final or {"id": job_id}, workspace_id, database=self.db)
            except Exception as exc:
                self._finish_typed(job_id, "failed", None, type(exc).__name__)
                final = self._mirror(
                    job_id, status="failed", message="执行失败", error=str(exc),
                    trace=traceback.format_exc(limit=12), finished_at=utcnow(),
                )
                self.db.job_event(job_id, "failed", final or {})
                dispatch_hooks("job.failed", final or {"id": job_id}, workspace_id, database=self.db)
            finally:
                with self._lock:
                    self.cancel_flags.pop(job_id, None)
                self._release(workspace_id)

    def _finish_typed(self, job_id: str, status: str, result: dict | None, error_code: str | None) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE typed_jobs SET status=?,result=?,error_code=?,lease_owner=NULL,"
                "lease_expires_at=NULL,updated_at=?,finished_at=? WHERE id=?",
                (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
                 error_code, utcnow(), utcnow(), job_id),
            )

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            flag = self.cancel_flags.get(job_id)
            if flag:
                flag.set()
        typed = self._typed_job(job_id)
        if not typed or typed.get("status") in {"completed", "failed", "cancelled", "blocked"}:
            return False
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE typed_jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelling' ELSE status END,updated_at=? WHERE id=?",
                (utcnow(), job_id),
            )
        self.db.patch("jobs", job_id, {"cancel_requested": True, "message": "正在取消"})
        self.db.job_event(job_id, "cancel_requested", {"id": job_id})
        return True

    def shutdown(self) -> None:
        with self._lock:
            for flag in self.cancel_flags.values():
                flag.set()
        self.executor.shutdown(wait=False, cancel_futures=True)


def get_job_manager(app: Flask) -> JobManager:
    manager = app.extensions.get("meridian_jobs")
    if manager is None:
        manager = JobManager(app)
        app.extensions["meridian_jobs"] = manager
    return manager
