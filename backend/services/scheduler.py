from __future__ import annotations

import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask

from ..core.database import Database, utcnow
from .workflows import start_workflow


def _field_matches(expression: str, value: int, minimum: int, maximum: int) -> bool:
    def atom_matches(atom: str) -> bool:
        if atom == "*":
            return True
        if atom.startswith("*/"):
            return value % max(1, int(atom[2:])) == 0
        if "-" in atom:
            start, end = atom.split("-", 1)
            return int(start) <= value <= int(end)
        return int(atom) == value
    try:
        return any(atom_matches(atom.strip()) for atom in expression.split(",")) and minimum <= value <= maximum
    except (TypeError, ValueError):
        return False


def cron_matches(expression: str, moment: datetime) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        return False
    values = [moment.minute, moment.hour, moment.day, moment.month, (moment.weekday() + 1) % 7]
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    return all(_field_matches(field, value, *bounds) for field, value, bounds in zip(fields, values, ranges))


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

    def _loop(self) -> None:
        while not self._stop.wait(20):
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
                    if not workflow:
                        continue
                    executable = {**workflow, "definition": workflow.get("published_definition") or workflow.get("definition", {})}
                    try:
                        run = start_workflow(executable, schedule.get("inputs", {}))
                        schedule.update({"last_run_at": utcnow(), "last_run_id": run["id"], "last_minute_key": minute_key, "last_error": None})
                    except Exception as exc:
                        schedule.update({"last_run_at": utcnow(), "last_minute_key": minute_key, "last_error": str(exc)})
                    self.db.put("schedules", schedule, workspace_id=schedule.get("workspace_id", "default"))


def start_scheduler(app: Flask) -> WorkflowScheduler:
    scheduler = WorkflowScheduler(app)
    scheduler.start()
    app.extensions["meridian_scheduler"] = scheduler
    return scheduler

