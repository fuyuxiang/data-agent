from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..services.lifecycle import (
    artifact_preview,
    artifact_references,
    archived,
    file_trash,
    load_settings,
    reclaim,
    recycle_registered_artifact,
    recycle_unregistered_artifact,
    recycle_upload,
    report,
    restore_archived,
    restore_artifact,
    restore_file_trash,
    save_settings,
    uploads_preview,
    workspace_preview,
)
from .common import body, db, workspace_id


bp = Blueprint("lifecycle", __name__)


def _failure(exc: Exception, not_found: str):
    status = 404 if isinstance(exc, FileNotFoundError) else 400
    return jsonify({"ok": False, "error": not_found if status == 404 else str(exc)}), status


def _days() -> int:
    try:
        days = int(body().get("retention_days", 30))
    except (TypeError, ValueError) as exc:
        raise ValueError("保留天数必须是非负整数") from exc
    if not 0 <= days <= 3650:
        raise ValueError("保留天数必须在 0 到 3650 之间")
    return days


@bp.get("/api/lifecycle/audit")
def lifecycle_audit():
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        return jsonify({"ok": False, "error": "limit 必须是整数"}), 400
    items = [item for item in db().audit_entries(workspace_id(), limit) if item["event_type"].startswith("lifecycle.")]
    return jsonify({"ok": True, "items": items})


@bp.get("/api/lifecycle/report")
def lifecycle_report(): return jsonify({"ok": True, "report": report(db(), workspace_id())})


@bp.get("/api/lifecycle/settings")
def lifecycle_settings_get(): return jsonify({"ok": True, "settings": load_settings(db(), workspace_id())})


@bp.put("/api/lifecycle/settings")
def lifecycle_settings_put():
    try:
        return jsonify({"ok": True, "settings": save_settings(db(), workspace_id(), body())})
    except ValueError as exc:
        return _failure(exc, "")


@bp.get("/api/lifecycle/workspaces/preview")
def lifecycle_workspaces(): return jsonify({"ok": True, "preview": workspace_preview(db(), workspace_id())})


@bp.get("/api/lifecycle/artifacts/preview")
def lifecycle_artifacts(): return jsonify({"ok": True, "preview": artifact_preview(db(), workspace_id())})


@bp.get("/api/lifecycle/uploads/preview")
def lifecycle_uploads(): return jsonify({"ok": True, "preview": uploads_preview(db(), workspace_id())})


@bp.post("/api/lifecycle/uploads/recycle")
def lifecycle_upload_recycle():
    try:
        summary = recycle_upload(
            db(), workspace_id(), str(body().get("category") or ""),
            str(body().get("relative_path") or ""),
        )
        return jsonify({"ok": True, "summary": summary})
    except (ValueError, FileNotFoundError) as exc:
        return _failure(exc, "上传文件不存在或已被处理")


@bp.get("/api/lifecycle/upload-trash")
def lifecycle_upload_trash(): return jsonify({"ok": True, "items": file_trash(db(), workspace_id(), "uploads")})


@bp.post("/api/lifecycle/upload-trash/reclaim")
def lifecycle_upload_reclaim():
    try:
        return jsonify({"ok": True, "summary": reclaim(db(), workspace_id(), kind="uploads", retention_days=_days())})
    except ValueError as exc:
        return _failure(exc, "")


@bp.post("/api/lifecycle/upload-trash/<trash_id>/restore")
def lifecycle_upload_restore(trash_id: str):
    try:
        return jsonify({"ok": True, "summary": restore_file_trash(db(), workspace_id(), trash_id, "uploads")})
    except (ValueError, FileNotFoundError) as exc:
        return _failure(exc, "上传回收站项目不存在")


@bp.get("/api/lifecycle/artifacts/references/preview")
def lifecycle_references(): return jsonify({"ok": True, "preview": artifact_references(db(), workspace_id())})


@bp.post("/api/lifecycle/artifacts/prune-missing")
def lifecycle_prune_missing():
    missing = artifact_preview(db(), workspace_id())["missing_registered_ids"]
    for artifact_id in missing:
        db().delete("artifacts", artifact_id)
    return jsonify({"ok": True, "summary": {"removed": len(missing)}})


@bp.post("/api/lifecycle/artifacts/unregistered/recycle")
def lifecycle_artifact_unknown_recycle():
    try:
        summary = recycle_unregistered_artifact(
            db(), workspace_id(), str(body().get("type") or ""),
            str(body().get("relative_path") or ""),
        )
        return jsonify({"ok": True, "summary": summary})
    except (ValueError, FileNotFoundError) as exc:
        return _failure(exc, "历史产物不存在或已被处理")


@bp.post("/api/lifecycle/artifacts/registered/recycle")
def lifecycle_artifact_registered_recycle():
    try:
        summary = recycle_registered_artifact(
            db(), workspace_id(), str(body().get("artifact_id") or ""),
        )
        return jsonify({"ok": True, "summary": summary})
    except (ValueError, FileNotFoundError) as exc:
        return _failure(exc, "已登记产物不存在或已被处理")


@bp.get("/api/lifecycle/artifact-trash")
def lifecycle_artifact_trash():
    items = file_trash(db(), workspace_id(), "artifacts")
    moved = {item.get("artifact_id") for item in items}
    items.extend({"id": item["id"], "artifact_id": item["id"], "filename": item.get("filename"), "deleted_at": item.get("archived_at")} for item in archived(db(), workspace_id(), "artifacts") if item["id"] not in moved)
    return jsonify({"ok": True, "items": items})


@bp.post("/api/lifecycle/artifact-trash/reclaim")
def lifecycle_artifact_reclaim():
    try:
        days = _days()
        file_summary = reclaim(db(), workspace_id(), kind="artifacts", retention_days=days)
        record_summary = reclaim(db(), workspace_id(), collection="artifacts", retention_days=days)
        return jsonify({"ok": True, "summary": {key: file_summary.get(key, 0) + record_summary.get(key, 0) for key in {"groups", "files", "items", "bytes"}}})
    except ValueError as exc:
        return _failure(exc, "")


@bp.post("/api/lifecycle/artifact-trash/<trash_id>/restore")
def lifecycle_artifact_restore(trash_id: str):
    try:
        return jsonify({"ok": True, "summary": restore_artifact(db(), workspace_id(), trash_id)})
    except (ValueError, FileNotFoundError) as exc:
        return _failure(exc, "产物回收站项目不存在")


def _record_trash(collection: str): return jsonify({"ok": True, "items": archived(db(), workspace_id(), collection)})


def _record_reclaim(collection: str):
    try:
        return jsonify({"ok": True, "summary": reclaim(db(), workspace_id(), collection=collection, retention_days=_days())})
    except ValueError as exc:
        return _failure(exc, "")


def _record_restore(collection: str, trash_id: str, label: str):
    try:
        return jsonify({"ok": True, "summary": restore_archived(db(), workspace_id(), collection, trash_id)})
    except (ValueError, FileNotFoundError) as exc:
        return _failure(exc, label)


@bp.get("/api/lifecycle/session-trash")
def lifecycle_session_trash(): return _record_trash("sessions")


@bp.post("/api/lifecycle/session-trash/reclaim")
def lifecycle_session_reclaim(): return _record_reclaim("sessions")


@bp.post("/api/lifecycle/session-trash/<trash_id>/restore")
def lifecycle_session_restore(trash_id: str): return _record_restore("sessions", trash_id, "回收站项目不存在")


@bp.get("/api/lifecycle/memory-trash")
def lifecycle_memory_trash(): return _record_trash("memories")


@bp.post("/api/lifecycle/memory-trash/reclaim")
def lifecycle_memory_reclaim(): return _record_reclaim("memories")


@bp.post("/api/lifecycle/memory-trash/<trash_id>/restore")
def lifecycle_memory_restore(trash_id: str): return _record_restore("memories", trash_id, "记忆回收站项目不存在")
