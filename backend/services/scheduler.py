from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask

from ..core.database import Database, utcnow
from .dashboard_refresh import dashboard_refresh_due, refresh_dashboard
from .jobs import get_job_manager, register_job_handler
from .workflows import start_workflow


def _field_values(expression: str, minimum: int, maximum: int, *, day_of_week: bool = False) -> set[int]:
    values: set[int] = set()
    for raw_atom in expression.split(","):
        atom = raw_atom.strip()
        if not atom:
            raise ValueError("cron 字段不能为空")
        base, separator, raw_step = atom.partition("/")
        step = int(raw_step) if separator else 1
        if step < 1:
            raise ValueError("cron 步长必须大于 0")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            raw_start, raw_end = base.split("-", 1)
            start, end = int(raw_start), int(raw_end)
        else:
            if separator:
                raise ValueError("cron 单值不能使用步长")
            start = end = int(base)
        if start < minimum or end > maximum or start > end:
            raise ValueError("cron 字段超出取值范围")
        values.update(range(start, end + 1, step))
    if day_of_week and 7 in values:
        values.remove(7)
        values.add(0)
    return values


def validate_cron(expression: str) -> str:
    fields = str(expression or "").split()
    if len(fields) != 5:
        raise ValueError("cron 表达式必须包含 5 个字段")
    ranges = [(0, 59, False), (0, 23, False), (1, 31, False), (1, 12, False), (0, 7, True)]
    for field, (minimum, maximum, day_of_week) in zip(fields, ranges):
        _field_values(field, minimum, maximum, day_of_week=day_of_week)
    return " ".join(fields)


def cron_matches(expression: str, moment: datetime) -> bool:
    try:
        fields = validate_cron(expression).split()
        minute = moment.minute in _field_values(fields[0], 0, 59)
        hour = moment.hour in _field_values(fields[1], 0, 23)
        month = moment.month in _field_values(fields[3], 1, 12)
        day_of_month = moment.day in _field_values(fields[2], 1, 31)
        day_of_week = (moment.weekday() + 1) % 7 in _field_values(fields[4], 0, 7, day_of_week=True)
    except (TypeError, ValueError):
        return False
    if not minute or not hour or not month:
        return False
    day_matches = (
        day_of_month and day_of_week if fields[2] == "*" or fields[4] == "*"
        else day_of_month or day_of_week
    )
    return day_matches


class WorkflowScheduler:
    def __init__(self, app: Flask):
        self.app = app
        self.db: Database = app.extensions["meridian_db"]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="meridian-scheduler", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=5)

    def _queue_dashboard_refresh(self, dashboard: dict) -> None:
        workspace_id = str(dashboard.get("workspace_id") or "default")
        queued_at = utcnow()
        self.db.patch("dashboards", dashboard["id"], {
            "refresh_queued_at": queued_at, "refresh_status": "queued", "refresh_error": None,
        })
        try:
            job = get_job_manager(self.app).submit_spec(
                workspace_id=workspace_id, session_id=dashboard.get("session_id"),
                job_type="dashboard_refresh", title=f"刷新看板：{dashboard.get('name') or dashboard['id']}",
                spec={"dashboard_id": dashboard["id"], "workspace_id": workspace_id},
            )
            self.db.patch("dashboards", dashboard["id"], {"refresh_job_id": job["id"]})
        except Exception as exc:
            self.db.patch("dashboards", dashboard["id"], {
                "refresh_queued_at": None, "refresh_status": "error", "refresh_error": str(exc),
            })

    def _loop(self) -> None:
        while not self._stop.wait(20):
            try:
                self._run_once()
            except Exception:
                # A transient database/filesystem problem must not permanently
                # kill the only scheduler thread in a single-node deployment.
                logging.getLogger(__name__).exception("scheduler_iteration_failed")

    def _run_once(self) -> None:
        with self.app.app_context():
            self._reconcile_warehouse_queries()
            self._reconcile_spark_batches()
            for schedule in self.db.list("schedules"):
                if not schedule.get("enabled", True):
                    continue
                try:
                    now = datetime.now(ZoneInfo(schedule.get("timezone") or "UTC"))
                except Exception:
                    now = datetime.now(ZoneInfo("UTC"))
                minute_key = now.strftime("%Y-%m-%dT%H:%M")
                if schedule.get("last_minute_key") == minute_key or not cron_matches(schedule.get("cron", ""), now):
                    continue
                workflow = self.db.get("workflows", schedule.get("workflow_id", ""))
                if (
                    not workflow or workflow.get("workspace_id") != schedule.get("workspace_id")
                    or workflow.get("status") != "published" or not workflow.get("published_definition")
                ):
                    continue
                executable = {**workflow, "definition": workflow["published_definition"]}
                try:
                    run = start_workflow(
                        executable, schedule.get("inputs", {}),
                        idempotency_key=f"schedule:{schedule['id']}:{minute_key}",
                        actor_id=str(schedule.get("actor_id") or "local-default"),
                    )
                    schedule.update({
                        "last_run_at": utcnow(), "last_run_id": run["id"],
                        "last_minute_key": minute_key, "last_error": None,
                    })
                except Exception as exc:
                    schedule.update({
                        "last_run_at": utcnow(), "last_minute_key": minute_key,
                        "last_error": str(exc),
                    })
                self.db.put("schedules", schedule, workspace_id=schedule.get("workspace_id", "default"))
            now_utc = datetime.now(timezone.utc)
            for dashboard in self.db.list("dashboards", limit=5000):
                if dashboard_refresh_due(dashboard, now_utc):
                    self._queue_dashboard_refresh(dashboard)

    def _reconcile_warehouse_queries(self) -> None:
        from ..agent.store import RunStore
        from .advanced_agent import materialize_trino_preview
        from .data_plane.factory import trino_adapter

        store = RunStore(self.db)
        for query in self.db.list("warehouse_queries", limit=5000):
            if query.get("status") not in {"running", "cancelling"}:
                continue
            run = store.get_run(str(query.get("run_id") or ""), workspace_id=query.get("workspace_id"))
            if not run:
                continue
            adapter = trino_adapter(self.db, run["workspace_id"], str(query["engine_id"]))
            try:
                if run["execution_status"] in {"cancelling", "cancelled"} and query.get("status") == "running":
                    adapter.cancel(query["id"])
                    continue
                current = adapter.poll(query["id"])
                if current.get("status") not in {"finished", "failed"}:
                    continue
                if current.get("status") == "failed":
                    store.complete_external_action(
                        run["id"], query["id"], status="failed",
                        result={"tool": "warehouse_query", "ok": False, "error_code": "warehouse_query_failed"},
                        error_code="warehouse_query_failed",
                    )
                else:
                    try:
                        remote_ref = adapter.result_ref(
                            query["id"], owner_id=run["actor_id"], contract_version=run["contract_version"],
                            policy_version=run["policy_version"],
                        )
                        result, bounded_ref = (None, None)
                        if query.get("result_mode") != "materialize":
                            result, bounded_ref = materialize_trino_preview(
                                self.db, run, current, source_ids=list(query.get("source_refs") or []),
                            )
                        refs = [remote_ref.ref_id, *([bounded_ref.ref_id] if bounded_ref else [])]
                        value = {
                            "status": "SUCCEEDED", "query_id": query["id"], "job_id": query["id"],
                            "dataset_ref_id": (bounded_ref or remote_ref).ref_id,
                            "result_id": result.get("id") if result else None,
                            "output_refs": refs, "completeness": "complete", "accuracy": "exact",
                        }
                        store.complete_external_action(
                            run["id"], query["id"], status="succeeded",
                            result={
                                "tool": "warehouse_query", "value": value,
                                "tool_result": {
                                    "output_refs": refs, "completeness": "complete",
                                    "validation_status": "not_evaluated",
                                },
                            },
                        )
                    except ValueError as exc:
                        store.complete_external_action(
                            run["id"], query["id"], status="failed",
                            result={
                                "tool": "warehouse_query", "ok": False,
                                "error": str(exc), "error_code": "warehouse_result_not_durable",
                            },
                            error_code="warehouse_result_not_durable",
                        )
                if not any(
                    item.get("run_id") == run["id"] and item.get("status") in {"queued", "running"}
                    for item in self.db.list("jobs", workspace_id=run["workspace_id"], limit=5000)
                ):
                    get_job_manager(self.app).submit_spec(
                        workspace_id=run["workspace_id"], session_id=run["session_id"],
                        job_type="analysis_run", title="继续远程查询后分析",
                        spec={"run_id": run["id"]}, run_id=run["id"],
                    )
            except Exception as exc:
                self.db.patch(
                    "warehouse_queries", query["id"],
                    {"reconcile_error": str(exc), "reconcile_error_at": utcnow()},
                    workspace_id=run["workspace_id"],
                )

    def _reconcile_spark_batches(self) -> None:
        from ..agent.store import RunStore
        from .data_plane.factory import livy_adapter

        store = RunStore(self.db)
        for batch in self.db.list("remote_batches", limit=5000):
            if batch.get("state") in {"success", "dead", "error", "killed"} and batch.get("reconciled_at"):
                continue
            run = store.get_run(str(batch.get("run_id") or ""), workspace_id=batch.get("workspace_id"))
            if not run:
                continue
            adapter = livy_adapter(self.db, run["workspace_id"], str(batch["engine_id"]))
            try:
                if run["execution_status"] in {"cancelling", "cancelled"} and batch.get("state") not in {
                    "success", "dead", "error", "killed", "cancelling",
                }:
                    adapter.cancel(batch["id"])
                    continue
                current = adapter.reconcile(batch["id"])
                state = str(current.get("state") or "unknown")
                if state not in {"success", "dead", "error", "killed", "unknown"}:
                    continue
                if state == "success":
                    manifest = adapter.result_manifest(batch["id"])
                    ref = adapter.result_ref(
                        batch["id"], owner_id=run["actor_id"],
                        contract_version=run["contract_version"], policy_version=run["policy_version"],
                        manifest=manifest,
                    )
                    value = {
                        "status": "SUCCEEDED", "job_id": batch["id"],
                        "dataset_ref_id": ref.ref_id, "output_refs": [ref.ref_id],
                        "completeness": ref.result_completeness, "accuracy": ref.accuracy,
                        "provenance_ref": ref.provenance_ref,
                    }
                    store.complete_external_action(
                        run["id"], batch["id"], status="succeeded",
                        result={
                            "tool": "warehouse_spark_submit", "value": value,
                            "tool_result": {
                                "output_refs": [ref.ref_id], "completeness": ref.result_completeness,
                                "validation_status": "not_evaluated", "preview": manifest.get("preview"),
                            },
                        },
                    )
                else:
                    status = "cancelled" if state == "killed" and run["execution_status"] == "cancelling" else (
                        "unknown" if state == "unknown" else "failed"
                    )
                    store.complete_external_action(
                        run["id"], batch["id"], status=status,
                        result={
                            "tool": "warehouse_spark_submit", "ok": False,
                            "error_code": f"spark_{state}", "state": state,
                        }, error_code=f"spark_{state}",
                    )
                self.db.patch(
                    "remote_batches", batch["id"], {"reconciled_at": utcnow()},
                    workspace_id=run["workspace_id"],
                )
                if state != "unknown" and not any(
                    item.get("run_id") == run["id"] and item.get("status") in {"queued", "running"}
                    for item in self.db.list("jobs", workspace_id=run["workspace_id"], limit=5000)
                ):
                    get_job_manager(self.app).submit_spec(
                        workspace_id=run["workspace_id"], session_id=run["session_id"],
                        job_type="analysis_run", title="继续 Spark 作业后分析",
                        spec={"run_id": run["id"]}, run_id=run["id"],
                    )
            except Exception as exc:
                self.db.patch(
                    "remote_batches", batch["id"],
                    {"reconcile_error": str(exc), "reconcile_error_at": utcnow()},
                    workspace_id=run["workspace_id"],
                )


def _dashboard_refresh_handler(app, spec, progress, cancel):
    database: Database = app.extensions["meridian_db"]
    dashboard_id = str(spec.get("dashboard_id") or "")
    workspace_id = str(spec.get("workspace_id") or "default")
    if cancel.is_set():
        database.patch("dashboards", dashboard_id, {
            "refresh_queued_at": None, "refresh_status": "cancelled",
        }, workspace_id=workspace_id)
        return {"dashboard_id": dashboard_id, "cancelled": True}
    progress(10, "正在刷新看板数据")
    current = database.get("dashboards", dashboard_id, workspace_id=workspace_id)
    if not current:
        raise FileNotFoundError("看板不存在")
    try:
        refreshed = refresh_dashboard(database, current)
    except Exception as exc:
        database.patch("dashboards", dashboard_id, {
            "refresh_queued_at": None, "refresh_status": "error", "refresh_error": str(exc),
        }, workspace_id=workspace_id)
        raise
    progress(100, "看板刷新完成")
    return {"dashboard_id": refreshed["id"], "revision": refreshed["revision"]}


register_job_handler("dashboard_refresh", _dashboard_refresh_handler)


def start_scheduler(app: Flask) -> WorkflowScheduler:
    scheduler = WorkflowScheduler(app)
    scheduler.start()
    app.extensions["meridian_scheduler"] = scheduler
    return scheduler
