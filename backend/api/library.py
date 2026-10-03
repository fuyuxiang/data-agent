"""资料库：用户可见的成果集合。

内部叫 Artifact，界面上叫资料库——这是行业成熟术语，不应该让用户再学一遍。
本蓝图把散落的成果、导出文件和可保存的分析结果聚合成一个可搜索、可分类、
可预览的列表，并提供收藏、重命名与「围绕该文件继续提问」所需的上下文。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, request, send_file

from ..agent.store import RunStore
from ..core.database import utcnow
from ..services.authorization import actor_role
from ..services.results.delivery import ARTIFACT_KINDS
from .common import (
    api_errors, body, current_user_id, db, ok, require_workspace_access, safe_child,
    workspace_id,
)

bp = Blueprint("library", __name__)


def _require_personal_write() -> None:
    wid = workspace_id()
    require_workspace_access(wid)
    if actor_role(db(), wid, current_user_id()) not in {"owner", "editor", "analyst"}:
        raise PermissionError("当前成员只有只读权限")

# (分类名, 图标, 归入该分类的成果类型)
CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("报告", "report", ("summary_docx", "report_docx")),
    ("演示文稿", "presentation", ("report_pptx",)),
    ("表格", "table", ("data_xlsx",)),
    ("网页", "code", ("report_html",)),
    ("图片", "image", ("dashboard_png",)),
    ("分析结果", "analysis", ("conversation_export",)),
    ("上传文件", "upload", ("upload",)),
)

_CATEGORY_BY_KIND: dict[str, str] = {
    kind: label for label, _icon, kinds in CATEGORIES for kind in kinds
}
_CATEGORY_ICON = {label: icon for label, icon, _kinds in CATEGORIES}
# 允许在浏览器里直接预览的类型；其余一律走下载。
_PREVIEWABLE = {".png", ".jpg", ".jpeg", ".webp", ".html", ".htm", ".csv", ".pdf", ".json", ".md", ".txt"}


def _size(record: dict[str, Any]) -> int:
    try:
        return int(record.get("size_bytes") or record.get("size") or 0)
    except (TypeError, ValueError):
        return 0


def _category_of(record: dict[str, Any]) -> str:
    return str(record.get("category") or _CATEGORY_BY_KIND.get(str(record.get("kind") or "")) or "上传文件")


def _public(record: dict[str, Any]) -> dict[str, Any]:
    kind = str(record.get("kind") or "upload")
    category = _category_of(record)
    suffix = Path(str(record.get("path") or record.get("filename") or "")).suffix.lower()
    return {
        "id": record["id"],
        "kind": kind,
        "category": category,
        "icon": record.get("icon") or _CATEGORY_ICON.get(category, "file"),
        "title": str(record.get("title") or record.get("filename") or "未命名成果")[:120],
        "filename": record.get("filename") or "",
        "extension": suffix,
        "size_bytes": _size(record),
        "created_at": record.get("created_at") or "",
        "run_id": record.get("run_id") or "",
        "session_id": record.get("session_id") or "",
        "favorite": bool(record.get("favorite")),
        "previewable": suffix in _PREVIEWABLE,
        "download_url": f"/api/library/{record['id']}/download",
    }


def _actor_visible(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Analysis-owned files stay private to the run owner."""
    actor = current_user_id()
    role = actor_role(db(), workspace_id(), actor)
    visible: list[dict[str, Any]] = []
    for item in items:
        run_id = str(item.get("run_id") or "")
        if run_id:
            run = RunStore(db()).get_run(run_id, workspace_id=workspace_id())
            if not run or run.get("actor_id") != actor:
                continue
            try:
                from ..services.advanced_agent import _source_authorized
                from .delivery import _artifact_policy_access

                _artifact_policy_access(item, source_ids=run.get("source_scope") or [])
                if not _source_authorized(db(), run):
                    continue
            except (FileNotFoundError, PermissionError):
                continue
            visible.append(item)
            continue
        owner = str(item.get("actor_id") or item.get("owner_id") or "")
        if owner and owner != actor and role != "owner":
            continue
        visible.append(item)
    return visible


def _collect() -> list[dict[str, Any]]:
    wid = workspace_id()
    return [
        *db().list("artifacts", workspace_id=wid, limit=5000),
        *db().list("saved_sessions", workspace_id=wid, limit=5000),
    ]


def _collection_of(record_id: str) -> str:
    wid = workspace_id()
    for collection in ("artifacts", "saved_sessions"):
        if any(item["id"] == record_id for item in db().list(collection, workspace_id=wid, limit=5000)):
            return collection
    raise FileNotFoundError(f"资料不存在：{record_id}")


def _resolve(record_id: str) -> tuple[dict[str, Any], str]:
    for record in _actor_visible(_collect()):
        if record["id"] == record_id:
            return record, _collection_of(record_id)
    raise FileNotFoundError(f"资料不存在：{record_id}")


@bp.get("/api/library")
@api_errors
def library():
    require_workspace_access(workspace_id())
    visible = _actor_visible(_collect())
    items = [_public(item) for item in visible]
    counts: dict[str, int] = {"全部": len(items)}
    for record in visible:
        category = _category_of(record)
        counts[category] = counts.get(category, 0) + 1

    query = str(request.args.get("q") or "").strip().lower()
    category = str(request.args.get("category") or "").strip()
    if query:
        items = [
            item for item in items
            if query in item["title"].lower() or query in item["filename"].lower()
        ]
    if category and category != "全部":
        items = [item for item in items if item["category"] == category]
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)

    return ok(
        items=items,
        categories=[
            {"key": "全部", "icon": "library", "count": counts.get("全部", 0)},
            *(
                {"key": label, "icon": _CATEGORY_ICON.get(label, "file"), "count": counts.get(label, 0)}
                for label, _icon, _kinds in CATEGORIES
            ),
        ],
        total=counts.get("全部", 0),
        counts=counts,
        artifact_kinds=list(ARTIFACT_KINDS),
    )


@bp.get("/api/library/<record_id>")
@api_errors
def library_detail(record_id: str):
    require_workspace_access(workspace_id())
    record, _collection = _resolve(record_id)
    payload = _public(record)
    payload["source_ids"] = list(record.get("source_ids") or [])
    payload["manifest_id"] = record.get("manifest_id") or ""
    return ok(item=payload)


@bp.get("/api/library/<record_id>/download")
@api_errors
def download(record_id: str):
    require_workspace_access(workspace_id())
    record, _collection = _resolve(record_id)
    if actor_role(db(), workspace_id(), current_user_id()) not in {"owner", "editor", "analyst"}:
        raise PermissionError("当前成员没有文件导出权限")
    if record.get("run_id"):
        from .delivery import _artifact_policy_access

        run = RunStore(db()).get_run(str(record["run_id"]), workspace_id=workspace_id())
        _artifact_policy_access(record, action="export", source_ids=(run or {}).get("source_scope") or [])
    path = safe_child(current_app.config["SETTINGS"].export_dir, Path(str(record.get("path") or "")))
    if not path.is_file():
        raise FileNotFoundError("资料文件已不存在")
    inline = path.suffix.lower() in _PREVIEWABLE
    return send_file(
        path,
        mimetype=str(record.get("content_type") or "") or None,
        as_attachment=not inline,
        download_name=str(record.get("filename") or path.name),
    )


@bp.get("/api/library/<record_id>/preview")
@api_errors
def preview(record_id: str):
    """Inline preview for the formats a browser renders on its own."""
    require_workspace_access(workspace_id())
    record, _collection = _resolve(record_id)
    path = safe_child(current_app.config["SETTINGS"].export_dir, Path(str(record.get("path") or "")))
    if not path.is_file():
        raise FileNotFoundError("资料文件已不存在")
    if path.suffix.lower() not in _PREVIEWABLE:
        raise ValueError("该格式不支持在线预览，请下载后查看")
    if path.suffix.lower() in {".html", ".htm"}:
        # 成果 HTML 在生成时已转义全部插值；这里再限制一次外链能力。
        return Response(
            path.read_text(encoding="utf-8"),
            mimetype="text/html",
            headers={
                "Content-Security-Policy":
                    "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
            },
        )
    return send_file(path, as_attachment=False, download_name=path.name)


@bp.patch("/api/library/<record_id>")
@api_errors
def update_record(record_id: str):
    _require_personal_write()
    record, collection = _resolve(record_id)
    payload = body()
    changes: dict[str, Any] = {}
    if "favorite" in payload:
        # 收藏名就是显示名；清空时回落到原始标题。
        alias = str(payload["favorite"] or "").strip()[:120]
        changes["favorite"] = alias
        changes["title"] = alias or record.get("title", "")
    elif "title" in payload:
        title = str(payload["title"] or "").strip()
        if not title:
            raise ValueError("名称不能为空")
        changes["title"] = title[:120]
    if not changes:
        raise ValueError("没有可更新的内容")
    updated = db().patch(collection, record["id"], changes, workspace_id=workspace_id())
    return ok(item=_public(updated))


@bp.delete("/api/library/<record_id>")
@api_errors
def remove(record_id: str):
    _require_personal_write()
    record, collection = _resolve(record_id)
    db().archive(collection, record["id"], workspace_id=workspace_id())
    db().audit("library.deleted", workspace_id=workspace_id(), actor=current_user_id(),
               object_type=collection, object_id=record["id"])
    return ok(item=_public(record))


@bp.post("/api/library")
@api_errors
def record_upload():
    """Register a workspace-produced file so it can live in 资料库."""
    _require_personal_write()
    uploads = request.files.getlist("file")
    if not uploads:
        raise ValueError("请选择要保存的文件")
    if len(uploads) > 20:
        raise ValueError("单次最多上传 20 个文件")
    for upload in uploads:
        position = upload.stream.tell()
        upload.stream.seek(0, 2)
        size = upload.stream.tell()
        upload.stream.seek(position)
        if size > 50 * 1024 * 1024:
            raise ValueError(f"文件 {upload.filename} 超过 50MB")
    wid = workspace_id()
    export_dir = current_app.config["SETTINGS"].export_dir
    export_dir.mkdir(parents=True, exist_ok=True)
    items = []
    paths: list[Path] = []
    records: list[str] = []
    try:
        for upload in uploads:
            record_id = db().new_id("art")
            original = Path(upload.filename or "upload.bin").name
            target = safe_child(export_dir, export_dir / f"{record_id}{Path(original).suffix.lower()}")
            paths.append(target)
            upload.save(target)
            item = db().put("artifacts", {
                "id": record_id, "workspace_id": wid,
                "kind": "upload", "category": "上传文件",
                "title": Path(original).stem[:120], "filename": original, "path": str(target),
                "size_bytes": target.stat().st_size, "content_type": upload.mimetype or "",
                "status": "ready", "immutable": False,
                "actor_id": current_user_id(), "created_at": utcnow(),
            }, workspace_id=wid)
            records.append(record_id)
            items.append(_public(item))
    except Exception:
        for record_id in records:
            db().archive("artifacts", record_id, workspace_id=wid)
        for path in paths:
            path.unlink(missing_ok=True)
        raise
    return ok(item=items[0], items=items), 201
