from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask

from ..core.database import Database, utcnow
from .dashboard_refresh import dashboard_refresh_due, refresh_dashboard
from .jobs import get_job_manager
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
        app = self.app

        def work(progress, cancel):
            with app.app_context():
                try:
                    if cancel.is_set():
                        self.db.patch("dashboards", dashboard["id"], {
                            "refresh_queued_at": None, "refresh_status": "cancelled",
                        })
                        return {"dashboard_id": dashboard["id"], "cancelled": True}
                    progress(10, "正在刷新看板数据")
                    current = self.db.get("dashboards", dashboard["id"])
                    if not current or str(current.get("workspace_id") or "default") != workspace_id:
                        raise FileNotFoundError("看板不存在")
                    refreshed = refresh_dashboard(self.db, current)
                    progress(100, "看板刷新完成")
                    return {"dashboard_id": refreshed["id"], "revision": refreshed["revision"]}
                except Exception as exc:
                    self.db.patch("dashboards", dashboard["id"], {
                        "refresh_queued_at": None, "refresh_status": "error", "refresh_error": str(exc),
                    })
                    raise

        try:
            job = get_job_manager(app).submit(
                workspace_id=workspace_id, session_id=dashboard.get("session_id"),
                kind="dashboard_refresh", title=f"刷新看板：{dashboard.get('name') or dashboard['id']}",
                work=work,
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


def start_scheduler(app: Flask) -> WorkflowScheduler:
    scheduler = WorkflowScheduler(app)
    scheduler.start()
    app.extensions["meridian_scheduler"] = scheduler
    return scheduler
