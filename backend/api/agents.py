"""Versioned, workspace-scoped analysis Agent definitions."""

from __future__ import annotations

from flask import Blueprint

from ..services.authorization import actor_role
from ..services.skills import get_skill, require_formal_skill
from .common import (
    api_errors, body, current_user_id, db, ok, require_source_access,
    require_workspace_access, require_workspace_record, workspace_id,
)


bp = Blueprint("agents", __name__)


def _public(item: dict, *, admin: bool) -> dict:
    if admin:
        return item
    return {key: item.get(key) for key in (
        "id", "workspace_id", "name", "description", "version", "status", "source_ids",
        "knowledge_document_ids", "provider_id", "skill_id", "published_at",
    )}


def _validated(payload: dict, current: dict | None = None) -> dict:
    merged = {**(current or {}), **payload}
    name = str(merged.get("name") or "").strip()
    if not name or len(name) > 100:
        raise ValueError("智能体名称须为 1–100 字")
    source_ids = merged.get("source_ids") or []
    if not isinstance(source_ids, list) or len(source_ids) > 100:
        raise ValueError("智能体数据源范围无效")
    source_ids = list(dict.fromkeys(str(value) for value in source_ids))
    for source_id in source_ids:
        require_source_access(source_id, action="analyze")
    knowledge_ids = merged.get("knowledge_document_ids") or []
    if not isinstance(knowledge_ids, list) or len(knowledge_ids) > 100:
        raise ValueError("智能体知识范围无效")
    knowledge_ids = list(dict.fromkeys(str(value) for value in knowledge_ids))
    for document_id in knowledge_ids:
        document = require_workspace_record("knowledge_documents", document_id)
        if document.get("enabled") is False or document.get("visibility") == "analysis_attachment":
            raise ValueError("智能体只能使用已启用的公共业务知识")
    provider_id = str(merged.get("provider_id") or "") or None
    if provider_id and provider_id != "environment-default":
        require_workspace_record("providers", provider_id)
    skill_id = str(merged.get("skill_id") or "") or None
    if skill_id:
        skill = get_skill(skill_id, workspace_id())
        if not skill or (skill.get("status") and skill.get("status") != "published"):
            raise ValueError("智能体只能绑定已发布的 Skill")
        require_formal_skill(skill)
    instruction = str(merged.get("instruction") or "").strip()
    if len(instruction) > 16_000:
        raise ValueError("智能体说明超过 16000 字")
    return {
        "name": name, "description": str(merged.get("description") or "").strip()[:1000],
        "instruction": instruction, "source_ids": source_ids,
        "knowledge_document_ids": knowledge_ids, "provider_id": provider_id, "skill_id": skill_id,
    }


@bp.get("/api/agents")
@api_errors
def list_agents():
    wid = workspace_id()
    require_workspace_access(wid)
    admin = actor_role(db(), wid, current_user_id()) in {"owner", "editor"}
    items = []
    for item in db().list("agent_definitions", workspace_id=wid, limit=5000):
        if not admin and item.get("status") != "published":
            continue
        try:
            for source_id in item.get("source_ids") or []:
                require_source_access(str(source_id), action="analyze")
        except (FileNotFoundError, PermissionError):
            continue
        items.append(_public(item, admin=admin))
    return ok(items=items)


@bp.post("/api/agents")
@api_errors
def create_agent():
    wid = workspace_id()
    require_workspace_access(wid, write=True)
    definition = _validated(body())
    item = db().put("agent_definitions", {
        "id": db().new_id("agent"), "workspace_id": wid, **definition,
        "status": "draft", "version": 1, "created_by": current_user_id(),
    }, workspace_id=wid)
    return ok(item=item), 201


@bp.patch("/api/agents/<agent_id>")
@api_errors
def update_agent(agent_id: str):
    item = require_workspace_record("agent_definitions", agent_id)
    require_workspace_access(item["workspace_id"], write=True)
    definition = _validated(body(), item)
    updated = db().patch("agent_definitions", agent_id, {
        **definition, "status": "draft", "version": int(item.get("version") or 1) + 1,
        "published_at": None,
    }, workspace_id=item["workspace_id"])
    return ok(item=updated)


@bp.post("/api/agents/<agent_id>/publish")
@api_errors
def publish_agent(agent_id: str):
    item = require_workspace_record("agent_definitions", agent_id)
    require_workspace_access(item["workspace_id"], owner=True)
    definition = _validated({}, item)
    if not definition["source_ids"]:
        raise ValueError("数据分析智能体至少需要一个数据源")
    from ..core.database import utcnow

    snapshot = {**definition, "id": item["id"], "version": item["version"], "workspace_id": item["workspace_id"]}
    db().put("agent_versions", {
        "id": f"{agent_id}:{item['version']}", "workspace_id": item["workspace_id"],
        "agent_id": agent_id, "version": item["version"], "snapshot": snapshot,
        "published_by": current_user_id(), "published_at": utcnow(),
    }, workspace_id=item["workspace_id"])
    updated = db().patch("agent_definitions", agent_id, {
        "status": "published", "published_at": utcnow(),
        "validation_status": "configuration_checked",
    }, workspace_id=item["workspace_id"])
    db().audit("agent.published", workspace_id=item["workspace_id"], actor=current_user_id(),
               object_type="agent_definition", object_id=agent_id, detail={"version": item["version"]})
    return ok(item=updated)


@bp.post("/api/agents/<agent_id>/rollback")
@api_errors
def rollback_agent(agent_id: str):
    item = require_workspace_record("agent_definitions", agent_id)
    require_workspace_access(item["workspace_id"], owner=True)
    version = int(body().get("version") or 0)
    old = require_workspace_record("agent_versions", f"{agent_id}:{version}", item["workspace_id"])
    definition = _validated(old["snapshot"], item)
    updated = db().patch("agent_definitions", agent_id, {
        **definition, "status": "draft", "version": int(item["version"]) + 1,
        "published_at": None,
    }, workspace_id=item["workspace_id"])
    return ok(item=updated)
