from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..core.database import Database, utcnow
from .charts import make_spec
from .datasets import execute_query, load_result_frame


def _workspace_record(database: Database, collection: str, record_id: str, workspace_id: str) -> dict:
    record = database.get(collection, record_id)
    if not record or str(record.get("workspace_id") or "default") != workspace_id:
        raise FileNotFoundError(f"{collection} 记录不存在：{record_id}")
    return record


def _fresh_result(
    database: Database, widget: dict, workspace_id: str, actor_id: str,
) -> dict | None:
    stored = None
    if widget.get("result_id"):
        stored = _workspace_record(database, "query_results", str(widget["result_id"]), workspace_id)
    sql = str(widget.get("query") or widget.get("sql") or (stored or {}).get("sql") or "").strip()
    source_ids = widget.get("source_ids")
    if not isinstance(source_ids, list):
        source_ids = [widget["source_id"]] if widget.get("source_id") else (stored or {}).get("source_ids")
    source_ids = [str(value) for value in source_ids or [] if str(value)]
    if not sql or not source_ids:
        return stored
    for source_id in source_ids:
        _workspace_record(database, "sources", source_id, workspace_id)
    return execute_query(source_ids, sql, workspace_id, actor_id=actor_id)


def refresh_widget(
    database: Database, widget: dict, workspace_id: str, actor_id: str = "local-default",
) -> dict:
    result = _fresh_result(database, widget, workspace_id, actor_id)
    refreshed_at = utcnow()
    if not result:
        return {**widget, "refresh_status": "static", "refreshed_at": refreshed_at}
    frame = load_result_frame(result["id"])
    base = {**widget, "result_id": result["id"], "refresh_status": "ready", "refreshed_at": refreshed_at}
    base.pop("refresh_error", None)
    if widget.get("type") == "kpi" or widget.get("chart_type") == "KPI_Card":
        if frame.empty or not len(frame.columns):
            raise ValueError("查询未返回 KPI 数据")
        row = frame.iloc[0]
        raw = row.iloc[0]
        try:
            value = float(raw)
            if abs(value) >= 100_000_000:
                shown = f"{value / 100_000_000:.2f} 亿"
            elif abs(value) >= 10_000:
                shown = f"{value / 10_000:.1f} 万"
            else:
                shown = str(int(value)) if value.is_integer() else f"{value:.2f}"
        except (TypeError, ValueError):
            shown = str(raw)
        trend = None
        if len(row) > 2:
            try:
                trend = round(float(row.iloc[2]), 1)
            except (TypeError, ValueError):
                pass
        return {
            **base, "kpi_value": shown, "kpi_sub": str(row.iloc[1]) if len(row) > 1 else "",
            "kpi_trend": trend,
        }
    old = widget.get("chart", {})
    chart = make_spec(
        frame,
        chart_type=old.get("type") or widget.get("chart_type"),
        title=old.get("title") or widget.get("title", "图表"),
        x=old.get("x"), y=old.get("y"), group=old.get("group"), options=old.get("options"),
    )
    return {**base, "chart": chart}


def refresh_dashboard(
    database: Database, dashboard: dict, actor_id: str | None = None,
) -> dict:
    workspace_id = str(dashboard.get("workspace_id") or "default")
    actor_id = str(actor_id or dashboard.get("owner_id") or "local-default")
    widgets = []
    for widget in dashboard.get("widgets") or []:
        try:
            widgets.append(refresh_widget(database, widget, workspace_id, actor_id))
        except (FileNotFoundError, ValueError, KeyError, TypeError) as exc:
            widgets.append({
                **widget, "refresh_status": "error", "refresh_error": str(exc), "refreshed_at": utcnow(),
            })
    now = utcnow()
    updated = {
        **dashboard, "widgets": widgets, "last_refreshed_at": now, "refreshed_at": now,
        "refresh_queued_at": None, "refresh_status": "ready",
        "revision": int(dashboard.get("revision", 1)) + 1,
    }
    return database.put("dashboards", updated, workspace_id=workspace_id)


def dashboard_refresh_due(dashboard: dict, now: datetime | None = None) -> bool:
    refresh = dashboard.get("refresh") or {}
    if not isinstance(refresh, dict) or not refresh.get("enabled"):
        return False
    try:
        minutes = max(1, min(int(refresh.get("minutes") or 60), 10_080))
    except (TypeError, ValueError):
        return False
    now = now or datetime.now(timezone.utc)
    queued_at = dashboard.get("refresh_queued_at")
    if queued_at:
        try:
            if now - datetime.fromisoformat(str(queued_at)) < timedelta(minutes=max(5, minutes)):
                return False
        except (TypeError, ValueError):
            pass
    previous = dashboard.get("last_refreshed_at") or dashboard.get("refreshed_at")
    if not previous:
        return True
    try:
        return now - datetime.fromisoformat(str(previous)) >= timedelta(minutes=minutes)
    except (TypeError, ValueError):
        return True
