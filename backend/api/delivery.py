from __future__ import annotations

from pathlib import Path

import pandas as pd
from flask import Blueprint, current_app, request, send_file

from ..agent.store import RunStore
from ..services.exports import export_dashboard_html, export_data, export_report
from ..services.dashboard_refresh import refresh_dashboard as refresh_dashboard_record
from ..services.dashboard_refresh import refresh_widget as refresh_widget_record
from ..services.authorization import require_sources_access
from ..services.results.delivery import ARTIFACT_KINDS, generate_artifacts, prepare_eml, send_email
from ..services.results.manifests import ResultService
from .common import (
    api_errors, current_user_id,
    body,
    db,
    ok,
    require_query_result_access,
    require_source_access,
    require_workspace_record,
    safe_child,
    workspace_id,
)


bp = Blueprint("delivery", __name__)


@bp.post("/api/exports/data")
@api_errors
def create_data_export():
    artifact = export_data(body(), workspace_id(), current_user_id())
    return ok(artifact=_public_artifact(artifact)), 201


@bp.post("/api/exports/report")
@api_errors
def create_report_export():
    artifact = export_report(body(), workspace_id(), current_user_id())
    return ok(artifact=_public_artifact(artifact)), 201


def _public_artifact(item: dict) -> dict:
    value = dict(item)
    value.pop("path", None)
    value["download_url"] = f"/api/artifacts/{item['id']}/download"
    return value


def _actor_artifacts(items: list[dict]) -> list[dict]:
    """Hide analysis-owned artifacts from every actor except the run owner."""
    actor_id = current_user_id()
    run_ids = {str(item["run_id"]) for item in items if item.get("run_id")}
    owned = {
        run_id for run_id in run_ids
        if (run := RunStore(db()).get_run(run_id, workspace_id=workspace_id()))
        and run.get("actor_id") == actor_id
    }
    visible = []
    for item in items:
        if item.get("run_id") and str(item["run_id"]) not in owned:
            continue
        try:
            require_sources_access(
                db(), item.get("source_ids") or [],
                workspace_id=item.get("workspace_id", workspace_id()), actor_id=actor_id,
            )
        except (FileNotFoundError, PermissionError):
            continue
        visible.append(item)
    return visible


@bp.get("/api/artifacts")
def list_artifacts():
    items = _actor_artifacts(db().list("artifacts", workspace_id=workspace_id()))
    return ok(items=[_public_artifact(item) for item in items])


@bp.get("/api/artifacts/<artifact_id>/download")
@api_errors
def download_artifact(artifact_id: str):
    item = require_workspace_record("artifacts", artifact_id)
    require_sources_access(
        db(), item.get("source_ids") or [], workspace_id=item["workspace_id"],
        actor_id=current_user_id(), action="export",
    )
    if item.get("run_id"):
        run = RunStore(db()).get_run(str(item["run_id"]), workspace_id=item["workspace_id"])
        if not run or run.get("actor_id") != current_user_id():
            raise FileNotFoundError("成果不存在")
    path = safe_child(current_app.config["SETTINGS"].export_dir, Path(item["path"]))
    if not path.exists():
        raise FileNotFoundError("成果文件已不存在")
    return send_file(path, as_attachment=True, download_name=item["filename"])


def _analysis_run(run_id: str) -> dict:
    run = RunStore(db()).get_run(run_id, workspace_id=workspace_id())
    if not run or run.get("actor_id") != current_user_id():
        raise FileNotFoundError("分析任务不存在")
    require_sources_access(
        db(), run.get("source_scope") or [], workspace_id=run["workspace_id"],
        actor_id=current_user_id(), action="read",
    )
    return run


@bp.get("/api/analyses/<run_id>/results")
@api_errors
def analysis_results(run_id: str):
    run = _analysis_run(run_id)
    service = ResultService(db())
    publication = service.publication(run_id, workspace_id=run["workspace_id"])
    if not publication:
        return ok(status="not_published", publication=None, manifest=None, artifacts=[])
    manifest = service.manifest(publication["manifest_id"], workspace_id=run["workspace_id"])
    artifacts = [
        _public_artifact(item) for item in db().list("artifacts", workspace_id=run["workspace_id"], limit=5000)
        if item.get("manifest_id") == publication["manifest_id"]
    ]
    return ok(status="published", publication=publication, manifest=manifest, artifacts=artifacts)


@bp.get("/api/analyses/<run_id>/details")
@api_errors
def analysis_details(run_id: str):
    run = _analysis_run(run_id)
    publication = ResultService(db()).publication(run_id, workspace_id=run["workspace_id"])
    if not publication:
        raise PermissionError("结果未通过发布门禁")
    manifest = ResultService(db()).manifest(publication["manifest_id"], workspace_id=run["workspace_id"])
    table = next(iter((manifest or {}).get("payload", {}).get("tables") or []), {})
    result = db().get("query_results", str(table.get("result_id") or ""), workspace_id=run["workspace_id"])
    if not result:
        return ok(items=[], columns=table.get("columns") or [], total=None, completeness="unknown", next_cursor=None)
    page_size = max(1, min(int(request.args.get("limit", 100)), 500))
    cursor = max(0, int(request.args.get("cursor", 0)))
    path = safe_child(current_app.config["SETTINGS"].export_dir, Path(result["path"]))
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("明细结果超过本地分页上限，应通过远程 DatasetRef 分页")
    frame = pd.read_csv(path, skiprows=range(1, cursor + 1), nrows=page_size)
    items = frame.where(pd.notna(frame), None).to_dict(orient="records")
    next_cursor = cursor + len(items) if cursor + len(items) < int(result.get("rows") or 0) else None
    return ok(
        items=items, columns=result.get("columns") or [], total=result.get("total_rows"),
        returned_total=result.get("rows"), completeness=result.get("completeness"), next_cursor=next_cursor,
    )


@bp.post("/api/analyses/<run_id>/artifacts")
@api_errors
def create_analysis_artifacts(run_id: str):
    run = _analysis_run(run_id)
    kinds = body().get("kinds")
    if kinds is not None and (not isinstance(kinds, list) or any(str(item) not in ARTIFACT_KINDS for item in kinds)):
        raise ValueError("成果 kinds 无效")
    items = generate_artifacts(db(), run_id, run["workspace_id"], [str(item) for item in kinds] if kinds else None)
    return ok(items=[_public_artifact(item) for item in items]), 201


def _email_payload(run: dict) -> tuple[dict, str, str, list[str] | None]:
    payload = body()
    publication = ResultService(db()).publication(run["id"], workspace_id=run["workspace_id"])
    if not publication:
        raise PermissionError("成果未通过发布门禁")
    manifest = ResultService(db()).manifest(publication["manifest_id"], workspace_id=run["workspace_id"])
    subject = str(payload.get("subject") or f"数据分析成果 · {run['id']}")
    text = str(payload.get("body") or (manifest or {}).get("payload", {}).get("summary") or "")
    kinds = payload.get("kinds")
    if kinds is not None and (not isinstance(kinds, list) or any(str(item) not in ARTIFACT_KINDS for item in kinds)):
        raise ValueError("附件 kinds 无效")
    return payload, subject, text, [str(item) for item in kinds] if kinds else None


@bp.post("/api/analyses/<run_id>/email/eml")
@api_errors
def create_analysis_eml(run_id: str):
    run = _analysis_run(run_id)
    payload, subject, text, kinds = _email_payload(run)
    eml, artifacts = prepare_eml(
        db(), run_id, run["workspace_id"], recipients=payload.get("recipients") or "",
        subject=subject, text=text, kinds=kinds,
    )
    return ok(
        eml=_public_artifact(eml), attachments=[_public_artifact(item) for item in artifacts],
        compatibility_note=".eml 包含真实 MIME 附件；mailto 仅可作为不含附件的文本回退。",
    ), 201


@bp.post("/api/analyses/<run_id>/email/send")
@api_errors
def send_analysis_email(run_id: str):
    run = _analysis_run(run_id)
    payload, subject, text, kinds = _email_payload(run)
    connector = require_workspace_record("connectors", str(payload.get("connector_id") or ""), run["workspace_id"])
    delivery = send_email(
        db(), run_id, run["workspace_id"], connector=connector,
        recipients=payload.get("recipients") or "", subject=subject, text=text, kinds=kinds,
        idempotency_key=str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or ""),
    )
    return ok(delivery=delivery)


@bp.delete("/api/artifacts/<artifact_id>")
@api_errors
def archive_artifact(artifact_id: str):
    item = require_workspace_record("artifacts", artifact_id)
    require_sources_access(
        db(), item.get("source_ids") or [], workspace_id=item["workspace_id"],
        actor_id=current_user_id(), action="delete",
    )
    if item.get("run_id"):
        _analysis_run(str(item["run_id"]))
    if not db().archive("artifacts", artifact_id):
        raise FileNotFoundError("成果不存在")
    return ok(archived=True)


def _dashboard_source_ids(dashboard: dict) -> list[str]:
    source_ids: list[str] = []
    for widget in dashboard.get("widgets") or []:
        raw = widget.get("source_ids")
        if not isinstance(raw, list):
            raw = [widget["source_id"]] if widget.get("source_id") else []
        source_ids.extend(str(value) for value in raw if str(value))
        result_id = str(widget.get("result_id") or "")
        if not result_id and widget.get("chart_id"):
            chart = db().get(
                "charts", str(widget["chart_id"]), workspace_id=dashboard["workspace_id"],
            ) or {}
            result_id = str(chart.get("result_id") or "")
        if result_id:
            result = db().get(
                "query_results", result_id, workspace_id=dashboard["workspace_id"],
            ) or {}
            source_ids.extend(str(value) for value in result.get("source_ids") or [])
    return list(dict.fromkeys(source_ids))


def _require_dashboard_access(dashboard: dict, *, action: str = "read") -> dict:
    require_sources_access(
        db(), _dashboard_source_ids(dashboard), workspace_id=dashboard["workspace_id"],
        actor_id=current_user_id(), action=action,
    )
    return dashboard


@bp.get("/api/dashboards")
def dashboards():
    items = []
    for item in db().list("dashboards", workspace_id=workspace_id()):
        try:
            items.append(_require_dashboard_access(item))
        except (FileNotFoundError, PermissionError):
            continue
    return ok(items=items)


def _normalize_refresh(value) -> dict:
    if value is None:
        return {"enabled": False, "minutes": 60}
    if not isinstance(value, dict):
        raise ValueError("看板刷新配置必须是对象")
    try:
        minutes = int(value.get("minutes", 60))
    except (TypeError, ValueError) as exc:
        raise ValueError("看板刷新周期必须是整数") from exc
    if not 1 <= minutes <= 10_080:
        raise ValueError("看板刷新周期必须在 1-10080 分钟之间")
    return {"enabled": bool(value.get("enabled", False)), "minutes": minutes}


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
            require_query_result_access(str(widget["result_id"]), wid)
        if widget.get("source_id"):
            require_source_access(str(widget["source_id"]), wid)
        if widget.get("source_ids"):
            if not isinstance(widget["source_ids"], list):
                raise ValueError("看板组件 source_ids 必须是数组")
            for source_id in widget["source_ids"]:
                require_source_access(str(source_id), wid)
        if widget.get("chart_id"):
            chart = require_workspace_record("charts", str(widget["chart_id"]), wid)
            if chart.get("result_id"):
                require_query_result_access(str(chart["result_id"]), wid)
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
            "refresh": _normalize_refresh(payload.get("refresh")),
            "owner_id": current_user_id(),
            "revision": 1,
        },
        workspace_id=wid,
    )
    return ok(item=item), 201


@bp.get("/api/dashboards/<dashboard_id>")
@api_errors
def dashboard(dashboard_id: str):
    return ok(item=_require_dashboard_access(require_workspace_record("dashboards", dashboard_id)))


@bp.put("/api/dashboards/<dashboard_id>")
@api_errors
def update_dashboard(dashboard_id: str):
    current = _require_dashboard_access(
        require_workspace_record("dashboards", dashboard_id), action="update",
    )
    payload = body()
    expected = payload.pop("expected_revision", None)
    if expected is not None and int(expected) != int(current.get("revision", 1)):
        raise ValueError("看板已被其他修改，请刷新后重试")
    allowed = {"name", "description", "widgets", "layout", "refresh"}
    changes = {key: value for key, value in payload.items() if key in allowed}
    if "widgets" in changes:
        changes["widgets"] = _normalize_widgets(changes["widgets"], current["workspace_id"])
    if "refresh" in changes:
        changes["refresh"] = _normalize_refresh(changes["refresh"])
    updated = {**current, **changes, "id": dashboard_id, "revision": int(current.get("revision", 1)) + 1}
    return ok(item=db().put("dashboards", updated, workspace_id=current.get("workspace_id", "default")))


@bp.delete("/api/dashboards/<dashboard_id>")
@api_errors
def archive_dashboard(dashboard_id: str):
    _require_dashboard_access(require_workspace_record("dashboards", dashboard_id), action="delete")
    if not db().archive("dashboards", dashboard_id):
        raise FileNotFoundError("看板不存在")
    return ok(archived=True)


@bp.post("/api/dashboards/<dashboard_id>/export")
@api_errors
def dashboard_export(dashboard_id: str):
    dashboard = _require_dashboard_access(
        require_workspace_record("dashboards", dashboard_id), action="export",
    )
    payload = {**dashboard, "source_ids": _dashboard_source_ids(dashboard)}
    return ok(artifact=_public_artifact(export_dashboard_html(payload, dashboard.get("workspace_id", "default"))))


@bp.post("/api/dashboards/<dashboard_id>/refresh")
@api_errors
def refresh_dashboard(dashboard_id: str):
    dashboard = _require_dashboard_access(
        require_workspace_record("dashboards", dashboard_id), action="query",
    )
    item = refresh_dashboard_record(db(), dashboard, current_user_id())
    return ok(item=item)


@bp.post("/api/dashboards/<dashboard_id>/widgets/<widget_id>/refresh")
@api_errors
def refresh_dashboard_widget(dashboard_id: str, widget_id: str):
    dashboard = _require_dashboard_access(
        require_workspace_record("dashboards", dashboard_id), action="query",
    )
    found = False
    widgets = []
    for widget in dashboard.get("widgets", []):
        if widget.get("id") == widget_id:
            widget = refresh_widget_record(
                db(), widget, str(dashboard.get("workspace_id") or "default"), current_user_id(),
            )
            found = True
        widgets.append(widget)
    if not found:
        raise FileNotFoundError("看板组件不存在")
    dashboard["widgets"] = widgets
    dashboard["revision"] = int(dashboard.get("revision", 1)) + 1
    item = db().put("dashboards", dashboard, workspace_id=dashboard.get("workspace_id", "default"))
    return ok(item=item, widget=next(item for item in widgets if item.get("id") == widget_id))
