from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from ..core.database import utcnow
from ..services.datasets import (
    preview_source,
    public_source,
    register_database,
    register_google_sheet,
    register_http,
    register_upload,
)
from ..services.jobs import get_job_manager
from ..services.security import SecretVault
from .common import api_errors, body, db, require_workspace_record, workspace_id


bp = Blueprint("datasource_compat", __name__)
EXACT_UPLOAD_EXTENSIONS = {".xlsx", ".xls", ".csv"}


def _session(sid: str) -> dict:
    return require_workspace_record("sessions", sid)


def _source_schema_preview(source: dict) -> str:
    parts = []
    for table in source.get("tables") or []:
        columns = table.get("schema") or table.get("columns") or []
        if isinstance(columns, list):
            column_text = ", ".join(str(item.get("name") if isinstance(item, dict) else item) for item in columns)
            column_count = len(columns)
        else:
            column_text = ""
            column_count = int(columns or 0)
        rows = table.get("rows")
        parts.append(
            f"Table: {table.get('name')}  ({rows if rows is not None else '?'} rows)  "
            f"[{column_text or f'{column_count} columns'}]"
        )
    return "\n\n".join(parts) or "No tables found."


def _session_sources(session: dict) -> list[dict]:
    active = {str(value) for value in session.get("source_ids") or []}
    attached = [str(value) for value in session.get("attached_source_ids") or session.get("source_ids") or []]
    result = []
    for source_id in attached:
        source = db().get("sources", source_id)
        if not source or source.get("workspace_id", "default") != session.get("workspace_id", "default"):
            continue
        item = public_source(source)
        result.append({
            **item,
            "source_id": source_id,
            "source_name": source.get("name", ""),
            "active": source_id in active,
        })
    return result


def _attach_source(session: dict, source_id: str, *, active: bool = True) -> dict:
    attached = [str(value) for value in session.get("attached_source_ids") or session.get("source_ids") or []]
    active_ids = [str(value) for value in session.get("source_ids") or []]
    if source_id not in attached:
        attached.append(source_id)
    if active and source_id not in active_ids:
        active_ids.append(source_id)
    return db().patch(
        "sessions", session["id"], {"attached_source_ids": attached, "source_ids": active_ids},
    ) or session


def _replace_sources(session: dict, attached: list[str], active: list[str]) -> dict:
    return db().patch(
        "sessions", session["id"],
        {"attached_source_ids": attached, "source_ids": active},
    ) or session


def _config_id(wid: str, ds_type: str) -> str:
    return f"{wid}:{ds_type}"


def _save_config(wid: str, ds_type: str, config: dict) -> None:
    public = dict(config)
    sensitive = {"sql": "connection_string", "gsheets": "creds_json", "api": "auth_value"}.get(ds_type)
    secret = {}
    if sensitive and sensitive in public:
        secret[sensitive] = public.pop(sensitive)
    db().put(
        "datasource_configs",
        {
            "id": _config_id(wid, ds_type), "workspace_id": wid, "type": ds_type,
            "config": public,
            "credential": SecretVault(current_app.config["SECRET_KEY"]).seal(secret),
        },
        workspace_id=wid,
    )


def _load_config(wid: str, ds_type: str) -> dict:
    record = db().get("datasource_configs", _config_id(wid, ds_type)) or {}
    secret = SecretVault(current_app.config["SECRET_KEY"]).open(record.get("credential", ""), {})
    return {**(record.get("config") or {}), **secret}


def _public_configs(wid: str) -> dict:
    result = {}
    sensitive = {"sql": "connection_string", "gsheets": "creds_json", "api": "auth_value"}
    for record in db().list("datasource_configs", workspace_id=wid):
        ds_type = record.get("type")
        value = dict(record.get("config") or {})
        secret = SecretVault(current_app.config["SECRET_KEY"]).open(record.get("credential", ""), {})
        key = sensitive.get(ds_type)
        if key:
            value[f"has_{key}"] = bool(secret.get(key))
        result[ds_type] = value
    return result


def _warehouse_public(record: dict) -> dict:
    snapshots = record.get("sources") or []
    return {
        "filename": record["id"], "name": record.get("name", ""),
        "saved_at": record.get("saved_at", record.get("created_at", "")),
        "source_count": len(snapshots),
        "active_count": sum(bool(item.get("active")) for item in snapshots),
        "source_names": [str(item.get("record", {}).get("name") or "") for item in snapshots[:3]],
        "autosaved": bool(record.get("autosaved")),
    }


def _save_warehouse(session: dict, name: str, *, autosaved: bool = False) -> dict:
    active = {str(value) for value in session.get("source_ids") or []}
    attached = [str(value) for value in session.get("attached_source_ids") or session.get("source_ids") or []]
    snapshots = []
    for source_id in attached:
        source = db().get("sources", source_id)
        if source and source.get("workspace_id", "default") == session.get("workspace_id", "default"):
            snapshots.append({"source_id": source_id, "active": source_id in active, "record": source})
    if not snapshots:
        raise ValueError("当前没有可保存的数据源")
    item = db().put(
        "data_warehouses",
        {
            "id": f"warehouse_{uuid.uuid4().hex[:16]}.json",
            "workspace_id": session.get("workspace_id", "default"),
            "session_id": session["id"], "name": name, "saved_at": utcnow(),
            "autosaved": autosaved, "sources": snapshots,
        },
        workspace_id=session.get("workspace_id", "default"),
    )
    return _warehouse_public(item)


def _compat_job(job: dict) -> dict:
    statuses = {"completed": "succeeded", "cancelled": "canceled"}
    return {
        "id": job["id"], "session_id": job.get("session_id"),
        "workspace_id": job.get("workspace_id"), "type": job.get("kind", ""),
        "label": job.get("title", ""), "status": statuses.get(job.get("status"), job.get("status")),
        "progress": job.get("progress", 0), "message": job.get("message", ""),
        "result": job.get("result"), "error": job.get("error"),
        "created_at": job.get("created_at"), "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
    }


def _upload_one(path: Path, original: str, wid: str) -> dict:
    with path.open("rb") as stream:
        return register_upload(FileStorage(stream=stream, filename=original), wid)


@bp.post("/api/session/<sid>/upload")
@api_errors
def upload(sid: str):
    session = _session(sid)
    files = request.files.getlist("file") or request.files.getlist("files")
    if not files or all(not item.filename for item in files):
        raise ValueError("未选择文件")
    threshold = int(os.getenv("BAA_EXCEL_JOB_THRESHOLD", "5000000"))
    added, pending, errors = [], [], []
    for file in files:
        filename = file.filename or ""
        suffix = Path(filename).suffix.lower()
        if suffix not in EXACT_UPLOAD_EXTENSIONS:
            errors.append(f"{filename}: 仅支持 .xlsx / .xls / .csv 文件")
            continue
        safe_name = secure_filename(filename) or f"upload_{uuid.uuid4().hex[:8]}{suffix}"
        pending_path = current_app.config["SETTINGS"].upload_dir / f"pending_{uuid.uuid4().hex[:16]}_{safe_name}"
        file.save(pending_path)
        try:
            if suffix != ".csv" and pending_path.stat().st_size > threshold:
                app = current_app._get_current_object()

                def work(progress, cancel, path=pending_path, original=filename, wid=session["workspace_id"]):
                    progress(5, "正在准备 Excel 解析")
                    if cancel.is_set():
                        return {"cancelled": True}
                    source = _upload_one(path, original, wid)
                    progress(95, "Excel 已解析，等待挂载")
                    path.unlink(missing_ok=True)
                    return {"source_id": source["id"], "source": public_source(source)}

                job = get_job_manager(app).submit(
                    workspace_id=session["workspace_id"], session_id=sid,
                    kind="excel_parse", title=filename, work=work,
                )
                pending.append({"id": job["id"], "type": "excel_parse", "source_name": filename, "status": "queued"})
                continue
            source = _upload_one(pending_path, filename, session["workspace_id"])
            pending_path.unlink(missing_ok=True)
            session = _attach_source(session, source["id"])
            added.append({
                "source_id": source["id"], "source_name": source["name"],
                "schema_preview": _source_schema_preview(source),
            })
        except Exception as exc:
            pending_path.unlink(missing_ok=True)
            errors.append(f"{filename}: {exc}")
    if not added and not pending:
        raise ValueError("; ".join(errors) or "文件解析失败")
    autosave = None
    if added:
        names = "、".join(item["source_name"] for item in added[:2])
        autosave = _save_warehouse(session, f"上传数据_{names}", autosaved=True)
    result = {
        "ok": True, "added": added, "pending_jobs": pending,
        "sources": _session_sources(session), "errors": errors,
        "source_name": (added or pending)[0]["source_name"],
        "schema_preview": added[0]["schema_preview"] if added else "",
        "warehouse_autosave": autosave,
    }
    return jsonify(result), (202 if pending else 200)


@bp.post("/api/session/<sid>/load-sample")
@api_errors
def load_sample(sid: str):
    session = _session(sid)
    if not (os.getenv("RAILWAY_PROJECT_ID") or os.getenv("VERCEL") == "1"):
        return jsonify({"error": "示例数据仅在云端演示环境可用"}), 403
    sample = Path(current_app.config["SETTINGS"].root) / "deploy" / "samples" / "Sample-data.xlsx"
    if not sample.is_file():
        return jsonify({"error": "示例数据文件未找到"}), 404
    for source in _session_sources(session):
        if source.get("sample_data"):
            return jsonify({"ok": True, "added": [], "duplicate": True, "sources": _session_sources(session)})
    source = _upload_one(sample, "示例数据-10城数据包.xlsx", session["workspace_id"])
    db().patch("sources", source["id"], {"sample_data": True})
    session = _attach_source(session, source["id"])
    added = [{
        "source_id": source["id"], "source_name": source["name"],
        "schema_preview": _source_schema_preview(source),
    }]
    return jsonify({"ok": True, "added": added, "pending_jobs": [], "sources": _session_sources(session)})


@bp.post("/api/session/<sid>/upload-jobs/<jid>/finalize")
@api_errors
def finalize_upload(sid: str, jid: str):
    session = _session(sid)
    job = require_workspace_record("jobs", jid, session["workspace_id"])
    if job.get("session_id") != sid or job.get("kind") != "excel_parse":
        raise FileNotFoundError("Excel 解析任务不存在")
    if job.get("status") != "completed":
        return jsonify({"error": "Excel 解析任务尚未完成", "status": _compat_job(job)["status"]}), 409
    source_id = str((job.get("result") or {}).get("source_id") or "")
    source = require_workspace_record("sources", source_id, session["workspace_id"])
    session = _attach_source(session, source_id)
    added = [{
        "source_id": source_id, "source_name": source["name"],
        "schema_preview": _source_schema_preview(source),
    }]
    autosave = _save_warehouse(session, f"上传数据_{source['name']}", autosaved=True)
    return jsonify({
        "ok": True, "added": added, "sources": _session_sources(session),
        "source_name": source["name"], "schema_preview": added[0]["schema_preview"],
        "warehouse_autosave": autosave,
    })


@bp.post("/api/session/<sid>/connect-db")
@api_errors
def connect_db(sid: str):
    session = _session(sid)
    payload = body()
    saved = _load_config(session["workspace_id"], "sql")
    connection_string = str(payload.get("connection_string") or saved.get("connection_string") or "").strip()
    if not connection_string:
        raise ValueError("连接字符串不能为空")
    source_public = register_database(
        {"url": connection_string, "name": payload.get("name") or saved.get("name") or ""},
        session["workspace_id"],
    )
    source = require_workspace_record("sources", source_public["id"], session["workspace_id"])
    session = _attach_source(session, source["id"])
    _save_config(session["workspace_id"], "sql", {
        "connection_string": connection_string, "name": payload.get("name") or "",
    })
    return jsonify({
        "ok": True, "source_id": source["id"], "source_name": source["name"],
        "schema_preview": _source_schema_preview(source), "sources": _session_sources(session),
    })


@bp.get("/api/session/<sid>/sources")
@api_errors
def sources(sid: str):
    return jsonify({"sources": _session_sources(_session(sid))})


@bp.get("/api/data-warehouses")
def warehouses():
    return jsonify([_warehouse_public(item) for item in db().list("data_warehouses", workspace_id=workspace_id())])


@bp.post("/api/session/<sid>/data-warehouse/save")
@api_errors
def save_warehouse(sid: str):
    session = _session(sid)
    name = str(body().get("name") or f"数据仓库_{utcnow()[:19]}").strip()
    return jsonify({"ok": True, **_save_warehouse(session, name)})


@bp.post("/api/session/<sid>/data-warehouse/load")
@api_errors
def load_warehouse(sid: str):
    session = _session(sid)
    filename = str(body().get("filename") or "").strip()
    if not filename:
        raise ValueError("未指定数据仓库文件")
    warehouse = require_workspace_record("data_warehouses", filename, session["workspace_id"])
    restored, active, errors = [], [], []
    for snapshot in warehouse.get("sources") or []:
        source_id = str(snapshot.get("source_id") or "")
        source = db().get("sources", source_id, include_archived=True)
        if source and source.get("archived_at"):
            db().restore("sources", source_id)
            source = db().get("sources", source_id)
        if not source:
            record = dict(snapshot.get("record") or {})
            path = Path(record.get("path") or "")
            if record.get("kind") != "database" and (not record.get("path") or not path.is_file()):
                errors.append(f"{record.get('name') or source_id}: 数据文件不存在")
                continue
            source_id = db().new_id("src")
            record["id"] = source_id
            record["workspace_id"] = session["workspace_id"]
            source = db().put("sources", record, workspace_id=session["workspace_id"])
        restored.append(source_id)
        if snapshot.get("active", True):
            active.append(source_id)
    if not restored:
        return jsonify({"error": "数据仓库中的数据源均恢复失败", "errors": errors}), 400
    session = _replace_sources(session, restored, active)
    return jsonify({
        "ok": True, "name": warehouse.get("name"), "restored": len(restored),
        "errors": errors, "sources": _session_sources(session),
    })


@bp.delete("/api/data-warehouses/<path:filename>")
@api_errors
def delete_warehouse(filename: str):
    require_workspace_record("data_warehouses", filename)
    if not db().archive("data_warehouses", filename):
        raise FileNotFoundError("数据仓库不存在")
    return jsonify({"ok": True})


@bp.post("/api/session/<sid>/sources/<source_id>/analysis-tables")
@api_errors
def analysis_tables(sid: str, source_id: str):
    session = _session(sid)
    source = require_workspace_record("sources", source_id, session["workspace_id"])
    if source_id not in (session.get("attached_source_ids") or session.get("source_ids") or []):
        raise FileNotFoundError("data source not found")
    if source.get("kind") != "database":
        raise ValueError("only SQL data sources support table selection")
    tables = body().get("tables", [])
    if not isinstance(tables, list):
        raise ValueError("tables must be a list")
    available = {str(item.get("name")) for item in source.get("tables") or []}
    selected = [str(value) for value in tables]
    unknown = [value for value in selected if value not in available]
    if unknown:
        raise ValueError(f"数据表不存在：{', '.join(unknown)}")
    db().patch("sources", source_id, {"analysis_tables": selected})
    return jsonify({"ok": True, "source_id": source_id, "tables": selected})


@bp.post("/api/session/<sid>/sources/<source_id>/toggle")
@api_errors
def toggle_source(sid: str, source_id: str):
    session = _session(sid)
    require_workspace_record("sources", source_id, session["workspace_id"])
    attached = [str(value) for value in session.get("attached_source_ids") or session.get("source_ids") or []]
    if source_id not in attached:
        raise FileNotFoundError("data source not found")
    active = [str(value) for value in session.get("source_ids") or []]
    if source_id in active:
        active.remove(source_id)
        state = False
    else:
        active.append(source_id)
        state = True
    session = _replace_sources(session, attached, active)
    return jsonify({"ok": True, "active": state, "sources": _session_sources(session)})


@bp.delete("/api/session/<sid>/sources/<source_id>")
@api_errors
def remove_source(sid: str, source_id: str):
    session = _session(sid)
    attached = [str(value) for value in session.get("attached_source_ids") or session.get("source_ids") or []]
    if source_id not in attached:
        raise FileNotFoundError("data source not found")
    attached.remove(source_id)
    active = [str(value) for value in session.get("source_ids") or [] if str(value) != source_id]
    session = _replace_sources(session, attached, active)
    return jsonify({"ok": True, "sources": _session_sources(session)})


@bp.get("/api/session/<sid>/preview")
@api_errors
def preview(sid: str):
    session = _session(sid)
    sources = _session_sources(session)
    active = [source for source in sources if source["active"]]
    if not active:
        raise FileNotFoundError("no data source")
    tables = []
    for source in active:
        selectable = source.get("kind") == "database"
        selected = set(source.get("analysis_tables") or [])
        for table in source.get("tables") or []:
            columns = table.get("schema") or table.get("columns") or []
            tables.append({
                "name": table.get("name"),
                "columns": [item.get("name") for item in columns] if isinstance(columns, list) else columns,
                "total_rows": table.get("rows"),
                "source_id": source["id"], "source_name": source["name"],
                "selectable_for_analysis": selectable,
                "selected_for_analysis": selectable and table.get("name") in selected,
            })
    return jsonify({
        "source_name": active[0]["name"], "tables": tables,
        "requires_table_selection": any(source.get("kind") == "database" for source in active),
    })


@bp.get("/api/session/<sid>/preview-table")
@api_errors
def preview_table(sid: str):
    session = _session(sid)
    table = str(request.args.get("table") or "")
    if not table:
        raise ValueError("missing table parameter")
    source_id = str(request.args.get("source_id") or "")
    active = [item for item in _session_sources(session) if item["active"]]
    target = next((item for item in active if item["id"] == source_id), None) if source_id else (active[0] if active else None)
    if not target:
        raise FileNotFoundError("no data source")
    source = require_workspace_record("sources", target["id"], session["workspace_id"])
    data = preview_source(source, table, 100)
    return jsonify({
        "name": data["table"], "columns": data["columns"], "rows": data["data"],
        "total_rows": data["rows"], "source_id": data["source_id"],
    })


@bp.delete("/api/session/<sid>/datasource")
@api_errors
def disconnect(sid: str):
    session = _session(sid)
    _replace_sources(session, [], [])
    return jsonify({"ok": True})


@bp.post("/api/session/<sid>/connect-gsheets")
@api_errors
def connect_gsheets(sid: str):
    session = _session(sid)
    payload, saved = body(), _load_config(session["workspace_id"], "gsheets")
    creds_raw = payload.get("creds_json") or saved.get("creds_json")
    spreadsheet = str(payload.get("spreadsheet") or saved.get("spreadsheet") or "").strip()
    if not creds_raw:
        raise ValueError("服务账号 JSON 不能为空")
    if not spreadsheet:
        raise ValueError("电子表格 URL 或 ID 不能为空")
    try:
        creds = json.loads(creds_raw) if isinstance(creds_raw, str) else creds_raw
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("服务账号 JSON 格式无效") from exc
    source_public = register_google_sheet(
        {"creds_dict": creds, "spreadsheet": spreadsheet, "name": payload.get("name") or ""},
        session["workspace_id"],
    )
    source = require_workspace_record("sources", source_public["id"], session["workspace_id"])
    session = _attach_source(session, source["id"])
    _save_config(session["workspace_id"], "gsheets", {
        "creds_json": json.dumps(creds, ensure_ascii=False), "spreadsheet": spreadsheet,
        "name": payload.get("name") or "",
    })
    return jsonify({
        "ok": True, "source_id": source["id"], "source_name": source["name"],
        "schema_preview": _source_schema_preview(source), "sources": _session_sources(session),
    })


@bp.post("/api/session/<sid>/connect-api")
@api_errors
def connect_api(sid: str):
    session = _session(sid)
    payload, saved = body(), _load_config(session["workspace_id"], "api")
    url = str(payload.get("url") or saved.get("url") or "").strip()
    if not url:
        raise ValueError("API URL 不能为空")
    auth_type = str(payload.get("auth_type") or saved.get("auth_type") or "none").strip()
    auth_value = str(payload.get("auth_value") or saved.get("auth_value") or "").strip()
    source_public = register_http(
        {"url": url, "auth_type": auth_type, "auth_value": auth_value, "name": payload.get("name") or ""},
        session["workspace_id"],
    )
    source = require_workspace_record("sources", source_public["id"], session["workspace_id"])
    session = _attach_source(session, source["id"])
    _save_config(session["workspace_id"], "api", {
        "url": url, "auth_type": auth_type, "auth_value": auth_value, "name": payload.get("name") or "",
    })
    return jsonify({
        "ok": True, "source_id": source["id"], "source_name": source["name"],
        "schema_preview": _source_schema_preview(source), "sources": _session_sources(session),
    })


@bp.get("/api/datasource-configs")
def datasource_configs():
    return jsonify(_public_configs(workspace_id()))


@bp.delete("/api/datasource-configs/<ds_type>")
@api_errors
def delete_datasource_config(ds_type: str):
    wid = workspace_id()
    record = db().get("datasource_configs", _config_id(wid, ds_type))
    if record:
        db().archive("datasource_configs", record["id"])
    return jsonify({"ok": True})


@bp.get("/api/session/<sid>/jobs")
@api_errors
def session_jobs(sid: str):
    session = _session(sid)
    limit = max(1, min(int(request.args.get("limit", "100")), 500))
    items = [
        _compat_job(item) for item in db().list("jobs", workspace_id=session["workspace_id"], limit=limit)
        if item.get("session_id") == sid
    ]
    if request.args.get("active", "").lower() in {"1", "true", "yes"}:
        items = [item for item in items if item["status"] in {"queued", "running", "canceling"}]
    return jsonify({"jobs": items})


@bp.delete("/api/session/<sid>/jobs")
@api_errors
def clear_session_jobs(sid: str):
    session = _session(sid)
    deleted = 0
    for item in db().list("jobs", workspace_id=session["workspace_id"], limit=5000):
        if item.get("session_id") == sid and item.get("status") in {"completed", "failed", "cancelled"}:
            deleted += int(db().archive("jobs", item["id"]))
    return jsonify({"ok": True, "deleted": deleted})


@bp.get("/api/session/<sid>/jobs/events")
@api_errors
def session_job_events(sid: str):
    session = _session(sid)
    allowed = {
        item["id"] for item in db().list("jobs", workspace_id=session["workspace_id"], limit=5000)
        if item.get("session_id") == sid
    }
    after = max(0, int(request.args.get("after_sequence", "0")))
    events = [item for item in db().job_events(after, int(request.args.get("limit", "200"))) if item["job_id"] in allowed]
    next_sequence = events[-1]["sequence"] if events else after
    return jsonify({"events": events, "next_sequence": next_sequence, "latest_sequence": next_sequence, "replay_truncated": False})


@bp.get("/api/session/<sid>/jobs/<jid>")
@api_errors
def session_job(sid: str, jid: str):
    session = _session(sid)
    item = require_workspace_record("jobs", jid, session["workspace_id"])
    if item.get("session_id") != sid:
        raise FileNotFoundError("job not found")
    return jsonify({"job": _compat_job(item)})


@bp.post("/api/session/<sid>/jobs/<jid>/cancel")
@api_errors
def cancel_session_job(sid: str, jid: str):
    session = _session(sid)
    item = require_workspace_record("jobs", jid, session["workspace_id"])
    if item.get("session_id") != sid:
        raise FileNotFoundError("job not found")
    if item.get("status") not in {"queued", "running"}:
        return jsonify({"error": "cannot cancel terminal job", "id": jid, "status": _compat_job(item)["status"]}), 409
    accepted = get_job_manager(current_app._get_current_object()).cancel(jid)
    return jsonify({"id": jid, "accepted": accepted, "status": "canceling" if accepted else _compat_job(item)["status"]})
