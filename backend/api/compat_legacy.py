from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, g, jsonify, request, send_file
from werkzeug.datastructures import FileStorage

from ..core.database import utcnow
from ..services.dashboard_refresh import refresh_dashboard as refresh_dashboard_record
from ..services.dashboard_refresh import refresh_widget as refresh_widget_record
from ..services.datasets import SUPPORTED_FILE_EXTENSIONS, public_source, register_upload
from ..services.authorization import (
    require_artifact_access, require_dashboard_access as require_dashboard_policy,
    require_result_access as require_result_policy,
)
from .common import (
    api_errors,
    body,
    current_user_id,
    db,
    require_session_access,
    require_source_access,
    require_workspace_access,
    require_workspace_record,
    safe_child,
    workspace_id,
    workspace_membership,
)


bp = Blueprint("compat_legacy", __name__)


def _session(session_id: str, *, create: bool = False) -> dict:
    wid = workspace_id()
    record = db().get("sessions", session_id)
    if record:
        return require_session_access(session_id, wid)
    if not create:
        raise FileNotFoundError("session not found")
    require_workspace_access(wid, write=True)
    return db().put(
        "sessions",
        {
            "id": session_id,
            "workspace_id": wid,
            "name": "新分析会话",
            "status": "active",
            "source_ids": [],
            "attached_source_ids": [],
            "provider_id": None,
            "owner_id": current_user_id(),
        },
        workspace_id=wid,
    )


def _visible_history(session_id: str, limit: int = 1000) -> list[dict]:
    result = []
    for item in db().messages(session_id, limit):
        metadata = item.get("metadata") or {}
        message = {
            "role": item.get("role", "user"),
            "content": item.get("content", ""),
            "created_at": item.get("created_at", ""),
            "id": item.get("id", ""),
        }
        if metadata:
            message["metadata"] = metadata
            for key in (
                "reasoning",
                "chart_ids",
                "artifacts",
                "tool_result_ids",
                "query_result_id",
                "dashboard_ids",
            ):
                if key in metadata:
                    message[key] = metadata[key]
        result.append(message)
    return result


def _visible_msg_count(history: list[dict]) -> int:
    return sum(1 for item in history if item.get("role") in {"user", "assistant"})


def _usage_for_session(session_id: str, wid: str) -> dict:
    events = [
        item
        for item in db().list("usage_events", workspace_id=wid, limit=5000)
        if item.get("session_id") == session_id
    ]
    if not events:
        for message in db().messages(session_id, 1000):
            usage = (message.get("metadata") or {}).get("usage")
            if isinstance(usage, dict):
                events.append({"session_id": session_id, "workspace_id": wid, **usage})
    breakdowns = [
        {
            "model": item.get("model", ""),
            "actual_prompt_tokens": int(item.get("prompt_tokens") or 0),
            "actual_completion_tokens": int(item.get("completion_tokens") or 0),
            "actual_total_tokens": int(item.get("total_tokens") or 0),
            "payload_tokens_est": int(item.get("prompt_tokens") or 0),
            "created_at": item.get("created_at", ""),
        }
        for item in events[-100:]
    ]
    total_input = sum(int(item.get("prompt_tokens") or 0) for item in events)
    total_output = sum(int(item.get("completion_tokens") or 0) for item in events)
    total_cached = sum(int(item.get("cached_input_tokens") or 0) for item in events)
    total_cache_write = sum(int(item.get("cache_write_tokens") or 0) for item in events)
    actual_prompt = sum(item["actual_prompt_tokens"] for item in breakdowns)
    estimated_prompt = sum(item["payload_tokens_est"] for item in breakdowns)
    return {
        "calls_retained": len(breakdowns),
        "retention_limit": 100,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cached_input_tokens": total_cached,
        "total_cache_write_tokens": total_cache_write,
        "cache_hit_ratio": round(total_cached / total_input, 4) if total_input else 0.0,
        "estimated_prompt_tokens_retained": estimated_prompt,
        "actual_prompt_tokens_retained": actual_prompt,
        "estimation_error_pct": (
            round(abs(estimated_prompt - actual_prompt) / actual_prompt * 100, 2)
            if actual_prompt
            else None
        ),
        "breakdowns": breakdowns,
    }


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value).strip("._")
    return (cleaned or "session")[:80]


def _saved_session_public(record: dict) -> dict:
    history = record.get("history") or record.get("messages") or []
    filename = str(record.get("filename") or record.get("id") or "")
    return {
        "id": record.get("id"),
        "filename": filename,
        "name": record.get("name", ""),
        "saved_at": record.get("saved_at") or record.get("created_at", ""),
        "msg_count": _visible_msg_count(history),
        "is_autosave": bool(record.get("autosave") or record.get("is_autosave")),
        "autosave": bool(record.get("autosave") or record.get("is_autosave")),
        "session_id": record.get("session_id", ""),
        "workspace_id": record.get("workspace_id", "default"),
    }


def _find_saved_session(identifier: str, wid: str | None = None) -> dict | None:
    wid = wid or workspace_id()
    safe = Path(identifier).name
    for key in (identifier, safe):
        record = db().get("saved_sessions", key)
        if record and str(record.get("workspace_id") or "default") == wid and _saved_session_owned(record):
            return record
    for record in db().list("saved_sessions", workspace_id=wid, limit=5000):
        if Path(str(record.get("filename") or "")).name == safe and _saved_session_owned(record):
            return record
    return None


def _saved_session_owned(record: dict) -> bool:
    owner_id = str(record.get("owner_id") or (record.get("session") or {}).get("owner_id") or "")
    if owner_id:
        return owner_id == current_user_id()
    membership = workspace_membership(str(record.get("workspace_id") or workspace_id()))
    return bool(membership and membership.get("role") == "owner")


def _save_session_snapshot(session: dict, *, name: str, autosave: bool, record_id: str = "") -> dict:
    wid = str(session.get("workspace_id") or "default")
    metrics = _usage_for_session(session["id"], wid)
    history = _visible_history(session["id"])
    if not record_id:
        if autosave:
            record_id = f"autosave_{session['id']}.json"
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            record_id = f"{_safe_stem(name)}_{stamp}.json"
    record = db().put(
        "saved_sessions",
        {
            "id": record_id,
            "filename": record_id,
            "workspace_id": wid,
            "owner_id": current_user_id(),
            "name": name[:100],
            "saved_at": utcnow(),
            "autosave": autosave,
            "is_autosave": autosave,
            "session_id": session["id"],
            "model_provider": session.get("provider_id") or "",
            "history": history,
            "messages": history,
            "total_input_tokens": metrics["total_input_tokens"],
            "total_output_tokens": metrics["total_output_tokens"],
            "total_cached_input_tokens": metrics["total_cached_input_tokens"],
            "total_cache_write_tokens": metrics["total_cache_write_tokens"],
            "usage_breakdowns": metrics["breakdowns"],
            "data_source": {"source_ids": session.get("source_ids") or []},
            "workspace": {"workspace_id": wid},
            "session": session,
        },
        workspace_id=wid,
    )
    return record


def _attach_source(session: dict, source_id: str) -> dict:
    require_source_access(source_id, str(session.get("workspace_id") or "default"))
    attached = [str(item) for item in session.get("attached_source_ids") or session.get("source_ids") or []]
    active = [str(item) for item in session.get("source_ids") or []]
    if source_id not in attached:
        attached.append(source_id)
    if source_id not in active:
        active.append(source_id)
    return db().patch(
        "sessions",
        session["id"],
        {"attached_source_ids": attached, "source_ids": active},
    ) or session


def _session_sources(session: dict) -> list[dict]:
    active = {str(item) for item in session.get("source_ids") or []}
    attached = [str(item) for item in session.get("attached_source_ids") or session.get("source_ids") or []]
    result = []
    for source_id in attached:
        try:
            source = require_source_access(
                source_id, str(session.get("workspace_id") or "default"),
            )
        except (FileNotFoundError, PermissionError):
            continue
        public = public_source(source)
        public["source_id"] = source_id
        public["source_name"] = source.get("name", "")
        public["active"] = source_id in active
        result.append(public)
    return result


def _schema_preview(source: dict) -> str:
    lines = []
    for table in source.get("tables") or []:
        columns = table.get("schema") or table.get("columns") or []
        if isinstance(columns, list):
            names = ", ".join(str(item.get("name") if isinstance(item, dict) else item) for item in columns)
        else:
            names = f"{columns} columns" if columns else ""
        lines.append(f"Table: {table.get('name')} ({table.get('rows', '?')} rows) [{names}]")
    return "\n\n".join(lines)


def _register_workspace_files(session: dict, root: Path) -> dict:
    added, errors = [], []
    for candidate in sorted(root.iterdir()):
        if not candidate.is_file() or candidate.suffix.lower() not in SUPPORTED_FILE_EXTENSIONS:
            continue
        try:
            with candidate.open("rb") as stream:
                source = register_upload(FileStorage(stream=stream, filename=candidate.name), session["workspace_id"])
            source = db().patch("sources", source["id"], {"kind": "workspace", "origin_path": str(candidate)}) or source
            session = _attach_source(session, source["id"])
            added.append({
                "source_id": source["id"],
                "source_name": source["name"],
                "schema_preview": _schema_preview(source),
            })
        except Exception as exc:
            errors.append(f"{candidate.name}: {exc}")
    return {"session": session, "added": added, "errors": errors, "sources": _session_sources(session)}


def _workspace_status(session: dict) -> dict:
    wid = str(session.get("workspace_id") or "default")
    workspace = db().get("workspaces", wid) or {"id": wid, "name": wid}
    mounted = bool(workspace.get("mounted_path"))
    permission = "read_write" if workspace.get("permission", "write") == "write" else "read_only"
    artifacts_dir = current_app.config["SETTINGS"].export_dir
    return {
        "workspace_id": wid,
        "id": wid,
        "name": workspace.get("name") or wid,
        "root_path": workspace.get("mounted_path") or "",
        "workdir": workspace.get("mounted_path") or "",
        "mounted": mounted,
        "mounted_at": workspace.get("mounted_at"),
        "permission": permission,
        "effective_permission": permission,
        "artifacts_dir": str(artifacts_dir),
    }


def _script_json(value: object) -> str:
    """Serialize JSON without allowing data to terminate its script container."""
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _chart_html(spec: dict, title: str = "Chart") -> str:
    encoded = _script_json(spec or {})
    title_text = html.escape(title or "Chart")
    nonce = html.escape(str(getattr(g, "csp_nonce", "")), quote=True)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_text}</title>
  <style>html,body,#chart{{width:100%;height:100%;margin:0}}body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}</style>
  <script src="/vendor/echarts.min.js"></script>
  <script src="/vendor/echarts-china.min.js"></script>
</head>
<body>
  <div id="chart"></div>
  <script id="chart-spec" type="application/json" nonce="{nonce}">{encoded}</script>
  <script nonce="{nonce}">
    const spec = JSON.parse(document.getElementById('chart-spec').textContent || '{{}}');
    const option = spec.option || spec;
    const chart = echarts.init(document.getElementById('chart'));
    chart.setOption(option);
    addEventListener('resize', () => chart.resize());
  </script>
</body>
</html>"""


def _dashboard_public(record: dict) -> dict:
    value = dict(record)
    widgets = []
    for widget in value.get("widgets") or []:
        item = dict(widget)
        if item.get("chart") and not item.get("chart_id"):
            item["chart_id"] = item.get("id")
        widgets.append(item)
    value["widgets"] = widgets
    value.setdefault("dashboard_id", value.get("id"))
    value.setdefault("created_at", value.get("created_at", ""))
    value.setdefault("color_scheme", value.get("color_scheme", "mckinsey"))
    return value


def _dashboard_html(record: dict) -> str:
    data = _script_json(_dashboard_public(record))
    title = html.escape(str(record.get("name") or "Dashboard"))
    nonce = html.escape(str(getattr(g, "csp_nonce", "")), quote=True)
    widget_blocks = []
    for widget in record.get("widgets") or []:
        spec = widget.get("chart") or {}
        widget_blocks.append(
            "<section class=\"widget\">"
            f"<h2>{html.escape(str(widget.get('title') or widget.get('name') or '图表'))}</h2>"
            f"<div class=\"chart\" data-spec=\"{html.escape(json.dumps(spec, ensure_ascii=False), quote=True)}\"></div>"
            "</section>"
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body{{margin:0;background:#f6f7f9;color:#18212f;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
    header{{padding:24px 28px;background:#fff;border-bottom:1px solid #e5e7eb}}
    main{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;padding:18px}}
    .widget{{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:14px;min-height:360px}}
    h1{{font-size:22px;margin:0}}h2{{font-size:15px;margin:0 0 10px}}.chart{{height:300px}}
  </style>
  <script src="/vendor/echarts.min.js"></script>
  <script src="/vendor/echarts-china.min.js"></script>
</head>
<body>
  <header><h1>{title}</h1></header>
  <main>{''.join(widget_blocks) or '<p>暂无组件</p>'}</main>
  <script id="dashboard-data" type="application/json" nonce="{nonce}">{data}</script>
  <script nonce="{nonce}">
    document.querySelectorAll('.chart').forEach((el) => {{
      const spec = JSON.parse(el.dataset.spec || '{{}}');
      if (!spec.option && !Object.keys(spec).length) return;
      const chart = echarts.init(el);
      chart.setOption(spec.option || spec);
      addEventListener('resize', () => chart.resize());
    }});
  </script>
</body>
</html>"""


@bp.post("/api/session/new")
def new_session():
    payload = body()
    wid = workspace_id()
    source_ids = [str(item) for item in payload.get("source_ids", [])]
    for session in db().list("sessions", workspace_id=wid, limit=5000):
        if session.get("status") == "active":
            db().patch("sessions", session["id"], {"status": "idle"})
    item = db().put(
        "sessions",
        {
            "id": db().new_id("ses"),
            "workspace_id": wid,
            "name": str(payload.get("name") or "新分析会话")[:100],
            "status": "active",
            "source_ids": source_ids,
            "attached_source_ids": source_ids,
            "provider_id": payload.get("provider_id"),
        },
        workspace_id=wid,
    )
    return jsonify({"session_id": item["id"], "item": item})


@bp.get("/api/session/<session_id>/ping")
@api_errors
def ping_session(session_id: str):
    session = _session(session_id)
    history = _visible_history(session_id)
    return jsonify({"alive": True, "msg_count": _visible_msg_count(history), "workspace_id": session["workspace_id"]})


@bp.get("/api/session/<session_id>/load-current")
@api_errors
def load_current_session(session_id: str):
    session = _session(session_id)
    history = _visible_history(session_id)
    metrics = _usage_for_session(session_id, session["workspace_id"])
    return jsonify({
        "history": history,
        "messages": history,
        "session": session,
        "sources": _session_sources(session),
        "total_input": metrics["total_input_tokens"],
        "total_output": metrics["total_output_tokens"],
        "total_cached_input": metrics["total_cached_input_tokens"],
        "total_cache_write": metrics["total_cache_write_tokens"],
        "usage_breakdowns": metrics["breakdowns"],
        "msg_count": _visible_msg_count(history),
    })


@bp.get("/api/session/<session_id>/token-metrics")
@api_errors
def token_metrics(session_id: str):
    session = _session(session_id)
    return jsonify({"ok": True, **_usage_for_session(session_id, session["workspace_id"])})


@bp.post("/api/session/<session_id>/clear")
@api_errors
def clear_session(session_id: str):
    _session(session_id, create=True)
    cleared = len(db().messages(session_id, 1000))
    db().replace_messages(session_id, [])
    return jsonify({"ok": True, "cleared": cleared})


@bp.post("/api/session/<session_id>/stop")
@api_errors
def stop_session(session_id: str):
    _session(session_id, create=True)
    from .conversation import stop_chat

    return stop_chat(session_id)


@bp.post("/api/session/<session_id>/prompt-suggestion")
@api_errors
def prompt_suggestion(session_id: str):
    _session(session_id)
    history = [item for item in _visible_history(session_id, 12) if item.get("role") in {"user", "assistant"}]
    if len(history) < 2:
        return jsonify({"ok": False, "suggestion": ""})
    last_user = next((item for item in reversed(history) if item.get("role") == "user"), {})
    content = str(last_user.get("content") or "").strip()
    suggestion = f"继续围绕“{content[:80]}”深入分析" if content else ""
    return jsonify({"ok": bool(suggestion), "suggestion": suggestion[:220]})


@bp.post("/api/session/<session_id>/chat")
@api_errors
def chat(session_id: str):
    _session(session_id, create=True)
    from .conversation import chat as conversation_chat

    return conversation_chat(session_id)


@bp.post("/api/session/<session_id>/autosave")
@api_errors
def autosave_session(session_id: str):
    session = _session(session_id, create=True)
    history = _visible_history(session_id)
    if not history:
        return jsonify({"ok": False, "reason": "empty"})
    payload = body()
    name = str(payload.get("name") or "").strip() or f"自动保存_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    target = Path(str(payload.get("target_file") or "")).name
    if target and not _find_saved_session(target, session["workspace_id"]):
        target = ""
    record = _save_session_snapshot(
        session,
        name=name,
        autosave=True,
        record_id=target or f"autosave_{session_id}.json",
    )
    return jsonify({"ok": True, "saved_at": record["saved_at"], "filename": record["filename"]})


@bp.get("/api/session/<session_id>/autosave")
@api_errors
def get_autosave(session_id: str):
    session = _session(session_id)
    record = _find_saved_session(f"autosave_{session_id}.json", session["workspace_id"])
    if not record:
        return jsonify({"exists": False})
    public = _saved_session_public(record)
    return jsonify({
        "exists": True,
        "saved_at": public["saved_at"],
        "msg_count": public["msg_count"],
        "filename": public["filename"],
    })


@bp.post("/api/session/<session_id>/save")
@api_errors
def save_session(session_id: str):
    session = _session(session_id)
    history = _visible_history(session_id)
    if not history:
        return jsonify({"error": "对话为空，无需保存"}), 400
    name = str(body().get("name") or "").strip() or datetime.now().strftime("对话_%Y%m%d_%H%M%S")
    record = _save_session_snapshot(session, name=name, autosave=False)
    return jsonify({"ok": True, "filename": record["filename"], "name": name, "item": _saved_session_public(record)})


@bp.post("/api/session/<session_id>/load")
@api_errors
def load_session(session_id: str):
    payload = body()
    filename = str(payload.get("filename") or payload.get("id") or "").strip()
    if not filename:
        return jsonify({"error": "未指定文件名"}), 400
    session = _session(session_id, create=True)
    saved = _find_saved_session(filename, session["workspace_id"])
    if not saved:
        return jsonify({"error": "文件不存在"}), 404
    history = [
        {**item, "id": db().new_id("msg"), "session_id": session_id}
        for item in saved.get("history") or saved.get("messages") or []
        if isinstance(item, dict)
    ]
    db().replace_messages(session_id, history)
    requested_source_ids = [
        str(item) for item in (saved.get("session") or {}).get("source_ids", session.get("source_ids", []))
    ]
    source_ids = []
    for source_id in requested_source_ids:
        try:
            require_source_access(source_id, session["workspace_id"])
        except (FileNotFoundError, PermissionError):
            continue
        source_ids.append(source_id)
    session = db().patch(
        "sessions",
        session_id,
        {
            "name": saved.get("name") or session.get("name"),
            "source_ids": source_ids,
            "attached_source_ids": [str(item) for item in (saved.get("session") or {}).get("attached_source_ids", source_ids)],
            "provider_id": session.get("provider_id") if payload.get("keep_provider") else saved.get("model_provider") or session.get("provider_id"),
        },
    ) or session
    return jsonify({
        "ok": True,
        "name": saved.get("name", ""),
        "history": _visible_history(session_id),
        "model_provider": session.get("provider_id") or "",
        "saved_provider": saved.get("model_provider") or "",
        "total_input": int(saved.get("total_input_tokens") or 0),
        "total_output": int(saved.get("total_output_tokens") or 0),
        "ds_connected": bool(source_ids),
        "ds_name": "",
        "ds_lost": len(source_ids) != len(requested_source_ids),
        "workspace_restored": True,
        "workspace_lost": False,
        "workspace_identity_mismatch": False,
    })


@bp.post("/api/saved-sessions/<path:identifier>/rename")
@bp.patch("/api/saved-sessions/<path:identifier>")
@api_errors
def rename_saved_session(identifier: str):
    saved = _find_saved_session(identifier, workspace_id())
    if not saved:
        return jsonify({"error": "文件不存在"}), 404
    name = str(body().get("name") or "").strip()
    if not name:
        return jsonify({"error": "名称不能为空"}), 400
    updated = db().patch(
        "saved_sessions",
        saved["id"],
        {"name": name[:100], "renamed_at": utcnow()},
    ) or saved
    return jsonify({"ok": True, "filename": updated.get("filename") or updated["id"], "name": updated["name"]})


@bp.delete("/api/saved-sessions/<path:identifier>")
@api_errors
def delete_saved_session(identifier: str):
    saved = _find_saved_session(identifier, workspace_id())
    if not saved:
        return jsonify({"error": "文件不存在"}), 404
    db().archive("saved_sessions", saved["id"])
    return jsonify({"ok": True, "deleted": True, "filename": saved.get("filename") or saved["id"]})


@bp.post("/api/session/<session_id>/commands/compact")
@api_errors
def compact_command(session_id: str):
    session = _session(session_id)
    from .conversation import compact

    response = compact(session_id)
    db().put(
        "command_metrics",
        {
            "id": db().new_id("cmd"),
            "workspace_id": session["workspace_id"],
            "session_id": session_id,
            "command": "compact",
            "command_type": "backend",
            "outcome": "success",
            "duration_ms": 0,
        },
        workspace_id=session["workspace_id"],
    )
    if isinstance(response, Response):
        response.headers["Deprecation"] = "true"
        response.headers["Link"] = f"</api/session/{session_id}/commands/compact/execute>; rel=\"successor-version\""
    return response


@bp.get("/api/session/<session_id>/command-metrics")
@api_errors
def command_metrics(session_id: str):
    session = _session(session_id)
    entries = [
        item
        for item in db().list("command_metrics", workspace_id=session["workspace_id"], limit=5000)
        if item.get("session_id") == session_id
    ][-200:]
    summary: dict[str, dict[str, int]] = {}
    for item in entries:
        command_type = str(item.get("command_type") or item.get("command") or "unknown")
        aggregate = summary.setdefault(
            command_type,
            {
                "count": 0,
                "success": 0,
                "error": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "duration_ms": 0,
            },
        )
        aggregate["count"] += 1
        if item.get("outcome") == "success":
            aggregate["success"] += 1
        elif item.get("outcome") in {"error", "rejected"}:
            aggregate["error"] += 1
        for key in ("input_tokens", "output_tokens", "cached_input_tokens", "duration_ms"):
            aggregate[key] += int(item.get(key) or 0)
    return jsonify({"ok": True, "retention_limit": 200, "entries": entries, "items": entries, "summary": summary})


@bp.post("/api/session/<session_id>/command-metrics")
@api_errors
def record_command_metric(session_id: str):
    session = _session(session_id, create=True)
    payload = body()
    command = str(payload.get("command") or "").strip().lower()
    if not command:
        raise ValueError("command 不能为空")
    outcome = str(payload.get("outcome") or "success").strip().lower()
    if outcome not in {"success", "error", "rejected"}:
        raise ValueError("无效的命令执行结果")
    db().put(
        "command_metrics",
        {
            "id": db().new_id("cmd"),
            "workspace_id": session["workspace_id"],
            "session_id": session_id,
            "command": command,
            "command_type": str(payload.get("command_type") or "local"),
            "outcome": outcome,
            "duration_ms": int(payload.get("duration_ms") or 0),
            "error_code": str(payload.get("error_code") or ""),
        },
        workspace_id=session["workspace_id"],
    )
    return jsonify({"ok": True})


@bp.get("/api/chart/<chart_id>")
@api_errors
def serve_chart(chart_id: str):
    chart = require_workspace_record("charts", chart_id)
    if chart.get("result_id"):
        require_result_policy(
            db(), db().get("query_results", str(chart["result_id"]), workspace_id=chart["workspace_id"]),
            workspace_id=chart["workspace_id"], actor_id=current_user_id(),
        )
    elif chart.get("source_id"):
        require_source_access(str(chart["source_id"]), chart["workspace_id"])
    return Response(_chart_html(chart.get("spec") or {}, str(chart.get("name") or "Chart")), mimetype="text/html")


@bp.get("/dashboard/<dashboard_id>")
@api_errors
def dashboard_page(dashboard_id: str):
    dashboard = db().get("dashboards", dashboard_id)
    if not dashboard or not workspace_membership(str(dashboard.get("workspace_id") or "default")):
        return Response("Dashboard not found", status=404, mimetype="text/plain")
    require_dashboard_policy(
        db(), dashboard, workspace_id=str(dashboard.get("workspace_id") or "default"),
        actor_id=current_user_id(),
    )
    return Response(_dashboard_html(dashboard), mimetype="text/html")


@bp.post("/api/dashboard/generate")
@api_errors
def generate_dashboard():
    payload = body()
    session_id = str(payload.get("session_id") or "")
    session = _session(session_id) if session_id else None
    wid = str((session or {}).get("workspace_id") or workspace_id())
    widgets_spec = payload.get("widgets")
    if not isinstance(widgets_spec, list) or not widgets_spec:
        widgets_spec = [
            {
                "id": f"widget-{index}",
                "title": chart.get("name", f"图表 {index}"),
                "chart_id": chart["id"],
                "chart": chart.get("spec", {}),
                "result_id": chart.get("result_id"),
                "source_id": chart.get("source_id"),
            }
            for index, chart in enumerate(db().list("charts", workspace_id=wid, limit=12), 1)
        ]
    widgets = []
    for index, raw in enumerate(widgets_spec, 1):
        if not isinstance(raw, dict):
            continue
        chart_id = str(raw.get("chart_id") or "")
        chart = require_workspace_record("charts", chart_id, wid) if chart_id else None
        result_id = str(raw.get("result_id") or (chart or {}).get("result_id") or "")
        source_id = str(raw.get("source_id") or (chart or {}).get("source_id") or "")
        if result_id:
            require_result_policy(
                db(), db().get("query_results", result_id, workspace_id=wid),
                workspace_id=wid, actor_id=current_user_id(),
            )
        if source_id:
            require_source_access(source_id, wid)
        spec = raw.get("chart") or raw.get("spec") or (chart or {}).get("spec") or {}
        widgets.append({
            "id": str(raw.get("id") or f"widget-{index}")[:100],
            "title": str(raw.get("title") or raw.get("name") or f"组件 {index}")[:100],
            "chart_type": raw.get("chart_type") or (spec or {}).get("type"),
            "chart_id": chart_id or None,
            "chart": spec if isinstance(spec, dict) else {},
            "query": raw.get("query") or raw.get("sql") or "",
            "field_mapping": raw.get("field_mapping") or {},
            "grid": raw.get("grid") or {"x": (index - 1) % 2 * 6, "y": (index - 1) // 2 * 4, "w": 6, "h": 4},
            "result_id": result_id or None,
            "source_id": source_id or None,
        })
    dashboard = db().put(
        "dashboards",
        {
            "id": db().new_id("dash"),
            "workspace_id": wid,
            "name": str(payload.get("name") or "Dashboard")[:100],
            "description": str(payload.get("description") or "")[:500],
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "color_scheme": str(payload.get("color_scheme") or "mckinsey"),
            "session_id": session_id,
            "owner_id": current_user_id(),
            "widgets": widgets,
            "layout": payload.get("layout", {"columns": 12}),
            "revision": 1,
        },
        workspace_id=wid,
    )
    return jsonify({"dashboard_id": dashboard["id"], "id": dashboard["id"], "url": f"/dashboard/{dashboard['id']}", "item": dashboard})


@bp.get("/api/dashboard/<dashboard_id>")
@api_errors
def get_dashboard(dashboard_id: str):
    dashboard = require_workspace_record("dashboards", dashboard_id)
    require_dashboard_policy(
        db(), dashboard, workspace_id=dashboard["workspace_id"], actor_id=current_user_id(),
    )
    return jsonify(_dashboard_public(dashboard))


@bp.put("/api/dashboard/<dashboard_id>")
@api_errors
def update_dashboard(dashboard_id: str):
    dashboard = require_workspace_record("dashboards", dashboard_id)
    require_dashboard_policy(
        db(), dashboard, workspace_id=dashboard["workspace_id"],
        actor_id=current_user_id(), action="update",
    )
    payload = body()
    changes: dict[str, Any] = {}
    if "name" in payload:
        changes["name"] = str(payload["name"])[:100]
    if "description" in payload:
        changes["description"] = str(payload["description"])[:500]
    if "container_width" in payload:
        changes["container_width"] = payload["container_width"]
    if isinstance(payload.get("widgets"), list):
        current_widgets = [dict(item) for item in dashboard.get("widgets") or []]
        by_id = {str(item.get("id")): item for item in current_widgets}
        for raw in payload["widgets"]:
            if not isinstance(raw, dict) or "id" not in raw:
                continue
            item = by_id.get(str(raw["id"]))
            if not item:
                continue
            for key in ("grid", "title", "chart", "chart_id", "chart_type", "field_mapping"):
                if key in raw:
                    item[key] = raw[key]
        changes["widgets"] = current_widgets
    changes["updated_at"] = utcnow()
    changes["revision"] = int(dashboard.get("revision", 1)) + 1
    require_dashboard_policy(
        db(), {**dashboard, **changes}, workspace_id=dashboard["workspace_id"],
        actor_id=current_user_id(), action="update",
    )
    updated = db().patch("dashboards", dashboard_id, changes) or dashboard
    return jsonify({"ok": True, "item": updated})


@bp.delete("/api/dashboard/<dashboard_id>")
@api_errors
def delete_dashboard(dashboard_id: str):
    dashboard = require_workspace_record("dashboards", dashboard_id)
    require_dashboard_policy(
        db(), dashboard, workspace_id=dashboard["workspace_id"],
        actor_id=current_user_id(), action="delete",
    )
    db().archive("dashboards", dashboard_id)
    return jsonify({"ok": True})


@bp.post("/api/dashboard/<dashboard_id>/refresh")
@api_errors
def refresh_dashboard(dashboard_id: str):
    dashboard = require_workspace_record("dashboards", dashboard_id)
    require_dashboard_policy(
        db(), dashboard, workspace_id=dashboard["workspace_id"],
        actor_id=current_user_id(), action="query",
    )
    dashboard = refresh_dashboard_record(db(), dashboard, current_user_id())
    results = [
        {
            "id": widget.get("id"), "chart_id": widget.get("chart_id"),
            "error": widget.get("refresh_error"), "result_id": widget.get("result_id"),
        }
        for widget in dashboard.get("widgets") or []
    ]
    return jsonify({"ok": True, "widgets": results, "kpi_widgets": [], "item": dashboard})


@bp.post("/api/dashboard/<dashboard_id>/widget/<widget_id>/refresh")
@api_errors
def refresh_widget(dashboard_id: str, widget_id: str):
    dashboard = require_workspace_record("dashboards", dashboard_id)
    require_dashboard_policy(
        db(), dashboard, workspace_id=dashboard["workspace_id"],
        actor_id=current_user_id(), action="query",
    )
    found = None
    widgets = []
    for widget in dashboard.get("widgets") or []:
        if str(widget.get("id")) == widget_id:
            widget = refresh_widget_record(
                db(), widget, str(dashboard.get("workspace_id") or "default"),
                current_user_id(),
            )
            found = widget
        widgets.append(widget)
    if found is None:
        raise FileNotFoundError("看板组件不存在")
    dashboard["widgets"] = widgets
    dashboard["revision"] = int(dashboard.get("revision", 1)) + 1
    db().put("dashboards", dashboard, workspace_id=dashboard.get("workspace_id", "default"))
    return jsonify({"ok": True, "id": widget_id, "chart_id": found.get("chart_id"), "error": found.get("error"), "widget": found})


@bp.get("/api/dashboard/<dashboard_id>/export-html")
@api_errors
def export_dashboard_legacy(dashboard_id: str):
    dashboard = require_workspace_record("dashboards", dashboard_id)
    require_dashboard_policy(
        db(), dashboard, workspace_id=dashboard["workspace_id"],
        actor_id=current_user_id(), action="export",
    )
    return Response(
        _dashboard_html(dashboard),
        mimetype="text/html",
        headers={"Content-Disposition": f'attachment; filename="{_safe_stem(dashboard.get("name") or "dashboard")}.html"'},
    )


@bp.get("/api/export/<path:filename>")
@api_errors
def download_export(filename: str):
    safe = Path(filename).name
    if safe != filename:
        raise FileNotFoundError("文件不存在")
    artifact = next(
        (item for item in db().list("artifacts", workspace_id=workspace_id(), limit=5000) if item.get("filename") == safe),
        None,
    )
    if not artifact:
        raise FileNotFoundError("文件不存在")
    require_artifact_access(
        db(), artifact, workspace_id=artifact["workspace_id"],
        actor_id=current_user_id(), action="export",
    )
    path = safe_child(current_app.config["SETTINGS"].export_dir, Path(artifact["path"]))
    if not path.is_file():
        raise FileNotFoundError("文件不存在")
    return send_file(path, as_attachment=True, download_name=safe)


@bp.post("/api/session/<session_id>/workspace/mount")
@api_errors
def mount_workspace(session_id: str):
    session = _session(session_id, create=True)
    require_workspace_access(session["workspace_id"], write=True)
    payload = body()
    raw_path = str(payload.get("path") or "").strip()
    if not raw_path:
        return jsonify({"ok": False, "error": "缺少 path 参数。"}), 400
    root = Path(raw_path).expanduser().resolve()
    if not root.is_dir():
        return jsonify({"ok": False, "error": "目录不存在或不是文件夹。"}), 400
    if db().list("users", include_archived=True) and os.getenv("MERIDIAN_ALLOW_HOST_MOUNTS", "0") != "1":
        allowed_root = (current_app.config["SETTINGS"].workspace_dir / session["workspace_id"]).resolve()
        if root != allowed_root and allowed_root not in root.parents:
            raise PermissionError("服务器模式只能挂载该工作空间的受控目录")
    permission = str(payload.get("permission") or "read_only")
    if permission not in {"read_only", "read_write"}:
        return jsonify({"ok": False, "error": "permission 必须是 read_only 或 read_write。"}), 400
    workspace = db().get("workspaces", session["workspace_id"]) or {"id": session["workspace_id"], "name": root.name}
    workspace.update({
        "id": session["workspace_id"],
        "workspace_id": session["workspace_id"],
        "name": str(payload.get("name") or workspace.get("name") or root.name)[:100],
        "mounted_path": str(root),
        "mounted_at": utcnow(),
        "permission": "write" if permission == "read_write" else "read",
    })
    db().put("workspaces", workspace, workspace_id=session["workspace_id"])
    registered = _register_workspace_files(session, root)
    return jsonify({
        "ok": True,
        "workspace": _workspace_status(registered["session"]),
        "added": registered["added"],
        "errors": registered["errors"],
        "reused": 0,
        "schema_preview": registered["added"][0]["schema_preview"] if registered["added"] else "",
        "source_name": registered["added"][0]["source_name"] if registered["added"] else "",
        "pending_jobs": [],
        "sources": registered["sources"],
        "continued_workspace": None,
    })


@bp.post("/api/session/<session_id>/workspace/jobs/<job_id>/finalize")
@api_errors
def finalize_workspace_job(session_id: str, job_id: str):
    _session(session_id)
    return jsonify({"error": "工作目录 Excel 任务不存在"}), 404


@bp.post("/api/session/<session_id>/workspace/unmount")
@api_errors
def unmount_workspace(session_id: str):
    session = _session(session_id)
    require_workspace_access(session["workspace_id"], write=True)
    workspace = db().get("workspaces", session["workspace_id"])
    if not workspace or not workspace.get("mounted_path"):
        return jsonify({"ok": False, "error": "未挂载工作目录。"}), 400
    db().patch("workspaces", session["workspace_id"], {"mounted_path": None, "mounted_at": None})
    return jsonify({"ok": True, "sources": _session_sources(session), "continued_workspace": None})


@bp.get("/api/session/<session_id>/workspace")
@api_errors
def get_workspace(session_id: str):
    return jsonify({"ok": True, "workspace": _workspace_status(_session(session_id))})


@bp.get("/api/session/<session_id>/workspaces")
@api_errors
def list_session_workspaces(session_id: str):
    session = _session(session_id)
    items = []
    for item in db().list("workspaces", limit=5000):
        if not workspace_membership(item["id"]):
            continue
        items.append({
            "workspace_id": item["id"],
            "id": item["id"],
            "name": item.get("name", item["id"]),
            "root_path": item.get("mounted_path") or "",
            "permission": "read_write" if item.get("permission") == "write" else "read_only",
            "mounted": item["id"] == session.get("workspace_id") and bool(item.get("mounted_path")),
            "connected_session_count": sum(
                1
                for current in db().list("sessions", workspace_id=item["id"], limit=5000)
                if current.get("workspace_id") == item["id"]
            ),
            "active_lease_count": 0,
            "active_job_count": 0,
        })
    return jsonify({"ok": True, "workspaces": items})


@bp.get("/api/session/<session_id>/workspaces/<target_workspace_id>/remove-preview")
@api_errors
def workspace_remove_preview(session_id: str, target_workspace_id: str):
    _session(session_id)
    workspace = require_workspace_access(target_workspace_id, owner=True)
    return jsonify({
        "ok": True,
        "workspace": {
            "workspace_id": workspace["id"],
            "name": workspace.get("name", workspace["id"]),
            "root_path": workspace.get("mounted_path") or "",
        },
        "can_remove": True,
        "blockers": [],
        "action": "archive_workspace_record",
        "preserved": {"physical_directory": True, "authoritative_metadata": True},
    })


@bp.delete("/api/session/<session_id>/workspaces/<target_workspace_id>")
@api_errors
def remove_workspace_record(session_id: str, target_workspace_id: str):
    _session(session_id)
    require_workspace_access(target_workspace_id, owner=True)
    if body().get("confirmed") is not True:
        return jsonify({"ok": False, "error": "移除前必须明确确认。", "code": "confirmation_required"}), 400
    if target_workspace_id == "default":
        db().patch("workspaces", target_workspace_id, {"mounted_path": None, "mounted_at": None})
    else:
        db().archive("workspaces", target_workspace_id)
    return jsonify({
        "ok": True,
        "removed_workspace_id": target_workspace_id,
        "files_deleted": 0,
        "directory_deleted": False,
        "metadata_deleted": False,
        "stable_identity_lookup_preserved": True,
    })


@bp.get("/api/session/<session_id>/workspaces/<target_workspace_id>/storage-cleanup-preview")
@api_errors
def storage_cleanup_preview(session_id: str, target_workspace_id: str):
    _session(session_id)
    workspace = require_workspace_access(target_workspace_id, owner=True)
    return jsonify({
        "ok": True,
        "workspace": {"workspace_id": target_workspace_id, "root_path": workspace.get("mounted_path") or ""},
        "active_lease_count": 0,
        "candidates": [],
        "summary": {"files": 0, "bytes": 0},
    })


@bp.post("/api/session/<session_id>/workspaces/<target_workspace_id>/storage-cleanup")
@api_errors
def storage_cleanup(session_id: str, target_workspace_id: str):
    _session(session_id)
    require_workspace_access(target_workspace_id, owner=True)
    if body().get("confirmed") is not True:
        return jsonify({"ok": False, "error": "清理前必须明确确认。", "code": "confirmation_required"}), 400
    return jsonify({"ok": True, "workspace_id": target_workspace_id, "summary": {"files": 0, "bytes": 0}, "items": []})


@bp.get("/api/session/<session_id>/workspaces/<target_workspace_id>/switch-preview")
@api_errors
def workspace_switch_preview(session_id: str, target_workspace_id: str):
    session = _session(session_id)
    workspace = require_workspace_access(target_workspace_id)
    return jsonify({
        "ok": True,
        "target": {
            "workspace_id": target_workspace_id,
            "name": workspace.get("name", target_workspace_id),
            "root_path": workspace.get("mounted_path") or "",
            "permission": "read_write" if workspace.get("permission") == "write" else "read_only",
        },
        "current": _workspace_status(session),
        "already_current": str(session.get("workspace_id") or "default") == target_workspace_id,
        "requires_confirmation": str(session.get("workspace_id") or "default") != target_workspace_id,
        "continuing_job_ids": [],
        "continuing_job_count": 0,
    })


@bp.patch("/api/session/<session_id>/workspaces/<target_workspace_id>")
@api_errors
def rename_workspace(session_id: str, target_workspace_id: str):
    _session(session_id)
    require_workspace_access(target_workspace_id, write=True)
    name = " ".join(str(body().get("name") or "").split())
    if not name:
        return jsonify({"ok": False, "error": "工作目录显示名称长度必须为 1-80 个字符，且不能包含控制字符。"}), 400
    workspace = db().patch("workspaces", target_workspace_id, {"name": name[:80]})
    if not workspace:
        raise FileNotFoundError("工作空间不存在")
    return jsonify({"ok": True, "workspace": {"workspace_id": workspace["id"], **workspace}})


@bp.get("/api/session/<session_id>/workspace/checkpoints")
@api_errors
def workspace_checkpoints(session_id: str):
    session = _session(session_id)
    require_workspace_access(session["workspace_id"], owner=True)
    snapshots = [
        {key: value for key, value in item.items() if key not in {"state", "messages", "snapshot_path"}}
        for item in db().list("checkpoints", workspace_id=session["workspace_id"], limit=5000)
    ]
    return jsonify({"ok": True, "workspace": _workspace_status(session), "snapshots": snapshots})


@bp.post("/api/session/<session_id>/workspace/checkpoints/<snapshot_id>/restore")
@api_errors
def restore_workspace_checkpoint(session_id: str, snapshot_id: str):
    session = _session(session_id)
    require_workspace_access(session["workspace_id"], owner=True)
    snapshot = db().get("checkpoints", snapshot_id)
    if not snapshot or snapshot.get("workspace_id") != session["workspace_id"]:
        raise FileNotFoundError("快照不存在")
    if body().get("confirm") is not True:
        return jsonify({"ok": False, "error": "恢复前必须明确确认。"}), 400
    for collection, records in (snapshot.get("state") or {}).items():
        for record in records:
            db().put(collection, record, workspace_id=session["workspace_id"])
    for sid, messages in (snapshot.get("messages") or {}).items():
        db().replace_messages(sid, messages)
    return jsonify({"ok": True, "restored": True, "job_id": ""})


@bp.post("/api/session/<session_id>/workspace/permission")
@api_errors
def workspace_permission(session_id: str):
    session = _session(session_id)
    require_workspace_access(session["workspace_id"], owner=True)
    permission = str(body().get("permission") or "")
    if permission not in {"read_only", "read_write"}:
        return jsonify({"ok": False, "error": "permission 必须是 read_only 或 read_write。"}), 400
    db().patch("workspaces", session["workspace_id"], {"permission": "write" if permission == "read_write" else "read"})
    return jsonify({"ok": True, "workspace": _workspace_status(session)})


@bp.get("/api/session/<session_id>/agent-profiles")
@api_errors
def session_agent_profiles(session_id: str):
    session = _session(session_id)
    defaults = [
        {"id": "data-specialist", "key": "data-specialist", "name": "数据工程顾问", "role": "负责结构识别、查询与质量校验", "built_in": True},
        {"id": "quant-specialist", "key": "quant-specialist", "name": "量化分析顾问", "role": "负责统计检验、建模与不确定性", "built_in": True},
        {"id": "business-specialist", "key": "business-specialist", "name": "经营策略顾问", "role": "负责业务解释、风险和行动建议", "built_in": True},
        {"id": "review-specialist", "key": "review-specialist", "name": "证据复核顾问", "role": "负责口径、证据链和交付审查", "built_in": True},
    ]
    profiles = defaults + db().list("agent_profiles", workspace_id=session["workspace_id"], limit=5000)
    return jsonify({"ok": True, "profiles": profiles, "items": profiles})


@bp.post("/api/session/<session_id>/agent-profiles")
@api_errors
def create_session_agent_profile(session_id: str):
    session = _session(session_id, create=True)
    payload = body()
    key = str(payload.get("key") or payload.get("id") or db().new_id("profile"))[:100]
    item = db().put(
        "agent_profiles",
        {
            "id": key,
            "key": key,
            "workspace_id": session["workspace_id"],
            "name": str(payload.get("name") or key)[:100],
            "role": str(payload.get("role") or "")[:1000],
            "instructions": str(payload.get("instructions") or "")[:8000],
            "allowed_tools": payload.get("allowed_tools", payload.get("tools", [])),
            "tools": payload.get("tools", payload.get("allowed_tools", [])),
            "model_policy": str(payload.get("model_policy") or "inherit"),
            "created_by": str(payload.get("created_by") or "")[:200],
            "enabled": True,
        },
        workspace_id=session["workspace_id"],
    )
    return jsonify({"ok": True, "profile": item, "item": item}), 201


@bp.get("/api/session/<session_id>/workflows")
@api_errors
def session_workflows(session_id: str):
    session = _session(session_id)
    workflows = db().list("workflows", workspace_id=session["workspace_id"], limit=5000)
    for workflow in workflows:
        version_id = str(workflow.get("current_version_id") or "")
        workflow["current_version"] = db().get("workflow_versions", version_id) if version_id else None
        workflow.setdefault("graph", workflow.get("definition", {}))
    return jsonify({"ok": True, "workflows": workflows, "items": workflows})


@bp.post("/api/session/<session_id>/workflows")
@api_errors
def create_session_workflow(session_id: str):
    session = _session(session_id, create=True)
    payload = body()
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("工作流名称不能为空")
    definition = payload.get("graph") or payload.get("definition") or {"steps": []}
    item = db().put(
        "workflows",
        {
            "id": db().new_id("flow"),
            "workspace_id": session["workspace_id"],
            "name": name[:120],
            "description": str(payload.get("description") or "")[:1000],
            "definition": definition,
            "graph": definition,
            "input_schema": payload.get("input_schema", {}),
            "output_schema": payload.get("output_schema", {}),
            "status": "draft",
            "version": 0,
            "draft_revision": 1,
            "current_version_id": None,
            "created_by": str(payload.get("created_by") or "")[:200],
        },
        workspace_id=session["workspace_id"],
    )
    return jsonify({"ok": True, "workflow": item, "item": item}), 201


@bp.get("/api/session/<session_id>/workflows/<workflow_id>")
@api_errors
def get_session_workflow(session_id: str, workflow_id: str):
    session = _session(session_id)
    workflow = db().get("workflows", workflow_id)
    if not workflow or workflow.get("workspace_id") != session["workspace_id"]:
        raise FileNotFoundError("工作流不存在")
    workflow.setdefault("graph", workflow.get("definition", {}))
    return jsonify({"ok": True, "workflow": workflow, "item": workflow})


@bp.delete("/api/session/<session_id>/workflows/<workflow_id>")
@api_errors
def delete_session_workflow(session_id: str, workflow_id: str):
    session = _session(session_id)
    workflow = db().get("workflows", workflow_id)
    if not workflow or workflow.get("workspace_id") != session["workspace_id"]:
        raise FileNotFoundError("工作流不存在")
    db().archive("workflows", workflow_id)
    return jsonify({"ok": True, "deleted": True, "archived": True})


@bp.put("/api/session/<session_id>/workflows/<workflow_id>/draft")
@api_errors
def update_session_workflow_draft(session_id: str, workflow_id: str):
    session = _session(session_id)
    workflow = db().get("workflows", workflow_id)
    if not workflow or workflow.get("workspace_id") != session["workspace_id"]:
        raise FileNotFoundError("工作流不存在")
    payload = body()
    expected = payload.get("expected_revision")
    if expected is not None and int(expected) != int(workflow.get("draft_revision", 1)):
        return jsonify({"ok": False, "error": "工作流草稿版本冲突，请刷新后重试", "code": "version_conflict"}), 409
    changes = {
        key: payload[key]
        for key in ("name", "description", "input_schema", "output_schema")
        if key in payload
    }
    if "graph" in payload or "definition" in payload:
        changes["definition"] = payload.get("graph") or payload.get("definition") or {}
        changes["graph"] = changes["definition"]
    changes["status"] = "draft"
    changes["draft_revision"] = int(workflow.get("draft_revision", 1)) + 1
    updated = db().patch("workflows", workflow_id, changes) or workflow
    return jsonify({"ok": True, "workflow": updated, "item": updated})


@bp.post("/api/session/<session_id>/workflows/<workflow_id>/validate")
@api_errors
def validate_session_workflow(session_id: str, workflow_id: str):
    session = _session(session_id)
    workflow = db().get("workflows", workflow_id)
    if not workflow or workflow.get("workspace_id") != session["workspace_id"]:
        raise FileNotFoundError("工作流不存在")
    from ..services.workflows import validate_definition

    return jsonify({"ok": True, "validation": validate_definition(workflow.get("definition") or {})})


@bp.post("/api/session/<session_id>/workflows/<workflow_id>/publish")
@api_errors
def publish_session_workflow(session_id: str, workflow_id: str):
    session = _session(session_id)
    workflow = db().get("workflows", workflow_id)
    if not workflow or workflow.get("workspace_id") != session["workspace_id"]:
        raise FileNotFoundError("工作流不存在")
    from ..services.workflows import validate_definition

    validation = validate_definition(workflow.get("definition") or {})
    if not validation.get("valid"):
        return jsonify({"ok": False, "validation": validation, "error": "；".join(validation.get("errors") or [])}), 400
    version = db().put(
        "workflow_versions",
        {
            "id": db().new_id("wfver"),
            "workspace_id": session["workspace_id"],
            "workflow_id": workflow_id,
            "version": int(workflow.get("version", 0)) + 1,
            "definition": validation.get("definition") or workflow.get("definition") or {},
            "input_schema": workflow.get("input_schema", {}),
            "output_schema": workflow.get("output_schema", {}),
            "published_by": str(body().get("published_by") or "")[:200],
            "published_at": utcnow(),
        },
        workspace_id=session["workspace_id"],
    )
    updated = db().patch(
        "workflows",
        workflow_id,
        {
            "status": "published",
            "version": version["version"],
            "current_version_id": version["id"],
            "published_definition": version["definition"],
            "published_at": utcnow(),
        },
    ) or workflow
    return jsonify({"ok": True, "workflow": updated, "item": updated, "version": version, "validation": validation})


def _team_by_name(session: dict, team_name: str) -> dict | None:
    teams = db().list("teams", workspace_id=session["workspace_id"], limit=5000)
    return next((item for item in teams if item.get("id") == team_name or item.get("name") == team_name), None)


@bp.get("/api/session/<session_id>/teams")
@api_errors
def list_teams(session_id: str):
    session = _session(session_id)
    teams = db().list("teams", workspace_id=session["workspace_id"], limit=5000)
    return jsonify({"ok": True, "teams": teams, "items": teams})


@bp.get("/api/session/<session_id>/teams/<team_name>")
@api_errors
def team_status(session_id: str, team_name: str):
    session = _session(session_id)
    team = _team_by_name(session, team_name)
    if not team:
        raise FileNotFoundError("team not found")
    messages = [
        item
        for item in db().list("team_messages", workspace_id=session["workspace_id"], limit=1000)
        if item.get("team_id") == team["id"]
    ]
    return jsonify({"ok": True, "team": {**team, "recent_messages": messages[-50:]}})


@bp.delete("/api/session/<session_id>/teams/<team_name>")
@api_errors
def delete_team(session_id: str, team_name: str):
    session = _session(session_id)
    if body().get("confirm") is not True:
        return jsonify({"ok": False, "error": "解散团队前必须明确确认。"}), 400
    team = _team_by_name(session, team_name)
    if not team:
        raise FileNotFoundError("team not found")
    db().archive("teams", team["id"])
    return jsonify({"ok": True, "deleted": True})


@bp.delete("/api/session/<session_id>/teams/<team_name>/messages")
@api_errors
def clear_team_messages(session_id: str, team_name: str):
    session = _session(session_id)
    if body().get("confirm") is not True:
        return jsonify({"ok": False, "error": "清空前必须明确确认。"}), 400
    team = _team_by_name(session, team_name)
    if not team:
        raise FileNotFoundError("team not found")
    cleared = 0
    for item in db().list("team_messages", workspace_id=session["workspace_id"], limit=5000):
        if item.get("team_id") == team["id"] and db().archive("team_messages", item["id"]):
            cleared += 1
    return jsonify({"ok": True, "cleared": cleared})


@bp.get("/api/session/<session_id>/team-plans")
@api_errors
def team_plans(session_id: str):
    session = _session(session_id)
    plans = db().list("team_plans", workspace_id=session["workspace_id"], limit=5000)
    team_name = str(request.args.get("team_name") or "")
    if team_name:
        plans = [item for item in plans if item.get("team_name") == team_name or item.get("team_id") == team_name]
    return jsonify({"ok": True, "plans": plans})


@bp.get("/api/session/<session_id>/team-plans/<plan_id>")
@api_errors
def get_team_plan(session_id: str, plan_id: str):
    session = _session(session_id)
    plan = db().get("team_plans", plan_id)
    if not plan or plan.get("workspace_id") != session["workspace_id"]:
        raise FileNotFoundError("team plan not found")
    return jsonify({"ok": True, "plan": plan})


@bp.post("/api/session/<session_id>/team-plans/<plan_id>/actions/<action>")
@api_errors
def control_team_plan(session_id: str, plan_id: str, action: str):
    session = _session(session_id)
    plan = db().get("team_plans", plan_id)
    if not plan or plan.get("workspace_id") != session["workspace_id"]:
        raise FileNotFoundError("team plan not found")
    if action not in {"cancel", "pause", "resume", "approve", "reject"}:
        raise ValueError("unsupported team plan action")
    status = {"cancel": "cancelled", "pause": "paused", "resume": "planned", "approve": "approved", "reject": "rejected"}[action]
    plan = db().patch("team_plans", plan_id, {"status": status, "updated_at": utcnow()}) or plan
    return jsonify({"ok": True, "plan": plan, "canceled_jobs": 0})


@bp.post("/api/session/<session_id>/team-plans/<plan_id>/workflow-draft")
@api_errors
def team_plan_workflow_draft(session_id: str, plan_id: str):
    session = _session(session_id)
    plan = db().get("team_plans", plan_id)
    if not plan or plan.get("workspace_id") != session["workspace_id"]:
        raise FileNotFoundError("team plan not found")
    workflow = db().put(
        "workflows",
        {
            "id": db().new_id("flow"),
            "workspace_id": session["workspace_id"],
            "name": str(plan.get("name") or f"团队计划 {plan_id}")[:120],
            "description": str(plan.get("description") or "")[:1000],
            "definition": {"steps": plan.get("tasks") or []},
            "graph": {"steps": plan.get("tasks") or []},
            "status": "draft",
            "version": 0,
            "draft_revision": 1,
        },
        workspace_id=session["workspace_id"],
    )
    return jsonify({"ok": True, "workflow": workflow}), 201


@bp.post("/api/desktop/clients/<client_id>/heartbeat")
def desktop_heartbeat(client_id: str):
    return jsonify({"ok": True, "client_id": client_id, "server_time": utcnow()})


@bp.post("/api/desktop/clients/<client_id>/disconnect")
def desktop_disconnect(client_id: str):
    return jsonify({"ok": True, "client_id": client_id, "disconnected": True})
