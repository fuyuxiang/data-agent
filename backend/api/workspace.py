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
from ..services.authorization import actor_role, decide_source_access, filter_authorized_sessions, filter_authorized_sources
from ..services.security import SecretVault
from ..services.demo_sales import SAMPLE_SEED_ID, sample_questions
from ..services.product import SUPER_AGENT_ID, product_status, seed_demo_workspace
from ..services.workspace_tools import WorkspaceFiles
from .common import (
    api_errors,
    body,
    current_user_id,
    db,
    ok,
    require_record,
    require_session_access,
    require_source_access,
    require_system_owner,
    require_workspace_access,
    require_workspace_record,
    safe_child,
    workspace_id,
    workspace_membership,
)


bp = Blueprint("workspace", __name__)


def _validated_session_source_ids(values, wid: str) -> list[str]:
    """Return de-duplicated source ids that still exist and are accessible.

    Browser state can legitimately contain stale ids after a source has been
    archived in another tab or by an older build. Missing ids carry no access
    control meaning, so prune them. Existing but unauthorized ids still go
    through the normal permission check and are rejected.
    """

    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        source_id = str(value)
        if not source_id or source_id in seen:
            continue
        if not db().get("sources", source_id, workspace_id=wid):
            continue
        require_source_access(source_id, wid)
        result.append(source_id)
        seen.add(source_id)
    return result


@bp.get("/api/bootstrap")
def bootstrap():
    """One aggregate the whole first screen needs, so the UI renders in one round trip.

    Everything here is permission-filtered for the acting user; the admin-only
    detail views load their own data when the user actually opens them.
    """
    from ..services.semantic import visible_metrics
    from ..skills.models import CATEGORIES
    from ..skills.permissions import available_resources, filter_visible
    from ..skills.registry import SkillRegistry
    from ..services.agent_definitions import agent_source_ids, published_agents

    wid = workspace_id()
    product = product_status(db(), wid, current_user_id())
    features = set(product["entitlements"].get("features") or [])
    sessions = filter_authorized_sessions(
        db(), db().list("sessions", workspace_id=wid),
        workspace_id=wid, actor_id=current_user_id(),
    )
    active_session = next((item for item in sessions if item.get("status") == "active"), sessions[0] if sessions else None)

    available = available_resources(db(), wid, current_user_id())
    skill_registry = SkillRegistry(db(), wid)
    skills = filter_visible(skill_registry.definitions(include_disabled=False), available)
    agents = []
    for item in published_agents(db(), wid):
        if item.get("visibility", "workspace") == "private" and item.get("created_by") != current_user_id():
            continue
        sources = agent_source_ids(db(), item, current_user_id())
        if all(source_id in available.source_ids for source_id in sources):
            agents.append({**item, "source_ids": sources})
    demo_accessible = any(
        (item.get("sample_seed") or {}).get("id") == SAMPLE_SEED_ID
        and item["id"] in available.source_ids
        for item in db().list("sources", workspace_id=wid, limit=5000)
    )
    recommended = _recommended_questions(wid, demo_accessible)
    return ok(
        skills=[item.to_card() for item in skills],
        skill_categories=list(CATEGORIES),
        agents=[
            {
                "id": item["id"], "name": item.get("name"),
                "status": "published", "version": item.get("version"),
                "source_scope_mode": item.get("source_scope_mode") or "bound",
                "visibility": item.get("visibility") or "workspace",
                "description": item.get("description") or "",
                "icon": item.get("icon") or "sparkle",
                "builtin": bool(item.get("builtin")),
                "source_ids": list(item.get("source_ids") or []),
                "created_by": item.get("created_by") or "",
                "tags": list(item.get("tags") or []),
                "welcome": item.get("welcome") or "",
                "suggested_questions": (
                    list(item.get("suggested_questions") or [])
                    if item["id"] != SUPER_AGENT_ID or demo_accessible else []
                ),
            }
            for item in agents
        ],
        recommended_questions=recommended,
        metrics=[
            {"id": item["id"], "name": item.get("name"), "label": item.get("label") or item.get("name"),
             "unit": item.get("unit") or "", "status": item.get("status")}
            for item in visible_metrics(db(), wid, current_user_id())
            if item.get("status") == "approved"
        ],
        workspaces=[item for item in db().list("workspaces") if workspace_membership(item["id"])],
        active_workspace=db().get("workspaces", wid) or db().get("workspaces", "default"),
        active_membership=workspace_membership(wid),
        sessions=sessions,
        active_session=active_session,
        sources=[
            _public_source(item) for item in filter_authorized_sources(
                db(), db().list("sources", workspace_id=wid),
                workspace_id=wid, actor_id=current_user_id(),
            )
        ],
        providers=[
            _public_provider(item) for item in db().list("providers")
            if item["id"] == "environment-default" or item.get("workspace_id", "default") == wid
        ],
        product=product,
        entitlements=product["entitlements"],
        onboarding=product["onboarding"],
        capabilities={
            "ingestion": ["csv", "tsv", "xlsx", "xls", "json", "parquet", "sql", "http"],
            "analysis": "governed_agent" in features,
            "knowledge": "knowledge_base" in features,
            "semantic_layer": "semantic_layer" in features,
            "mcp": "mcp_integrations" in features,
            "exports": ["csv", "xlsx", "docx", "pptx", "png"] if "result_delivery" in features else [],
        },
    )


def _recommended_questions(wid: str, demo_accessible: bool) -> list[str]:
    """Questions the workbench should offer *right now*.

    Demo workspaces get the documented sample questions; everyone else gets the
    questions their own agents declare, so the first screen is never generic.
    """
    if demo_accessible:
        return sample_questions()[:4]
    from ..services.agent_definitions import agent_source_ids, published_agents

    questions: list[str] = []
    for agent in published_agents(db(), wid):
        if agent.get("id") == SUPER_AGENT_ID:
            continue
        if agent.get("visibility") == "private" and agent.get("created_by") != current_user_id():
            continue
        try:
            for source_id in agent_source_ids(db(), agent, current_user_id()):
                require_source_access(str(source_id), wid, action="analyze")
        except (FileNotFoundError, PermissionError):
            continue
        questions.extend(str(value) for value in agent.get("suggested_questions") or [])
    return list(dict.fromkeys(questions))[:6]


@bp.post("/api/demo/seed")
@api_errors
def seed_demo():
    wid = workspace_id()
    require_workspace_access(wid, write=True)
    result = seed_demo_workspace(db(), wid, current_user_id())
    return ok(
        created=result["created"],
        source=_public_source(result["source"]),
        summary=result["summary"],
        onboarding=result["onboarding"],
        recommended_questions=sample_questions()[:4],
    )


def _public_source(item: dict) -> dict:
    value = dict(item)
    value.pop("path", None)
    value.pop("credential", None)
    value.pop("row_policy", None)
    value.pop("column_policies", None)
    if item.get("row_policy") or item.get("column_policies"):
        value["tables"] = [
            {"name": table.get("name"), "source_name": table.get("source_name")}
            for table in item.get("tables") or []
        ]
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
    require_system_owner()
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
    if role not in {"owner", "editor", "analyst", "viewer"}:
        raise ValueError("成员角色必须是 owner、editor、analyst 或 viewer")
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
    if role not in {"owner", "editor", "analyst", "viewer"}:
        raise ValueError("成员角色必须是 owner、editor、analyst 或 viewer")
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
    if role not in {"owner", "editor", "analyst", "viewer"}:
        raise ValueError("成员角色必须是 owner、editor、analyst 或 viewer")
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
    require_workspace_access(record_id, owner=True)
    collections = ("sessions", "sources", "knowledge_documents", "artifacts")
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
    workspace = require_workspace_access(record_id, owner=True)
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
            for collection in ("sessions", "sources", "knowledge_documents")
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
    require_workspace_access(record_id, owner=True)
    return ok(items=[{
        key: value for key, value in item.items()
        if key not in {"state", "messages", "snapshot_path"}
    } for item in db().list("checkpoints", workspace_id=record_id)])


@bp.post("/api/checkpoints/<snapshot_id>/restore")
@api_errors
def restore_checkpoint(snapshot_id: str):
    snapshot = require_record("checkpoints", snapshot_id)
    require_workspace_access(snapshot["workspace_id"], owner=True)
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
    wid = workspace_id()
    return ok(items=filter_authorized_sessions(
        db(), db().list("sessions", workspace_id=wid),
        workspace_id=wid, actor_id=current_user_id(),
    ))


@bp.post("/api/sessions")
@api_errors
def create_session():
    wid = workspace_id()
    payload = body()
    if payload.get("business_space_id"):
        raise ValueError("业务数据空间已停用，请直接选择数据源")
    source_ids = _validated_session_source_ids(payload.get("source_ids", []), wid)
    provider_id = payload.get("provider_id")
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
            "name": str(payload.get("name") or "新分析会话")[:100],
            "status": "active",
            "source_ids": source_ids,
            "provider_id": provider_id,
            "owner_id": current_user_id(),
        },
        workspace_id=wid,
    )
    return ok(item=item), 201


@bp.get("/api/sessions/<session_id>")
@api_errors
def get_session(session_id: str):
    item = require_session_access(session_id)
    return ok(item=item, messages=db().messages(session_id))


@bp.patch("/api/sessions/<session_id>")
@api_errors
def update_session(session_id: str):
    current = require_session_access(session_id)
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
        allowed["source_ids"] = _validated_session_source_ids(
            allowed["source_ids"], current["workspace_id"],
        )
        if "business_space_id" not in allowed:
            # A direct source selection replaces the legacy space snapshot.
            allowed["business_space_id"] = None
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
    require_session_access(session_id)
    if not db().archive("sessions", session_id):
        raise FileNotFoundError("会话不存在")
    return ok(archived=True)


@bp.post("/api/sessions/<session_id>/save")
@api_errors
def save_session(session_id: str):
    session = require_session_access(session_id)
    snapshot = db().put(
        "saved_sessions",
        {
            "id": db().new_id("save"),
            "workspace_id": session.get("workspace_id", "default"),
            "owner_id": current_user_id(),
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
        if not _saved_session_owned(item):
            continue
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
    if not _saved_session_owned(saved):
        raise FileNotFoundError("已保存会话不存在")
    original = saved["session"]
    source_ids = []
    for source_id in original.get("source_ids") or []:
        try:
            require_source_access(str(source_id), saved["workspace_id"])
        except (FileNotFoundError, PermissionError):
            continue
        source_ids.append(str(source_id))
    new_id = db().new_id("ses")
    session = db().put(
        "sessions",
        {
            **original, "id": new_id, "name": saved["name"], "status": "active",
            "source_ids": source_ids, "owner_id": current_user_id(), "visibility": "private",
        },
        workspace_id=saved["workspace_id"],
    )
    db().replace_messages(new_id, saved.get("messages", []))
    return ok(item=session, messages=db().messages(new_id))


def _saved_session_owned(item: dict) -> bool:
    owner_id = str(item.get("owner_id") or (item.get("session") or {}).get("owner_id") or "")
    if owner_id:
        return owner_id == current_user_id()
    membership = workspace_membership(str(item.get("workspace_id") or workspace_id()))
    return bool(membership and membership.get("role") == "owner")


@bp.get("/api/audit")
@api_errors
def audit_entries():
    require_workspace_access(workspace_id(), owner=True)
    return ok(items=db().audit_entries(workspace_id(), int(request.args.get("limit", "100"))))


@bp.get("/api/usage")
@api_errors
def usage_metrics():
    require_workspace_access(workspace_id(), owner=True)
    events = db().list("usage_events", workspace_id=workspace_id(), limit=int(request.args.get("limit", "5000")))
    by_model = {}
    for event in events:
        bucket = by_model.setdefault(event.get("model", "unknown"), {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        bucket["requests"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            bucket[key] += int(event.get(key, 0))
    totals = {"requests": len(events), "prompt_tokens": sum(int(item.get("prompt_tokens", 0)) for item in events), "completion_tokens": sum(int(item.get("completion_tokens", 0)) for item in events), "total_tokens": sum(int(item.get("total_tokens", 0)) for item in events)}
    return ok(totals=totals, by_model=by_model, events=events[:200])


_TRASH_COLLECTIONS = {
    "sessions", "sources", "knowledge_documents", "knowledge_entries",
    "artifacts", "saved_sessions", "agent_runs",
}


def _trash_access(collection: str, item: dict, wid: str, role: str) -> bool:
    """Recycle-bin access retains the original resource's ownership and ACL."""
    actor = current_user_id()
    if collection in {"knowledge_documents", "knowledge_entries"}:
        return role in {"owner", "editor"} and item.get("visibility") != "analysis_attachment"
    if collection == "sources":
        return role == "owner" and decide_source_access(
            db(), item, workspace_id=wid, actor_id=actor,
        ).allowed
    if collection == "agent_runs":
        return item.get("actor_id") == actor
    if collection in {"sessions", "saved_sessions"}:
        owner = item.get("owner_id") or (item.get("session") or {}).get("owner_id")
        return owner == actor or not owner and role == "owner"
    if collection == "artifacts":
        from .library import _actor_visible

        return bool(_actor_visible([item]))
    return False


def _trash_record(collection: str, record_id: str) -> tuple[dict, str]:
    wid = workspace_id()
    require_workspace_access(wid)
    role = actor_role(db(), wid, current_user_id())
    if collection not in _TRASH_COLLECTIONS or collection == "agent_runs":
        raise FileNotFoundError("回收站记录不存在")
    item = db().get(collection, record_id, workspace_id=wid, include_archived=True)
    if not item or not item.get("archived_at") or not _trash_access(collection, item, wid, role):
        raise FileNotFoundError("回收站记录不存在")
    return item, role


def _session_has_analysis(session_id: str, wid: str) -> bool:
    with db().connect() as connection:
        return bool(connection.execute(
            "SELECT 1 FROM agent_runs WHERE session_id=? AND workspace_id=? LIMIT 1",
            (session_id, wid),
        ).fetchone())


def _analysis_restore_block_reason(item: dict, wid: str, role: str) -> str:
    if role not in {"owner", "editor", "analyst"}:
        return "当前为只读权限，无法恢复分析。"
    missing_sources = [str(source_id) for source_id in item.get("source_scope") or []
                       if not db().get("sources", str(source_id), workspace_id=wid)]
    if any(not db().get("sources", source_id, workspace_id=wid, include_archived=True)
           for source_id in missing_sources):
        return "分析使用的数据源已永久删除，无法恢复原分析；可用新数据重新发起分析。"
    if missing_sources:
        return "请先恢复分析使用的数据源；无管理权限时，请联系工作空间所有者。"
    from ..services.advanced_agent import _source_authorized

    if not _source_authorized(db(), item):
        return "数据权限或授权规则已变更，请联系工作空间所有者恢复原授权。"
    parent = db().get("sessions", item["session_id"], workspace_id=wid, include_archived=True)
    if not parent:
        return "原会话已不存在，无法恢复到原会话。"
    owner = parent.get("owner_id")
    if owner == current_user_id() or (not owner and role == "owner"):
        return ""
    if parent.get("visibility") == "workspace":
        return "请先由会话所有者恢复原会话。" if parent.get("archived_at") else ""
    return "原会话的归属已变更，无法恢复到其他成员的私有会话。"


@bp.get("/api/trash")
@api_errors
def trash():
    from ..agent.store import RunStore

    wid = workspace_id()
    require_workspace_access(wid)
    role = actor_role(db(), wid, current_user_id())
    collections = request.args.getlist("collection") or sorted(_TRASH_COLLECTIONS)
    if any(collection not in _TRASH_COLLECTIONS for collection in collections):
        raise ValueError("回收站类型无效")
    items = []
    store = RunStore(db())
    for collection in dict.fromkeys(collections):
        records = (
            store.list_runs(wid, archived_only=True, actor_id=current_user_id(), limit=5000)
            if collection == "agent_runs" else
            db().list(collection, workspace_id=wid, archived_only=True, limit=5000)
        )
        for item in records:
            if not item.get("archived_at") or not _trash_access(collection, item, wid, role):
                continue
            contract = store.latest_contract(item["id"]) if collection == "agent_runs" else None
            title = (contract or {}).get("payload", {}).get("objective") or item.get("name") or item.get("title")
            restore_reason = _analysis_restore_block_reason(item, wid, role) if collection == "agent_runs" else ""
            items.append({
                "id": item["id"], "collection": collection,
                "title": str(title or item.get("filename") or "未命名内容")[:200],
                "name": item.get("name") or "", "archived_at": item["archived_at"],
                "session_id": item.get("session_id") or "",
                "can_restore": role in {"owner", "editor", "analyst"} and not restore_reason,
                "restore_block_reason": restore_reason,
                "can_delete": (role == "owner" and collection != "agent_runs"
                               and not (collection == "sessions" and _session_has_analysis(item["id"], wid))),
            })
    items.sort(key=lambda item: item["archived_at"], reverse=True)
    return ok(items=items)


@bp.post("/api/trash/<collection>/<record_id>/restore")
@api_errors
def restore_trash(collection: str, record_id: str):
    with db().transaction():
        return _restore_trash(collection, record_id)


def _restore_trash(collection: str, record_id: str):
    item, role = _trash_record(collection, record_id)
    if role not in {"owner", "editor", "analyst"}:
        raise PermissionError("当前成员只有只读权限")
    if collection == "artifacts" and item.get("trash_id"):
        from ..services.lifecycle import restore_artifact

        restore_artifact(db(), workspace_id(), item["trash_id"])
    elif not db().restore(collection, record_id, workspace_id=workspace_id()):
        raise FileNotFoundError("回收站记录不存在")
    db().audit("trash.restored", workspace_id=workspace_id(), actor=current_user_id(),
               object_type=collection, object_id=record_id)
    return ok(restored=True)


@bp.delete("/api/trash/<collection>/<record_id>")
@api_errors
def delete_trash(collection: str, record_id: str):
    require_workspace_access(workspace_id(), owner=True)
    if body().get("confirm") is not True:
        raise ValueError("永久删除需要 confirm=true")
    with db().transaction():
        return _delete_trash(collection, record_id)


def _delete_trash(collection: str, record_id: str):
    item, _role = _trash_record(collection, record_id)
    if collection == "sessions" and _session_has_analysis(record_id, workspace_id()):
        raise ValueError("会话包含需保留的分析记录，可恢复但不能永久删除")
    if collection == "sources":
        from ..services.agent_definitions import agent_references

        if agent_references(db(), workspace_id(), "source_ids", record_id):
            raise ValueError("数据源仍被智能体草稿或发布版本引用，请先解除引用再永久删除")
    if collection == "knowledge_documents":
        from ..services.knowledge import document_references

        references = document_references(db(), record_id, workspace_id(), actor_id=current_user_id())
        if references:
            return {"ok": False, "error": "文档仍被智能体引用，请先解除引用再永久删除", "references": references}, 409
    for key in ("path", "trash_path"):
        path_value = item.get(key)
        if path_value:
            safe_child(current_app.config["SETTINGS"].storage_dir, Path(path_value)).unlink(missing_ok=True)
    if collection == "sessions":
        with db().transaction() as connection:
            connection.execute("DELETE FROM messages WHERE session_id=?", (record_id,))
    if collection == "artifacts" and item.get("trash_id"):
        db().delete("lifecycle_file_trash", item["trash_id"], workspace_id=workspace_id())
    db().delete(collection, record_id, workspace_id=workspace_id())
    db().audit("trash.deleted", workspace_id=workspace_id(), actor=current_user_id(),
               object_type=collection, object_id=record_id)
    return ok(deleted=True)
