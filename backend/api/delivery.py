from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, send_file

from ..services.exports import export_dashboard_html, export_data, export_report
from ..services.charts import make_spec
from ..services.datasets import load_result_frame
from .common import (
    api_errors,
    body,
    db,
    ok,
    require_workspace_record,
    safe_child,
    workspace_id,
)


bp = Blueprint("delivery", __name__)


@bp.post("/api/exports/data")
@api_errors
def create_data_export():
    artifact = export_data(body(), workspace_id())
    return ok(artifact=_public_artifact(artifact)), 201


@bp.post("/api/exports/report")
@api_errors
def create_report_export():
    artifact = export_report(body(), workspace_id())
    return ok(artifact=_public_artifact(artifact)), 201


def _public_artifact(item: dict) -> dict:
    value = dict(item)
    value.pop("path", None)
    value["download_url"] = f"/api/artifacts/{item['id']}/download"
    return value


@bp.get("/api/artifacts")
def list_artifacts():
    return ok(items=[_public_artifact(item) for item in db().list("artifacts", workspace_id=workspace_id())])


@bp.get("/api/artifacts/<artifact_id>/download")
@api_errors
def download_artifact(artifact_id: str):
    item = require_workspace_record("artifacts", artifact_id)
    path = safe_child(current_app.config["SETTINGS"].export_dir, Path(item["path"]))
    if not path.exists():
        raise FileNotFoundError("成果文件已不存在")
    return send_file(path, as_attachment=True, download_name=item["filename"])


@bp.delete("/api/artifacts/<artifact_id>")
@api_errors
def archive_artifact(artifact_id: str):
    require_workspace_record("artifacts", artifact_id)
    if not db().archive("artifacts", artifact_id):
        raise FileNotFoundError("成果不存在")
    return ok(archived=True)


@bp.get("/api/dashboards")
def dashboards():
    return ok(items=db().list("dashboards", workspace_id=workspace_id()))


def _normalize_widgets(value, wid: str) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("看板组件必须是数组")
    if len(value) > 50:
        raise ValueError("单个看板最多包含 50 个组件")
    result = []
    identifiers: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index + 1} 个看板组件格式无效")
        widget = dict(raw)
        identifier = str(widget.get("id") or db().new_id("widget"))[:100]
        if identifier in identifiers:
            raise ValueError(f"看板组件 ID 重复：{identifier}")
        identifiers.add(identifier)
        widget["id"] = identifier
        widget["title"] = str(widget.get("title") or f"组件 {index + 1}")[:100]
        if widget.get("result_id"):
            require_workspace_record("query_results", str(widget["result_id"]), wid)
        if widget.get("source_id"):
            require_workspace_record("sources", str(widget["source_id"]), wid)
        if widget.get("chart_id"):
            chart = require_workspace_record("charts", str(widget["chart_id"]), wid)
            widget.setdefault("chart", chart.get("spec", {}))
        if "chart" in widget and not isinstance(widget["chart"], dict):
            raise ValueError(f"组件 {identifier} 的图表规格无效")
        result.append(widget)
    return result


@bp.post("/api/dashboards")
@api_errors
def create_dashboard():
    payload = body()
    if not str(payload.get("name") or "").strip():
        raise ValueError("看板名称不能为空")
    wid = workspace_id()
    item = db().put(
        "dashboards",
        {
            "id": db().new_id("dash"), "workspace_id": wid,
            "name": str(payload["name"])[:100], "description": str(payload.get("description") or "")[:500],
            "widgets": _normalize_widgets(payload.get("widgets", []), wid),
            "layout": payload.get("layout", {"columns": 12}),
            "refresh": payload.get("refresh", {"enabled": False, "minutes": 60}),
            "revision": 1,
        },
        workspace_id=wid,
    )
    return ok(item=item), 201


@bp.get("/api/dashboards/<dashboard_id>")
@api_errors
def dashboard(dashboard_id: str):
    return ok(item=require_workspace_record("dashboards", dashboard_id))


@bp.put("/api/dashboards/<dashboard_id>")
@api_errors
def update_dashboard(dashboard_id: str):
    current = require_workspace_record("dashboards", dashboard_id)
    payload = body()
    expected = payload.pop("expected_revision", None)
    if expected is not None and int(expected) != int(current.get("revision", 1)):
        raise ValueError("看板已被其他修改，请刷新后重试")
    allowed = {"name", "description", "widgets", "layout", "refresh"}
    changes = {key: value for key, value in payload.items() if key in allowed}
    if "widgets" in changes:
        changes["widgets"] = _normalize_widgets(changes["widgets"], current["workspace_id"])
    updated = {**current, **changes, "id": dashboard_id, "revision": int(current.get("revision", 1)) + 1}
    return ok(item=db().put("dashboards", updated, workspace_id=current.get("workspace_id", "default")))


@bp.delete("/api/dashboards/<dashboard_id>")
@api_errors
def archive_dashboard(dashboard_id: str):
    require_workspace_record("dashboards", dashboard_id)
    if not db().archive("dashboards", dashboard_id):
        raise FileNotFoundError("看板不存在")
    return ok(archived=True)


@bp.post("/api/dashboards/<dashboard_id>/export")
@api_errors
def dashboard_export(dashboard_id: str):
    dashboard = require_workspace_record("dashboards", dashboard_id)
    return ok(artifact=_public_artifact(export_dashboard_html(dashboard, dashboard.get("workspace_id", "default"))))


def _refresh_widget(widget: dict) -> dict:
    result_id = widget.get("result_id")
    if not result_id:
        return {**widget, "refresh_status": "static", "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    result = require_workspace_record("query_results", str(result_id))
    frame = load_result_frame(result["id"])
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
            **widget, "kpi_value": shown, "kpi_sub": str(row.iloc[1]) if len(row) > 1 else "",
            "kpi_trend": trend, "refresh_status": "ready",
            "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    old = widget.get("chart", {})
    chart = make_spec(
        frame,
        chart_type=old.get("type"),
        title=old.get("title") or widget.get("title", "图表"),
        x=old.get("x"), y=old.get("y"), group=old.get("group"), options=old.get("options"),
    )
    return {**widget, "chart": chart, "refresh_status": "ready", "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


@bp.post("/api/dashboards/<dashboard_id>/refresh")
@api_errors
def refresh_dashboard(dashboard_id: str):
    dashboard = require_workspace_record("dashboards", dashboard_id)
    widgets = []
    for widget in dashboard.get("widgets", []):
        try:
            widgets.append(_refresh_widget(widget))
        except (FileNotFoundError, ValueError, KeyError, TypeError) as exc:
            widgets.append({
                **widget, "refresh_status": "error", "refresh_error": str(exc),
                "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
    dashboard["widgets"] = widgets
    dashboard["revision"] = int(dashboard.get("revision", 1)) + 1
    item = db().put("dashboards", dashboard, workspace_id=dashboard.get("workspace_id", "default"))
    return ok(item=item)


@bp.post("/api/dashboards/<dashboard_id>/widgets/<widget_id>/refresh")
@api_errors
def refresh_dashboard_widget(dashboard_id: str, widget_id: str):
    dashboard = require_workspace_record("dashboards", dashboard_id)
    found = False
    widgets = []
    for widget in dashboard.get("widgets", []):
        if widget.get("id") == widget_id:
            widget = _refresh_widget(widget)
            found = True
        widgets.append(widget)
    if not found:
        raise FileNotFoundError("看板组件不存在")
    dashboard["widgets"] = widgets
    dashboard["revision"] = int(dashboard.get("revision", 1)) + 1
    item = db().put("dashboards", dashboard, workspace_id=dashboard.get("workspace_id", "default"))
    return ok(item=item, widget=next(item for item in widgets if item.get("id") == widget_id))
