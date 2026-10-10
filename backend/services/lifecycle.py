from __future__ import annotations

import os
import json
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import current_app

from ..core.database import Database, utcnow


DEFAULT_SETTINGS = {"retention_preset": "forever", "retention_custom_days": 30}


def settings() -> Any:
    return current_app.config["SETTINGS"]


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def load_settings(database: Database, workspace_id: str) -> dict:
    item = database.get("lifecycle_settings", f"lifecycle_{workspace_id}")
    return {
        "retention_preset": str((item or {}).get("retention_preset") or "forever"),
        "retention_custom_days": int((item or {}).get("retention_custom_days", 30)),
    }


def save_settings(database: Database, workspace_id: str, payload: dict, *, actor: str = "system") -> dict:
    preset = str(payload.get("retention_preset") or "forever")
    if preset not in {"7", "14", "forever", "custom"}:
        raise ValueError("保留策略无效")
    try:
        days = int(payload.get("retention_custom_days", 30))
    except (TypeError, ValueError) as exc:
        raise ValueError("自定义保留天数必须是整数") from exc
    if isinstance(payload.get("retention_custom_days"), bool) or str(payload.get("retention_custom_days", 30)) != str(days):
        raise ValueError("自定义保留天数必须是整数")
    if not 1 <= days <= 3650:
        raise ValueError("自定义保留天数必须在 1 到 3650 之间")
    database.put(
        "lifecycle_settings",
        {"id": f"lifecycle_{workspace_id}", "workspace_id": workspace_id, "retention_preset": preset, "retention_custom_days": days},
        workspace_id=workspace_id,
    )
    result = {"retention_preset": preset, "retention_custom_days": days}
    database.audit("lifecycle.settings.updated", workspace_id=workspace_id, actor=actor,
                   object_type="lifecycle_settings", object_id=f"lifecycle_{workspace_id}", detail=result)
    return result


def expire_retention(database: Database, workspace_id: str, *, now: datetime | None = None) -> dict:
    """Soft-archive inactive content; files, messages and audit evidence remain recoverable.

    Use database timestamps rather than payload timestamps so restoring an item
    starts a new retention period. Check and archive in one write transaction to
    avoid racing a new message, task or edit in another application worker.
    """
    instant = now or datetime.now(timezone.utc)
    summary = {"sessions": 0, "artifacts": 0, "saved_sessions": 0}
    with database.transaction() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT payload FROM records WHERE collection='lifecycle_settings' "
            "AND id=? AND workspace_id=? AND archived_at IS NULL",
            (f"lifecycle_{workspace_id}", workspace_id),
        ).fetchone()
        policy = json.loads(row["payload"]) if row else DEFAULT_SETTINGS
        preset = str(policy.get("retention_preset") or "forever")
        if preset == "forever":
            return summary
        days = int(policy.get("retention_custom_days", 30)) if preset == "custom" else int(preset)
        if not 1 <= days <= 3650:
            return summary
        cutoff = (instant - timedelta(days=days)).isoformat()
        archived_at = instant.isoformat(timespec="seconds")
        candidates = connection.execute(
            """SELECT r.collection,r.id FROM records r
               WHERE r.workspace_id=? AND r.archived_at IS NULL
                 AND r.collection IN ('sessions','artifacts','saved_sessions')
                 AND julianday(r.updated_at) <= julianday(?)
                 AND NOT EXISTS (
                   SELECT 1 FROM agent_runs a WHERE a.workspace_id=r.workspace_id
                     AND (a.session_id=r.id OR a.id=json_extract(r.payload,'$.run_id'))
                     AND (a.execution_status NOT IN ('finished','failed','cancelled','partial')
                          OR julianday(a.updated_at)>julianday(?)))
                 AND NOT EXISTS (
                   SELECT 1 FROM messages m WHERE r.collection='sessions'
                     AND m.session_id=r.id AND m.workspace_id=r.workspace_id
                     AND julianday(m.created_at)>julianday(?))
               ORDER BY r.updated_at LIMIT 500""",
            (workspace_id, cutoff, cutoff, cutoff),
        ).fetchall()
        for item in candidates:
            connection.execute(
                "UPDATE records SET archived_at=?,updated_at=? "
                "WHERE collection=? AND id=? AND workspace_id=? AND archived_at IS NULL",
                (archived_at, archived_at, item["collection"], item["id"], workspace_id),
            )
            summary[item["collection"]] += 1
            connection.execute(
                "INSERT INTO audit_log(workspace_id,event_type,actor,object_type,object_id,detail,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (workspace_id, "lifecycle.retention.archived", "system", item["collection"], item["id"],
                 json.dumps({"retention_days": days}, ensure_ascii=False), archived_at),
            )
    return summary


class RetentionWorker:
    """Apply persisted workspace policies once a minute while the service is running."""

    def __init__(self, app):
        self.app = app
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="workspace-retention", daemon=True)

    def sweep(self) -> None:
        database = self.app.extensions["meridian_db"]
        with database.connect() as connection:
            workspaces = connection.execute(
                "SELECT DISTINCT workspace_id FROM records WHERE collection='lifecycle_settings' AND archived_at IS NULL",
            ).fetchall()
        for item in workspaces:
            try:
                expire_retention(database, item["workspace_id"])
            except Exception:
                self.app.logger.exception("Failed to apply retention for workspace %s", item["workspace_id"])

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception:
                self.app.logger.exception("Failed to check workspace retention policies")
            self._stop.wait(60)

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)


def _files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()] if root.is_dir() else []


def _registered_paths(database: Database, collections: tuple[str, ...]) -> set[str]:
    # Storage roots are shared. Archived records and other workspaces still own
    # their files; treating those files as unknown would break restore/delivery.
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT collection,json_extract(payload,'$.path') AS path FROM records "
            "WHERE json_type(payload,'$.path')='text'",
        ).fetchall()
    return {str(Path(item["path"]).resolve()) for item in rows
            if item["collection"] in collections and item["path"]}


def report(database: Database, workspace_id: str) -> dict:
    roots = {
        "session_trash": settings().trash_dir / "sessions", "artifact_trash": settings().trash_dir / "artifacts",
        "upload_trash": settings().trash_dir / "uploads", "uploads": settings().upload_dir,
        "exports": settings().export_dir, "knowledge": settings().knowledge_dir,
        "workspaces": settings().workspace_dir,
    }
    result: dict[str, Any] = {"locations": {}, "total_files": 0, "total_bytes": 0}
    for name, root in roots.items():
        files = _files(root)
        size = sum(path.stat().st_size for path in files)
        result["locations"][name] = {"files": len(files), "bytes": size}
        result["total_files"] += len(files)
        result["total_bytes"] += size
    result["records"] = {
        collection: len(database.list(collection, workspace_id=workspace_id, include_archived=True, limit=5000))
        for collection in ("sessions", "sources", "artifacts", "memories", "knowledge_documents")
    }
    return result


def workspace_preview(database: Database, workspace_id: str) -> dict:
    items = []
    for workspace in database.list("workspaces", include_archived=True, limit=5000):
        if workspace["id"] != workspace_id:
            continue
        roots = [settings().workspace_dir / workspace["id"]]
        if workspace.get("mounted_path"):
            roots.append(Path(workspace["mounted_path"]))
        size = files = 0
        for root in roots:
            owned = _files(root)
            files += len(owned)
            size += sum(path.stat().st_size for path in owned)
        items.append({"workspace_id": workspace["id"], "name": workspace.get("name"), "files": files, "bytes": size})
    return {"items": items, "dry_run": True}


def uploads_preview(database: Database, workspace_id: str) -> dict:
    registered = _registered_paths(database, ("sources", "knowledge_documents"))
    categories = {
        "registered_uploads": {"files": 0, "bytes": 0}, "knowledge": {"files": 0, "bytes": 0},
        "parsed_excel_cache": {"files": 0, "bytes": 0}, "unknown_uploads": {"files": 0, "bytes": 0},
    }
    samples, cache_samples = [], []
    for path in _files(settings().upload_dir):
        relative = path.resolve().relative_to(settings().upload_dir.resolve())
        size = path.stat().st_size
        if str(path.resolve()) in registered:
            category = "registered_uploads"
        elif relative.parts and relative.parts[0] == ".parsed_excel":
            category = "parsed_excel_cache"
        else:
            category = "unknown_uploads"
        categories[category]["files"] += 1
        categories[category]["bytes"] += size
        sample = {"filename": path.name, "relative_path": str(relative), "size_bytes": size, "category": category}
        if category == "unknown_uploads" and len(samples) < 20:
            samples.append(sample)
        elif category == "parsed_excel_cache" and len(cache_samples) < 20:
            cache_samples.append(sample)
    knowledge_files = _files(settings().knowledge_dir)
    categories["knowledge"] = {"files": len(knowledge_files), "bytes": sum(path.stat().st_size for path in knowledge_files)}
    return {"categories": categories, "samples": samples, "cache_samples": cache_samples, "missing_registered_upload_ids": [], "dry_run": True}


def _relative(value: str) -> Path:
    relative = Path(str(value or ""))
    if not str(value or "").strip() or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("文件路径无效")
    return relative


def _move_to_trash(database: Database, workspace_id: str, kind: str, source: Path, metadata: dict) -> dict:
    root = settings().trash_dir / kind
    trash_id = database.new_id("trash")
    destination = root / trash_id / source.name
    if not source.is_file() or not _within(source, settings().storage_dir):
        raise FileNotFoundError(str(source))
    destination.parent.mkdir(parents=True, exist_ok=False)
    os.replace(source, destination)
    record = database.put(
        "lifecycle_file_trash",
        {
            "id": trash_id, "workspace_id": workspace_id, "kind": kind,
            "filename": source.name, "original_path": str(source), "trash_path": str(destination),
            "size_bytes": destination.stat().st_size, "deleted_at": utcnow(), **metadata,
        },
        workspace_id=workspace_id,
    )
    database.audit(f"lifecycle.{kind}.recycled", workspace_id=workspace_id, object_type="trash", object_id=trash_id, detail={"filename": source.name})
    return record


def recycle_upload(database: Database, workspace_id: str, category: str, relative_path: str) -> dict:
    if category not in {"unknown_uploads", "parsed_excel_cache"}:
        raise ValueError("仅支持回收未知上传或 Excel 解析缓存")
    source = (settings().upload_dir / _relative(relative_path)).resolve()
    if not _within(source, settings().upload_dir):
        raise ValueError("上传文件路径无效")
    if str(source) in _registered_paths(database, ("sources", "knowledge_documents")):
        raise ValueError("文件仍属于已注册或回收站中的数据，不能按未知上传回收")
    item = _move_to_trash(database, workspace_id, "uploads", source, {"category": category})
    return {"trash_id": item["id"], "filename": item["filename"], "bytes": item["size_bytes"]}


def file_trash(database: Database, workspace_id: str, kind: str) -> list[dict]:
    return [
        {key: value for key, value in item.items() if key not in {"original_path", "trash_path", "workspace_id"}}
        for item in database.list("lifecycle_file_trash", workspace_id=workspace_id, limit=5000)
        if item.get("kind") == kind
    ]


def restore_file_trash(database: Database, workspace_id: str, trash_id: str, kind: str) -> dict:
    item = database.get("lifecycle_file_trash", trash_id)
    if not item or item.get("workspace_id") != workspace_id or item.get("kind") != kind:
        raise FileNotFoundError(trash_id)
    source, destination = Path(item["trash_path"]), Path(item["original_path"])
    if not source.is_file() or not _within(source, settings().trash_dir) or not _within(destination, settings().storage_dir):
        raise ValueError("回收站项目已损坏，无法恢复")
    if destination.exists():
        raise ValueError(f"无法恢复：{destination.name} 已存在")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
    source.parent.rmdir()
    database.delete("lifecycle_file_trash", trash_id)
    database.audit(f"lifecycle.{kind}.restored", workspace_id=workspace_id, object_type="trash", object_id=trash_id)
    return {"restored": [destination.name], "trash_id": trash_id}


def artifact_preview(database: Database, workspace_id: str) -> dict:
    active = database.list("artifacts", workspace_id=workspace_id, limit=5000)
    registered_paths = _registered_paths(database, ("artifacts", "query_results"))
    missing = [item["id"] for item in active if item.get("path") and not Path(item["path"]).is_file()]
    unknown = []
    for path in _files(settings().export_dir):
        if str(path.resolve()) not in registered_paths:
            unknown.append({"type": "exports", "filename": path.name, "relative_path": str(path.resolve().relative_to(settings().export_dir.resolve())), "size_bytes": path.stat().st_size})
    return {"registered": len(active), "missing_registered_ids": missing, "unknown_files": unknown, "unknown_bytes": sum(item["size_bytes"] for item in unknown), "dry_run": True}


def artifact_references(database: Database, workspace_id: str) -> dict:
    artifacts = database.list("artifacts", workspace_id=workspace_id, limit=5000)
    corpus = str({
        collection: database.list(collection, workspace_id=workspace_id, limit=5000)
        for collection in ("saved_sessions",)
    })
    items = []
    for artifact in artifacts:
        references = sum(corpus.count(str(token)) for token in (artifact["id"], artifact.get("filename", "")) if token)
        items.append({"artifact_id": artifact["id"], "filename": artifact.get("filename"), "references": references, "recyclable": references == 0})
    return {"items": items, "dry_run": True}


def recycle_unregistered_artifact(database: Database, workspace_id: str, artifact_type: str, relative_path: str) -> dict:
    if artifact_type not in {"exports", "charts", "export", "chart", "report"}:
        raise ValueError("产物类型无效")
    source = (settings().export_dir / _relative(relative_path)).resolve()
    if not _within(source, settings().export_dir):
        raise ValueError("产物路径无效")
    if str(source) in _registered_paths(database, ("artifacts", "query_results")):
        raise ValueError("文件仍属于已注册或回收站中的成果，不能按未知产物回收")
    item = _move_to_trash(database, workspace_id, "artifacts", source, {"artifact_type": artifact_type})
    return {"trash_id": item["id"], "filename": item["filename"], "bytes": item["size_bytes"]}


def recycle_registered_artifact(database: Database, workspace_id: str, artifact_id: str) -> dict:
    artifact = database.get("artifacts", artifact_id)
    if not artifact or artifact.get("workspace_id") != workspace_id:
        raise FileNotFoundError(artifact_id)
    path = Path(str(artifact.get("path") or ""))
    if not path.is_file():
        raise FileNotFoundError(artifact_id)
    trash = _move_to_trash(database, workspace_id, "artifacts", path, {"artifact_id": artifact_id, "artifact_type": artifact.get("kind")})
    artifact.update({"trash_id": trash["id"], "trash_path": trash["trash_path"], "original_path": trash["original_path"]})
    database.put("artifacts", artifact, workspace_id=workspace_id)
    database.archive("artifacts", artifact_id)
    return {"trash_id": trash["id"], "artifact_id": artifact_id, "filename": trash["filename"], "bytes": trash["size_bytes"]}


def restore_artifact(database: Database, workspace_id: str, trash_id: str) -> dict:
    file_item = database.get("lifecycle_file_trash", trash_id)
    if file_item and file_item.get("workspace_id") == workspace_id and file_item.get("kind") == "artifacts":
        summary = restore_file_trash(database, workspace_id, trash_id, "artifacts")
        artifact_id = file_item.get("artifact_id")
        if artifact_id:
            database.restore("artifacts", artifact_id)
            database.patch("artifacts", artifact_id, {"trash_id": "", "trash_path": "", "path": file_item["original_path"]})
        return summary
    artifact = database.get("artifacts", trash_id, include_archived=True)
    if artifact and artifact.get("workspace_id") == workspace_id and artifact.get("archived_at"):
        database.restore("artifacts", trash_id)
        return {"restored": [artifact.get("filename")], "trash_id": trash_id}
    raise FileNotFoundError(trash_id)


def archived(database: Database, workspace_id: str, collection: str) -> list[dict]:
    return [item for item in database.list(collection, workspace_id=workspace_id, include_archived=True, limit=5000) if item.get("archived_at")]


def restore_archived(database: Database, workspace_id: str, collection: str, record_id: str) -> dict:
    item = database.get(collection, record_id, include_archived=True)
    if not item or item.get("workspace_id") != workspace_id or not item.get("archived_at"):
        raise FileNotFoundError(record_id)
    database.restore(collection, record_id)
    database.audit(f"lifecycle.{collection}.restored", workspace_id=workspace_id, object_type=collection, object_id=record_id)
    return {"restored": [item.get("name") or record_id], "trash_id": record_id}


def reclaim(database: Database, workspace_id: str, *, collection: str = "", kind: str = "", retention_days: int = 30) -> dict:
    if not 0 <= retention_days <= 3650:
        raise ValueError("保留天数必须在 0 到 3650 之间")
    now = datetime.now(timezone.utc)
    removed, size = 0, 0
    if kind:
        candidates = [item for item in database.list("lifecycle_file_trash", workspace_id=workspace_id, limit=5000) if item.get("kind") == kind]
        for item in candidates:
            deleted = datetime.fromisoformat(str(item.get("deleted_at") or item.get("updated_at")))
            if (now - deleted).total_seconds() < retention_days * 86400:
                continue
            path = Path(item["trash_path"])
            if path.is_file() and _within(path, settings().trash_dir):
                size += path.stat().st_size
                path.unlink()
                shutil.rmtree(path.parent, ignore_errors=True)
            artifact_id = item.get("artifact_id")
            if artifact_id:
                database.delete("artifacts", artifact_id)
            database.delete("lifecycle_file_trash", item["id"])
            removed += 1
    if collection:
        for item in archived(database, workspace_id, collection):
            deleted = datetime.fromisoformat(str(item["archived_at"]))
            if (now - deleted).total_seconds() < retention_days * 86400:
                continue
            path_value = item.get("path")
            if path_value:
                path = Path(path_value)
                if path.is_file() and _within(path, settings().storage_dir):
                    size += path.stat().st_size
                    path.unlink()
            if collection == "sessions":
                with database.transaction() as connection:
                    connection.execute("DELETE FROM messages WHERE session_id=?", (item["id"],))
            database.delete(collection, item["id"])
            removed += 1
    database.audit("lifecycle.reclaimed", workspace_id=workspace_id, detail={"kind": kind or collection, "items": removed, "bytes": size, "retention_days": retention_days})
    return {"groups": removed, "files": removed, "items": removed, "bytes": size}
