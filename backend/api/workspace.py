from __future__ import annotations

import os
import hashlib
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, current_app, request, session as flask_session
from werkzeug.datastructures import FileStorage

from ..core.database import utcnow
from ..services.memory import consolidate_memories, search_memories
from ..services.security import SecretVault
from ..services.workspace_tools import WorkspaceFiles
from .common import (
    api_errors,
    body,
    current_user_id,
    db,
    ok,
    require_record,
    require_workspace_access,
    require_workspace_record,
    workspace_id,
    workspace_membership,
)


bp = Blueprint("workspace", __name__)


@bp.get("/api/bootstrap")
def bootstrap():
    wid = workspace_id()
    sessions = db().list("sessions", workspace_id=wid)
    active_session = next((item for item in sessions if item.get("status") == "active"), sessions[0] if sessions else None)
    return ok(
        workspaces=[item for item in db().list("workspaces") if workspace_membership(item["id"])],
        active_workspace=db().get("workspaces", wid) or db().get("workspaces", "default"),
        sessions=sessions,
        active_session=active_session,
        sources=[_public_source(item) for item in db().list("sources", workspace_id=wid)],
        providers=[
            _public_provider(item) for item in db().list("providers")
            if item["id"] == "environment-default" or item.get("workspace_id", "default") == wid
        ],
        capabilities={
            "ingestion": ["csv", "tsv", "xlsx", "xls", "json", "parquet", "sql", "http"],
            "analysis": True,
            "knowledge": True,
            "workflows": True,
            "teams": True,
            "hooks": True,
            "mcp": True,
            "exports": ["csv", "xlsx", "docx", "pptx", "html"],
        },
    )


def _public_source(item: dict) -> dict:
    value = dict(item)
    value.pop("path", None)
    value.pop("credential", None)
    return value


def _public_provider(item: dict) -> dict:
    from ..services.models import public_provider

    return public_provider(item)


@bp.get("/api/workspaces")
def list_workspaces():
    items = db().list("workspaces")
    return ok(items=[item for item in items if workspace_membership(item["id"])])


@bp.post("/api/workspaces")
@api_errors
def create_workspace():
    payload = body()
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("工作空间名称不能为空")
    record = db().put(
        "workspaces",
        {
            "id": db().new_id("ws"),
            "name": name[:80],
            "description": str(payload.get("description") or "")[:500],
            "permission": "write",
            "owner_id": current_user_id(),
        },
    )
    db().put(
        "workspace_members",
        {
            "id": f"{record['id']}:{current_user_id()}", "workspace_id": record["id"],
            "user_id": current_user_id(), "role": "owner", "enabled": True,
        },
        workspace_id=record["id"],
    )
    db().audit("workspace.created", workspace_id=record["id"], object_type="workspace", object_id=record["id"])
    return ok(item=record), 201


@bp.patch("/api/workspaces/<record_id>")
@api_errors
def update_workspace(record_id: str):
    require_workspace_access(record_id, write=True)
    allowed = {key: value for key, value in body().items() if key in {"name", "description", "permission"}}
    if "permission" in allowed and allowed["permission"] not in {"read", "write"}:
        raise ValueError("permission 必须是 read 或 write")
    return ok(item=db().patch("workspaces", record_id, allowed))


@bp.delete("/api/workspaces/<record_id>")
@api_errors
def archive_workspace(record_id: str):
    if record_id == "default":
        raise ValueError("默认工作空间不能归档")
    require_workspace_access(record_id, owner=True)
    if not db().archive("workspaces", record_id):
        raise FileNotFoundError("工作空间不存在")
    return ok(archived=True)


@bp.post("/api/workspaces/<record_id>/activate")
@api_errors
def activate_workspace(record_id: str):
    require_workspace_access(record_id)
    flask_session["active_workspace_id"] = record_id
    return ok(active_workspace_id=record_id)


@bp.get("/api/workspaces/<record_id>/members")
@api_errors
def list_workspace_members(record_id: str):
    require_workspace_access(record_id)
    users = {item["id"]: item for item in db().list("users")}
    items = []
    for member in db().list("workspace_members", workspace_id=record_id):
        user = users.get(member.get("user_id"), {})
        items.append({
            **member, "email": user.get("email", ""), "name": user.get("name", ""),
        })
    return ok(items=items)


@bp.post("/api/workspaces/<record_id>/members")
@api_errors
def add_workspace_member(record_id: str):
    require_workspace_access(record_id, owner=True)
    payload = body()
    email = str(payload.get("email") or "").strip().lower()
    user = next((item for item in db().list("users") if item.get("email") == email), None)
    if not user:
        raise FileNotFoundError("用户不存在")
    role = str(payload.get("role") or "viewer")
    if role not in {"owner", "editor", "viewer"}:
        raise ValueError("成员角色必须是 owner、editor 或 viewer")
    item = db().put(
        "workspace_members",
        {
            "id": f"{record_id}:{user['id']}", "workspace_id": record_id,
            "user_id": user["id"], "role": role, "enabled": True,
        },
        workspace_id=record_id,
    )
    return ok(item=item), 201


@bp.post("/api/workspaces/<record_id>/invitations")
@api_errors
def create_workspace_invitation(record_id: str):
    require_workspace_access(record_id, owner=True)
    payload = body()
    email = str(payload.get("email") or "").strip().lower()
    role = str(payload.get("role") or "viewer")
    if "@" not in email:
        raise ValueError("请输入有效邮箱")
    if role not in {"owner", "editor", "viewer"}:
        raise ValueError("成员角色必须是 owner、editor 或 viewer")
    if any(item.get("email") == email for item in db().list("users", include_archived=True)):
        raise ValueError("该邮箱已注册，请直接添加为工作空间成员")
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")
    invite = db().put(
        "invitations",
        {
            "id": f"invite_{token_hash}", "workspace_id": record_id, "email": email,
            "role": role, "status": "pending", "expires_at": expires_at,
            "invited_by": current_user_id(),
        },
        workspace_id=record_id,
    )
    db().audit(
        "workspace.invitation_created", workspace_id=record_id,
        object_type="invitation", object_id=invite["id"], detail={"email": email, "role": role},
    )
    return ok(
        item={key: value for key, value in invite.items() if key != "id"},
        invitation_token=token,
        registration_url=f"/?invite={token}",
    ), 201


@bp.post("/api/workspaces/<record_id>/integration-token")
@api_errors
def rotate_workspace_integration_token(record_id: str):
    require_workspace_access(record_id, owner=True)
    token = secrets.token_urlsafe(48)
    credential = db().put(
        "integration_credentials",
        {
            "id": f"integration_{record_id}", "workspace_id": record_id,
            "credential": SecretVault(current_app.config["VAULT_KEY"]).seal({"token": token}),
            "rotated_by": current_user_id(), "rotated_at": utcnow(),
        },
        workspace_id=record_id,
    )
    db().audit(
        "workspace.integration_token_rotated", workspace_id=record_id,
        object_type="integration_credential", object_id=credential["id"],
    )
    return ok(token=token)


@bp.patch("/api/workspaces/<record_id>/members/<user_id>")
@api_errors
def update_workspace_member(record_id: str, user_id: str):
    require_workspace_access(record_id, owner=True)
    role = str(body().get("role") or "")
    if role not in {"owner", "editor", "viewer"}:
        raise ValueError("成员角色必须是 owner、editor 或 viewer")
    member = require_workspace_record("workspace_members", f"{record_id}:{user_id}", record_id)
    if user_id == current_user_id() and role != "owner":
        owners = [item for item in db().list("workspace_members", workspace_id=record_id) if item.get("role") == "owner"]
        if len(owners) <= 1:
            raise ValueError("工作空间至少保留一名所有者")
    return ok(item=db().patch("workspace_members", member["id"], {"role": role}))


@bp.delete("/api/workspaces/<record_id>/members/<user_id>")
@api_errors
def remove_workspace_member(record_id: str, user_id: str):
    require_workspace_access(record_id, owner=True)
    member = require_workspace_record("workspace_members", f"{record_id}:{user_id}", record_id)
    owners = [item for item in db().list("workspace_members", workspace_id=record_id) if item.get("role") == "owner"]
    if member.get("role") == "owner" and len(owners) <= 1:
        raise ValueError("工作空间至少保留一名所有者")
    db().archive("workspace_members", member["id"])
    return ok(archived=True)


@bp.post("/api/workspaces/<record_id>/mount")
@api_errors
def mount_workspace(record_id: str):
    require_workspace_access(record_id, write=True)
    path = Path(str(body().get("path") or "")).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError("目录不存在或不是文件夹")
    if db().list("users", include_archived=True) and os.getenv("MERIDIAN_ALLOW_HOST_MOUNTS", "0") != "1":
        allowed_root = (current_app.config["SETTINGS"].workspace_dir / record_id).resolve()
        if path != allowed_root and allowed_root not in path.parents:
            raise PermissionError("服务器模式只能挂载该工作空间的受控目录")
    item = db().patch("workspaces", record_id, {"mounted_path": str(path), "mounted_at": utcnow()})
    discovered = []
    for file in sorted(path.iterdir()):
        if file.is_file() and file.suffix.lower() in {".csv", ".tsv", ".xlsx", ".xls", ".json", ".parquet"}:
            discovered.append({"name": file.name, "path": str(file), "size": file.stat().st_size})
    return ok(item=item, discovered=discovered)


@bp.post("/api/workspaces/<record_id>/unmount")
@api_errors
def unmount_workspace(record_id: str):
    require_workspace_access(record_id, write=True)
    return ok(item=db().patch("workspaces", record_id, {"mounted_path": None, "mounted_at": None}))


@bp.post("/api/workspaces/<record_id>/files/register")
@api_errors
def register_workspace_file(record_id: str):
    workspace = require_workspace_access(record_id, write=True)
    mounted = workspace.get("mounted_path")
    if not mounted:
        raise ValueError("工作空间尚未挂载本地目录")
    base = Path(mounted).resolve()
    candidate = Path(str(body().get("path") or "")).expanduser().resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError("只能登记已挂载目录内的文件")
    if not candidate.is_file():
        raise ValueError("文件不存在")
    from ..services.datasets import public_source, register_upload

    with candidate.open("rb") as stream:
        item = register_upload(FileStorage(stream=stream, filename=candidate.name), record_id)
    item["kind"] = "workspace"
    item["origin_path"] = str(candidate)
    item = db().put("sources", item, workspace_id=record_id)
    return ok(item=public_source(item)), 201


@bp.get("/api/workspaces/<record_id>/storage")
@api_errors
def workspace_storage(record_id: str):
    require_workspace_access(record_id)
    collections = ("sessions", "sources", "knowledge_documents", "memories", "artifacts", "workflows", "dashboards", "decision_maps")
    summary = []
    total_size = 0
    for collection in collections:
        items = db().list(collection, workspace_id=record_id, include_archived=True, limit=5000)
        size = sum(int(item.get("size") or 0) for item in items)
        for item in items:
            path = item.get("path")
            if path:
                try:
                    size += Path(path).stat().st_size
                except OSError:
                    pass
        total_size += size
        summary.append({"collection": collection, "records": len(items), "bytes": size, "archived": sum(bool(item.get("archived_at")) for item in items)})
    return ok(summary=summary, total_bytes=total_size)


@bp.post("/api/workspaces/<record_id>/checkpoints")
@api_errors
def create_checkpoint(record_id: str):
    workspace = require_workspace_access(record_id, write=True)
    snapshot_id = db().new_id("snap")
    files = WorkspaceFiles(db(), record_id, set(), str(body().get("session_id") or ""))
    snapshot_root = (
        current_app.config["SETTINGS"].workspace_dir / record_id / "checkpoints" / snapshot_id
    ).resolve()
    snapshot_root.mkdir(parents=True, exist_ok=True)
    file_manifest = []
    total_bytes = 0
    for namespace, root in (("outputs", files.output_root), ("user", files.user_root)):
        if root is None:
            continue
        for path in root.rglob("*"):
            if len(file_manifest) >= 1000 or total_bytes >= 512 * 1024 * 1024:
                break
            resolved_path = path.resolve()
            if snapshot_root == resolved_path or snapshot_root in resolved_path.parents:
                continue
            if path.is_symlink() or not path.is_file() or any(
                part in {".git", ".baa", "node_modules", "__pycache__", ".venv"} for part in path.parts
            ):
                continue
            size = path.stat().st_size
            if size > 64 * 1024 * 1024 or total_bytes + size > 512 * 1024 * 1024:
                continue
            relative = path.relative_to(root)
            backup = snapshot_root / namespace / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            file_manifest.append({
                "uri": files.uri(path, namespace), "backup": f"{namespace}/{relative.as_posix()}",
                "size": size,
            })
            total_bytes += size
    snapshot = {
        "id": snapshot_id,
        "workspace_id": record_id,
        "name": str(body().get("name") or f"快照 {utcnow()[:19]}")[:100],
        "state": {
            collection: db().list(collection, workspace_id=record_id)
            for collection in ("sessions", "sources", "knowledge_documents", "dashboards", "workflows", "decision_maps")
        },
        "messages": {
            item["id"]: db().messages(item["id"], 1000)
            for item in db().list("sessions", workspace_id=record_id)
        },
        "files": file_manifest,
        "file_bytes": total_bytes,
        "snapshot_path": str(snapshot_root),
        "workspace": workspace,
    }
    return ok(item=db().put("checkpoints", snapshot, workspace_id=record_id)), 201


@bp.get("/api/workspaces/<record_id>/checkpoints")
def list_checkpoints(record_id: str):
    require_workspace_access(record_id)
    return ok(items=[{
        key: value for key, value in item.items()
        if key not in {"state", "messages", "snapshot_path"}
    } for item in db().list("checkpoints", workspace_id=record_id)])


@bp.post("/api/checkpoints/<snapshot_id>/restore")
@api_errors
def restore_checkpoint(snapshot_id: str):
    snapshot = require_record("checkpoints", snapshot_id)
    require_workspace_access(snapshot["workspace_id"], write=True)
    if body().get("confirm") is not True:
        raise ValueError("恢复快照需要 confirm=true")
    wid = snapshot["workspace_id"]
    scope = str(body().get("scope") or "both")
    if scope not in {"both", "conversation", "files"}:
        raise ValueError("scope 必须是 both、conversation 或 files")
    restored = 0
    if scope in {"both", "conversation"}:
        for collection, records in snapshot.get("state", {}).items():
            for item in records:
                db().put(collection, item, workspace_id=wid)
                restored += 1
        for session_id, messages in snapshot.get("messages", {}).items():
            session = db().get("sessions", session_id)
            if session and session.get("workspace_id") == wid:
                db().replace_messages(session_id, messages)
    restored_files = 0
    if scope in {"both", "files"}:
        root = Path(str(snapshot.get("snapshot_path") or "")).resolve()
        allowed = (current_app.config["SETTINGS"].workspace_dir / wid / "checkpoints" / snapshot_id).resolve()
        if root != allowed:
            raise PermissionError("快照路径无效")
        files = WorkspaceFiles(db(), wid, set(), str(body().get("session_id") or ""))
        for item in snapshot.get("files", []):
            backup = (root / str(item.get("backup") or "")).resolve()
            if root not in backup.parents or not backup.is_file():
                raise FileNotFoundError("快照中的文件备份缺失")
            target, namespace = files.resolve(str(item["uri"]), write=True, must_exist=False)
            files._backup(target, namespace, "checkpoint_restore")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
            restored_files += 1
    db().audit("checkpoint.restored", workspace_id=wid, object_type="checkpoint", object_id=snapshot_id, detail={"records": restored})
    return ok(restored=restored, restored_files=restored_files, scope=scope)


@bp.get("/api/workspaces/<record_id>/file-history")
@api_errors
def list_file_history(record_id: str):
    require_workspace_access(record_id)
    items = db().list("file_history", workspace_id=record_id, limit=5000)
    return ok(items=[{key: value for key, value in item.items() if key != "backup_path"} for item in items])


@bp.post("/api/file-history/<version_id>/restore")
@api_errors
def restore_file_version(version_id: str):
    version = require_record("file_history", version_id)
    require_workspace_access(version["workspace_id"], write=True)
    if body().get("confirm") is not True:
        raise ValueError("恢复文件版本需要 confirm=true")
    history_root = (
        current_app.config["SETTINGS"].workspace_dir / version["workspace_id"] / "file_history"
    ).resolve()
    backup = Path(str(version.get("backup_path") or "")).resolve()
    if backup.parent != history_root or not backup.is_file():
        raise FileNotFoundError("文件历史备份不存在")
    files = WorkspaceFiles(db(), version["workspace_id"], set(), str(body().get("session_id") or ""))
    target, namespace = files.resolve(version["original_uri"], write=True, must_exist=False)
    previous = files._backup(target, namespace, "restore")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, target)
    db().audit(
        "file_version.restored", workspace_id=version["workspace_id"],
        object_type="file_history", object_id=version_id,
        detail={"uri": version["original_uri"], "previous_version_id": (previous or {}).get("id")},
    )
    return ok(
        restored=version["original_uri"], previous_version_id=(previous or {}).get("id"),
        sha256=version.get("sha256"),
    )


@bp.get("/api/sessions")
def list_sessions():
    return ok(items=db().list("sessions", workspace_id=workspace_id()))


@bp.post("/api/sessions")
@api_errors
def create_session():
    wid = workspace_id()
    source_ids = [str(value) for value in body().get("source_ids", [])]
    for source_id in source_ids:
        require_workspace_record("sources", source_id, wid)
    provider_id = body().get("provider_id")
    if provider_id and provider_id != "environment-default":
        require_workspace_record("providers", str(provider_id), wid)
    for session in db().list("sessions", workspace_id=wid):
        if session.get("status") == "active":
            db().patch("sessions", session["id"], {"status": "idle"})
    item = db().put(
        "sessions",
        {
            "id": db().new_id("ses"),
            "workspace_id": wid,
            "name": str(body().get("name") or "新分析会话")[:100],
            "status": "active",
            "source_ids": source_ids,
            "provider_id": provider_id,
        },
        workspace_id=wid,
    )
    return ok(item=item), 201


@bp.get("/api/sessions/<session_id>")
@api_errors
def get_session(session_id: str):
    item = require_workspace_record("sessions", session_id)
    return ok(item=item, messages=db().messages(session_id))


@bp.patch("/api/sessions/<session_id>")
@api_errors
def update_session(session_id: str):
    current = require_workspace_record("sessions", session_id)
    allowed = {
        key: value for key, value in body().items()
        if key in {
            "name", "status", "source_ids", "provider_id", "temporary_instruction",
            "temp_prompt_enabled", "agent_allow_mutations", "agent_allow_mcp",
        }
    }
    for flag in {"agent_allow_mutations", "agent_allow_mcp"} & allowed.keys():
        allowed[flag] = bool(allowed[flag])
    if {"agent_allow_mutations", "agent_allow_mcp"} & allowed.keys():
        require_workspace_access(current["workspace_id"], owner=True)
    if "source_ids" in allowed:
        allowed["source_ids"] = [str(value) for value in allowed["source_ids"]]
        for source_id in allowed["source_ids"]:
            require_workspace_record("sources", source_id, current["workspace_id"])
    if allowed.get("provider_id") and allowed["provider_id"] != "environment-default":
        require_workspace_record("providers", str(allowed["provider_id"]), current["workspace_id"])
    item = db().patch("sessions", session_id, allowed)
    if {"agent_allow_mutations", "agent_allow_mcp"} & allowed.keys():
        db().audit(
            "session.agent_policy_updated", workspace_id=current["workspace_id"],
            object_type="session", object_id=session_id,
            detail={key: allowed[key] for key in {"agent_allow_mutations", "agent_allow_mcp"} & allowed.keys()},
        )
    return ok(item=item)


@bp.delete("/api/sessions/<session_id>")
@api_errors
def archive_session(session_id: str):
    require_workspace_record("sessions", session_id)
    if not db().archive("sessions", session_id):
        raise FileNotFoundError("会话不存在")
    return ok(archived=True)


@bp.post("/api/sessions/<session_id>/save")
@api_errors
def save_session(session_id: str):
    session = require_workspace_record("sessions", session_id)
    snapshot = db().put(
        "saved_sessions",
        {
            "id": db().new_id("save"),
            "workspace_id": session.get("workspace_id", "default"),
            "name": str(body().get("name") or session.get("name") or "已保存会话")[:100],
            "session": session,
            "messages": db().messages(session_id, 1000),
        },
        workspace_id=session.get("workspace_id", "default"),
    )
    return ok(item={key: value for key, value in snapshot.items() if key not in {"session", "messages"}}), 201


@bp.get("/api/saved-sessions")
def saved_sessions():
    items = db().list("saved_sessions", workspace_id=workspace_id())
    public = []
    for item in items:
        value = {key: value for key, value in item.items() if key not in {"session", "messages", "history"}}
        messages = item.get("messages") or item.get("history") or []
        value.setdefault("filename", item.get("filename") or item.get("id"))
        value.setdefault("saved_at", item.get("saved_at") or item.get("created_at", ""))
        value.setdefault("is_autosave", bool(item.get("autosave") or item.get("is_autosave")))
        value.setdefault("msg_count", sum(1 for message in messages if message.get("role") in {"user", "assistant"}))
        value.setdefault("session_id", item.get("session_id") or (item.get("session") or {}).get("id", ""))
        public.append(value)
    return ok(items=public)


@bp.post("/api/saved-sessions/<saved_id>/load")
@api_errors
def load_saved_session(saved_id: str):
    saved = require_workspace_record("saved_sessions", saved_id)
    original = saved["session"]
    new_id = db().new_id("ses")
    session = db().put("sessions", {**original, "id": new_id, "name": saved["name"], "status": "active"}, workspace_id=saved["workspace_id"])
    db().replace_messages(new_id, saved.get("messages", []))
    return ok(item=session, messages=db().messages(new_id))


@bp.get("/api/memories")
def list_memories():
    user_id = str(flask_session.get("user_id") or "local-default")
    items = db().list("memories", workspace_id=workspace_id())
    return ok(items=[item for item in items if not item.get("user_id") or item.get("user_id") == user_id])


@bp.get("/api/memories/search")
def find_memories():
    return ok(items=search_memories(
        request.args.get("q", ""), workspace_id(), int(request.args.get("limit", "12")),
        str(flask_session.get("user_id") or "local-default"),
    ))


@bp.post("/api/memories")
@api_errors
def create_memory():
    payload = body()
    if not str(payload.get("title") or "").strip():
        raise ValueError("记忆标题不能为空")
    wid = workspace_id()
    memory_type = str(payload.get("type") or ("user" if payload.get("scope") == "user" else "project"))
    if memory_type not in {"user", "feedback", "project", "reference"}:
        raise ValueError("记忆类型无效")
    content = str(payload.get("body") or payload.get("content") or "")[:12000]
    item = db().put(
        "memories",
        {
            "id": db().new_id("mem"), "workspace_id": wid,
            "name": str(payload.get("name") or db().new_id("memory"))[:100],
            "title": str(payload["title"])[:120], "content": content, "body": content,
            "type": memory_type,
            "scope": str(payload.get("scope") or "workspace"), "tags": payload.get("tags", []),
            "why": str(payload.get("why") or "")[:1000],
            "how_to_apply": str(payload.get("how_to_apply") or "")[:2000],
            "user_id": str(flask_session.get("user_id") or "local-default") if memory_type in {"user", "feedback"} else "",
            "enabled": bool(payload.get("enabled", True)),
        },
        workspace_id=wid,
    )
    return ok(item=item), 201


@bp.patch("/api/memories/<record_id>")
@api_errors
def update_memory(record_id: str):
    memory = require_workspace_record("memories", record_id)
    if memory.get("user_id") and memory.get("user_id") != current_user_id():
        raise PermissionError("不能修改其他用户的个人记忆")
    allowed = {
        key: value for key, value in body().items()
        if key in {"name", "title", "content", "body", "type", "scope", "tags", "why", "how_to_apply", "enabled"}
    }
    if "body" in allowed:
        allowed["content"] = allowed["body"]
    elif "content" in allowed:
        allowed["body"] = allowed["content"]
    return ok(item=db().patch("memories", record_id, allowed))


@bp.delete("/api/memories/<record_id>")
@api_errors
def archive_memory(record_id: str):
    if body().get("confirm") is not True:
        raise ValueError("归档记忆需要 confirm=true")
    memory = require_workspace_record("memories", record_id)
    if memory.get("user_id") and memory.get("user_id") != current_user_id():
        raise PermissionError("不能归档其他用户的个人记忆")
    if not db().archive("memories", record_id):
        raise FileNotFoundError("记忆不存在")
    return ok(archived=True)


@bp.post("/api/memories/consolidate")
def consolidate_memory_records():
    return ok(result=consolidate_memories(workspace_id()))


@bp.get("/api/memory-notices")
def memory_notices():
    wid = workspace_id()
    items = db().list("memory_notices", workspace_id=wid, limit=int(request.args.get("limit", "20")))
    session_id = request.args.get("session_id")
    if session_id:
        items = [item for item in items if item.get("session_id") == session_id]
    if request.args.get("mark_read") == "true":
        for item in items:
            db().patch("memory_notices", item["id"], {"read": True})
    return ok(items=items)


@bp.get("/api/audit")
def audit_entries():
    return ok(items=db().audit_entries(workspace_id(), int(request.args.get("limit", "100"))))


@bp.get("/api/usage")
def usage_metrics():
    events = db().list("usage_events", workspace_id=workspace_id(), limit=int(request.args.get("limit", "5000")))
    by_model = {}
    for event in events:
        bucket = by_model.setdefault(event.get("model", "unknown"), {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        bucket["requests"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            bucket[key] += int(event.get(key, 0))
    totals = {"requests": len(events), "prompt_tokens": sum(int(item.get("prompt_tokens", 0)) for item in events), "completion_tokens": sum(int(item.get("completion_tokens", 0)) for item in events), "total_tokens": sum(int(item.get("total_tokens", 0)) for item in events)}
    return ok(totals=totals, by_model=by_model, events=events[:200])


@bp.get("/api/trash")
def trash():
    collections = request.args.getlist("collection") or ["sessions", "sources", "knowledge_documents", "memories", "artifacts", "workflows", "dashboards"]
    items = []
    for collection in collections:
        for item in db().list(collection, workspace_id=workspace_id(), include_archived=True):
            if item.get("archived_at"):
                items.append({"collection": collection, **item})
    return ok(items=items)


@bp.post("/api/trash/<collection>/<record_id>/restore")
@api_errors
def restore_trash(collection: str, record_id: str):
    allowed = {"sessions", "sources", "knowledge_documents", "memories", "artifacts", "workflows", "dashboards", "decision_maps"}
    item = db().get(collection, record_id, include_archived=True) if collection in allowed else None
    if not item or item.get("workspace_id", "default") != workspace_id() or not db().restore(collection, record_id):
        raise FileNotFoundError("回收站记录不存在")
    return ok(restored=True)


@bp.delete("/api/trash/<collection>/<record_id>")
@api_errors
def delete_trash(collection: str, record_id: str):
    if body().get("confirm") is not True:
        raise ValueError("永久删除需要 confirm=true")
    item = db().get(collection, record_id, include_archived=True)
    if not item or item.get("workspace_id", "default") != workspace_id() or not item.get("archived_at"):
        raise FileNotFoundError("回收站记录不存在")
    for key in ("path",):
        path_value = item.get(key)
        if path_value:
            try:
                Path(path_value).unlink(missing_ok=True)
            except OSError:
                pass
    db().delete(collection, record_id)
    return ok(deleted=True)
