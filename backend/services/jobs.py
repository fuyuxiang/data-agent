from __future__ import annotations

import os
import hashlib
import json
import threading
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from flask import Flask

from ..core.database import Database, utcnow


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
        self._futures: dict[str, Future] = {}
        self._workspace_outstanding: dict[str, int] = {}
        self._outstanding = 0
        self._lock = threading.RLock()
        self._reconcile_lock = threading.Lock()
        self._reconcile_thread: threading.Thread | None = None
        self._stop_reconcile = threading.Event()
        self._recover_orphans()
        with self.db.connect() as connection:
            cancelling = connection.execute("SELECT id FROM agent_runs WHERE execution_status='cancelling'").fetchall()
        for row in cancelling:
            self._finalize_cancelled_run(row["id"])
        self._ensure_cancel_reconciler()

    def _active_run_jobs(self, run_id: str) -> list[dict]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT id,workspace_id,run_id FROM typed_jobs WHERE run_id=? "
                "AND status IN ('queued','running','waiting_external','cancelling')", (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _remote_run_jobs(self, run_id: str) -> list[tuple[str, dict]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT collection,payload FROM records WHERE collection IN ('warehouse_queries','remote_batches') "
                "AND json_extract(payload,'$.run_id')=?", (run_id,),
            ).fetchall()
        return [(row["collection"], json.loads(row["payload"])) for row in rows]

    @staticmethod
    def _remote_active(collection: str, record: dict) -> bool:
        terminal = {"finished", "failed", "cancelled", "success", "dead", "error", "killed"}
        return str(record.get("status" if collection == "warehouse_queries" else "state") or "").lower() not in terminal

    def cancel_run(self, run_id: str, *, expected_version: int | None = None) -> dict:
        """Persist the intent before cancelling local and remote work."""
        from ..agent.store import RunStore

        store = RunStore(self.db)
        run = store.update_status(
            run_id, "cancelling", stop_reason="cancel_requested", expected_version=expected_version,
        )
        if run["execution_status"] != "cancelling":
            return run
        for job in self._active_run_jobs(run_id):
            self.cancel(job["id"], reconcile=False)
        self._finalize_cancelled_run(run_id)
        self._ensure_cancel_reconciler()
        return store.get_run(run_id) or run

    def _finalize_cancelled_run(self, run_id: str | None) -> None:
        """Finish an analysis whose durable job has already disappeared."""
        if not run_id:
            return
        from ..agent.store import RunStore

        store = RunStore(self.db)
        run = store.get_run(str(run_id))
        if not run or run.get("execution_status") != "cancelling":
            return
        if self._active_run_jobs(str(run_id)):
            return
        remotes = self._remote_run_jobs(str(run_id))
        if any(self._remote_active(collection, record) for collection, record in remotes):
            return
        # Reconcile durable actions as well as the UI status. A missing external
        # record must remain visibly pending instead of claiming cancellation.
        known_ids = {str(record["id"]) for _collection, record in remotes}
        for action in store.actions(str(run_id)):
            if action.get("status") in {"accepted", "unknown"} and action.get("external_job_id"):
                if str(action["external_job_id"]) not in known_ids:
                    store.update_status(str(run_id), "cancelling", stop_reason="cancel_retry_pending")
                    return
                store.complete_external_action(
                    str(run_id), str(action["external_job_id"]), status="cancelled",
                    result={"status": "CANCELLED", "reason": "user_cancelled"},
                )
        if store.get_run(str(run_id)):
            store.update_status(
                str(run_id), "cancelled", outcome="cancelled", stop_reason="user_cancelled",
            )

    def _reconcile_cancelled_runs(self) -> None:
        """Mark cancelling runs complete when no active durable job remains."""
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM agent_runs WHERE execution_status='cancelling' AND archived_at IS NULL",
            ).fetchall()
        from .data_plane.factory import livy_adapter, trino_adapter

        with self.app.app_context(), self._reconcile_lock:
            for row in rows:
                run_id = str(row["id"])
                errors = False
                for job in self._active_run_jobs(run_id):
                    self.cancel(job["id"], reconcile=False)
                for collection, record in self._remote_run_jobs(run_id):
                    if not self._remote_active(collection, record):
                        continue
                    try:
                        factory = trino_adapter if collection == "warehouse_queries" else livy_adapter
                        adapter = factory(self.db, record["workspace_id"], record["engine_id"], for_cancellation=True)
                        if not record.get("cancel_dispatched_at"):
                            adapter.cancel(record["id"])
                        current = self.db.get(collection, record["id"], workspace_id=record["workspace_id"], include_archived=True)
                        if current and self._remote_active(collection, current):
                            adapter.poll(record["id"])
                        self.db.patch(collection, record["id"], {"cancellation_error": None}, workspace_id=record["workspace_id"])
                    except Exception as exc:
                        # Keep intent durable and retry after transient network or
                        # engine configuration failures, including process restarts.
                        self.db.patch(collection, record["id"], {"cancellation_error": str(exc)}, workspace_id=record["workspace_id"])
                        errors = True
                from ..agent.store import RunStore
                RunStore(self.db).update_status(
                    run_id, "cancelling", stop_reason="cancel_retry_pending" if errors else "cancel_confirmation_pending",
                )
                self._finalize_cancelled_run(run_id)

    def _ensure_cancel_reconciler(self) -> None:
        with self._lock:
            if self._stop_reconcile.is_set() or (self._reconcile_thread and self._reconcile_thread.is_alive()):
                return
            with self.db.connect() as connection:
                pending = connection.execute("SELECT 1 FROM agent_runs WHERE execution_status='cancelling' LIMIT 1").fetchone()
            if pending:
                self._reconcile_thread = threading.Thread(target=self._cancel_reconcile_loop, name="analysis-cancel", daemon=True)
                self._reconcile_thread.start()

    def _cancel_reconcile_loop(self) -> None:
        while not self._stop_reconcile.is_set():
            self._reconcile_cancelled_runs()
            with self._lock, self.db.connect() as connection:
                pending = connection.execute("SELECT 1 FROM agent_runs WHERE execution_status='cancelling' AND archived_at IS NULL LIMIT 1").fetchone()
                if not pending:
                    self._reconcile_thread = None
                    return
            self._stop_reconcile.wait(1)

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
                "SELECT * FROM typed_jobs WHERE status IN ('queued','running','waiting_external','cancelling') "
                "ORDER BY created_at LIMIT 5000",
            ).fetchall()
            for row in rows:
                run = connection.execute("SELECT execution_status FROM agent_runs WHERE id=?", (row["run_id"],)).fetchone() if row["run_id"] else None
                cancelled = row["cancel_requested"] or (run and run["execution_status"] in {"cancelling", "cancelled"})
                status = "cancelled" if cancelled else "queued" if row["job_type"] in _HANDLERS else "blocked"
                error = "cancel_requested" if status == "cancelled" else None if status == "queued" else "handler_unavailable"
                connection.execute(
                    "UPDATE typed_jobs SET status=?, error_code=?, cancel_requested=?, lease_owner=NULL, "
                    "lease_expires_at=NULL, updated_at=? WHERE id=?",
                    (status, error, int(bool(cancelled)), utcnow(), row["id"]),
                )
        for row in rows:
            payload = dict(row)
            current = self._typed_job(payload["id"]) or {}
            if current.get("cancel_requested"):
                self._mirror(payload["id"], status="cancelled", message="已取消")
                if payload.get("run_id"):
                    from ..agent.store import RunStore
                    RunStore(self.db).update_status(payload["run_id"], "cancelling", stop_reason="cancel_requested")
                self._finalize_cancelled_run(payload.get("run_id"))
            elif payload["job_type"] in _HANDLERS:
                self._reserve(payload["workspace_id"])
                cancel = threading.Event()
                with self._lock:
                    self.cancel_flags[payload["id"]] = cancel
                future = self.executor.submit(self._run_spec, payload["id"], payload["workspace_id"], cancel, True)
                with self._lock:
                    if payload["id"] in self.cancel_flags:
                        self._futures[payload["id"]] = future
            else:
                self._mirror(payload["id"], status="blocked", message="任务处理器不可用", error="handler_unavailable")
        # Recovery never assumes that a disappeared local worker stopped its
        # already-submitted warehouse/Spark work.

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
                if job_type == "analysis_run" and run_id:
                    run = connection.execute("SELECT execution_status,archived_at,workspace_id,session_id FROM agent_runs WHERE id=?", (run_id,)).fetchone()
                    if not run or run["workspace_id"] != workspace_id or run["session_id"] != session_id:
                        raise PermissionError("分析任务不属于当前工作空间或会话")
                    if run["archived_at"] or run["execution_status"] != "queued":
                        raise ValueError("分析任务不在待执行状态")
                    active = connection.execute(
                        "SELECT 1 FROM typed_jobs WHERE run_id=? AND status IN ('queued','running','waiting_external','cancelling') LIMIT 1",
                        (run_id,),
                    ).fetchone()
                    if active:
                        raise ValueError("分析任务已在执行，请等待当前任务收尾")
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
            future = self.executor.submit(self._run_spec, job_id, workspace_id, cancel, False)
            with self._lock:
                if job_id in self.cancel_flags:
                    self._futures[job_id] = future
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
            if cancel.is_set():
                self._finish_typed(job_id, "cancelled", None, "cancel_requested")
                self._mirror(job_id, status="cancelled", message="已取消", finished_at=utcnow())
                with self._lock:
                    self.cancel_flags.pop(job_id, None)
                    self._futures.pop(job_id, None)
                self._release(workspace_id)
                self._finalize_cancelled_run(typed.get("run_id"))
                self._ensure_cancel_reconciler()
                return
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
            def progress(value: float, message: str) -> None:
                current = self._mirror(job_id, progress=max(0, min(100, round(value, 1))), message=message)
                self.db.job_event(job_id, "progress", current or {})

            try:
                result = handler(self.app, typed["spec"], progress, cancel)
                current = self._typed_job(job_id) or {}
                status = "cancelled" if cancel.is_set() or current.get("cancel_requested") or result.get("status") == "cancelled" else "completed"
                self._finish_typed(job_id, status, result, None)
                final = self._mirror(
                    job_id, status=status, progress=100 if status == "completed" else 0,
                    message="执行完成" if status == "completed" else "已取消",
                    result=result, finished_at=utcnow(),
                )
                self.db.job_event(job_id, status, final or {})
                if status == "cancelled":
                    self._finalize_cancelled_run(typed.get("run_id"))
                    self._ensure_cancel_reconciler()
            except Exception as exc:
                current = self._typed_job(job_id) or {}
                if cancel.is_set() or current.get("cancel_requested"):
                    self._finish_typed(job_id, "cancelled", None, "cancel_requested")
                    final = self._mirror(
                        job_id, status="cancelled", progress=0, message="已取消",
                        finished_at=utcnow(),
                    )
                    self.db.job_event(job_id, "cancelled", final or {})
                    self._finalize_cancelled_run(typed.get("run_id"))
                    self._ensure_cancel_reconciler()
                else:
                    self._finish_typed(job_id, "failed", None, type(exc).__name__)
                    final = self._mirror(
                        job_id, status="failed", message="执行失败", error=str(exc),
                        trace=traceback.format_exc(limit=12), finished_at=utcnow(),
                    )
                    self.db.job_event(job_id, "failed", final or {})
            finally:
                with self._lock:
                    self.cancel_flags.pop(job_id, None)
                    self._futures.pop(job_id, None)
                self._release(workspace_id)

    def _finish_typed(self, job_id: str, status: str, result: dict | None, error_code: str | None) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE typed_jobs SET status=?,result=?,error_code=?,lease_owner=NULL,"
                "lease_expires_at=NULL,updated_at=?,finished_at=? WHERE id=?",
                (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
                 error_code, utcnow(), utcnow(), job_id),
            )

    def cancel(self, job_id: str, *, reconcile: bool = True) -> bool:
        typed = self._typed_job(job_id)
        if not typed or typed.get("status") in {"completed", "failed", "cancelled", "blocked"}:
            return False
        if typed.get("run_id"):
            from ..agent.store import RunStore
            run = RunStore(self.db).update_status(typed["run_id"], "cancelling", stop_reason="cancel_requested")
            if run["execution_status"] in {"finished", "failed", "cancelled"}:
                return False
        with self._lock:
            flag = self.cancel_flags.get(job_id)
            if flag:
                flag.set()
            future = self._futures.get(job_id)
            removed_from_queue = bool(future and future.cancel())
            if removed_from_queue:
                # Future.cancel() also succeeds for an already-cancelled future.
                # Remove it while holding the lock so only one caller owns the
                # queued task's completion and capacity release.
                self._futures.pop(job_id, None)
                self.cancel_flags.pop(job_id, None)
            if not typed.get("cancel_requested"):
                with self.db.transaction() as connection:
                    changed = connection.execute(
                        "UPDATE typed_jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelling' ELSE status END,updated_at=? "
                        "WHERE id=? AND cancel_requested=0 AND status IN ('queued','running','waiting_external','cancelling')",
                        (utcnow(), job_id),
                    ).rowcount
                if changed:
                    self.db.patch("jobs", job_id, {"cancel_requested": True, "message": "正在取消"})
                    self.db.job_event(job_id, "cancel_requested", {"id": job_id})
        if removed_from_queue:
            self._finish_typed(job_id, "cancelled", None, "cancel_requested")
            self._mirror(job_id, status="cancelled", message="已取消", finished_at=utcnow())
            self._release(typed["workspace_id"])
            self._finalize_cancelled_run(typed.get("run_id"))
        if reconcile:
            self._ensure_cancel_reconciler()
        return True

    def shutdown(self) -> None:
        self._stop_reconcile.set()
        with self._lock:
            for flag in self.cancel_flags.values():
                flag.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        maintenance = self._reconcile_thread
        if maintenance and maintenance is not threading.current_thread():
            maintenance.join(timeout=3)


def get_job_manager(app: Flask) -> JobManager:
    manager = app.extensions.get("meridian_jobs")
    if manager is None:
        manager = JobManager(app)
        app.extensions["meridian_jobs"] = manager
    return manager
