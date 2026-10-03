"""Versioned, workspace-scoped analysis Agent definitions."""

from __future__ import annotations

from flask import Blueprint

from ..services.authorization import actor_role
from .common import (
    api_errors, body, current_user_id, db, ok, require_source_access,
    require_workspace_access, require_workspace_record, workspace_id,
)


bp = Blueprint("agents", __name__)


def _check_private_owner(item: dict) -> None:
    if item.get("visibility") == "private" and item.get("created_by") != current_user_id():
        raise FileNotFoundError("智能体不存在")


def _public(item: dict, *, admin: bool) -> dict:
    if admin:
        return {**item, "skill_ids": list(item.get("skill_ids") or ([item["skill_id"]] if item.get("skill_id") else []))}
    return {key: item.get(key) for key in (
        "id", "workspace_id", "name", "description", "version", "status", "source_ids",
        "knowledge_document_ids", "provider_id", "skill_id", "skill_ids", "metric_ids",
        "mcp_server_ids", "icon", "tags", "welcome", "suggested_questions", "visibility",
        "published_at",
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

    # Skills: a resource list, not a single prompt.  Every one must exist, be
    # published, and stay inside the governed tool surface.
    from ..skills.models import FORMAL_AGENT_TOOLS
    from ..skills.registry import SkillRegistry

    wid = workspace_id()
    registry = SkillRegistry(db(), wid)
    requested = merged.get("skill_ids")
    if requested is None:
        requested = [merged.get("skill_id")] if merged.get("skill_id") else []
    skill_ids: list[str] = []
    for skill_id in dict.fromkeys(str(value) for value in requested if value):
        definition = registry.get(skill_id)
        if definition is None:
            raise ValueError(f"技能不存在：{skill_id}")
        if definition.status != "published":
            raise ValueError("智能体只能绑定已发布的 Skill")
        unsupported = sorted(set(definition.allowed_tools) - FORMAL_AGENT_TOOLS)
        if unsupported:
            raise ValueError(f"Skill 使用了正式分析不支持的工具：{'、'.join(unsupported)}")
        skill_ids.append(definition.id)

    metric_ids = list(dict.fromkeys(str(value) for value in merged.get("metric_ids") or []))
    for metric_id in metric_ids:
        metric = require_workspace_record("semantic_metrics", metric_id, wid)
        model = require_workspace_record("semantic_models", str(metric.get("model_id") or ""), wid)
        if metric.get("status") != "approved" or not model.get("enabled", True):
            raise ValueError("智能体只能绑定已发布且模型可用的指标")
        if model.get("source_id") not in source_ids:
            raise ValueError("智能体绑定指标的数据源必须在已选范围内")

    mcp_server_ids = list(dict.fromkeys(str(value) for value in merged.get("mcp_server_ids") or []))
    for server_id in mcp_server_ids:
        require_workspace_record("mcp_servers", server_id, wid)

    from ..services.knowledge import strip_reasoning

    instruction = strip_reasoning(merged.get("instruction"))
    if len(instruction) > 16_000:
        raise ValueError("智能体说明超过 16000 字")
    visibility = str(merged.get("visibility") or "workspace").strip()
    if visibility not in {"workspace", "private"}:
        raise ValueError("可见范围只能是 workspace 或 private")
    suggested = merged.get("suggested_questions") or []
    if not isinstance(suggested, list) or len(suggested) > 8:
        raise ValueError("推荐问题最多 8 条")
    return {
        "name": name, "description": str(merged.get("description") or "").strip()[:1000],
        "instruction": instruction, "source_ids": source_ids,
        "knowledge_document_ids": knowledge_ids, "provider_id": provider_id,
        "skill_id": skill_ids[0] if skill_ids else None, "skill_ids": skill_ids,
        "metric_ids": metric_ids, "mcp_server_ids": mcp_server_ids,
        "icon": str(merged.get("icon") or "sparkle")[:40],
        "tags": [str(value)[:24] for value in (merged.get("tags") or [])][:8],
        "welcome": str(merged.get("welcome") or "")[:500],
        "suggested_questions": [str(value)[:200] for value in suggested],
        "visibility": visibility,
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
        if item.get("visibility") == "private" and item.get("created_by") != current_user_id():
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
    _check_private_owner(item)
    if body().get("visibility") == "private" and item.get("created_by") != current_user_id():
        raise PermissionError("只能将自己创建的智能体设为私有")
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
    _check_private_owner(item)
    require_workspace_access(
        item["workspace_id"], write=True if item.get("visibility") == "private" else False,
        owner=item.get("visibility") != "private",
    )
    definition = _validated({}, item)
    if not definition["source_ids"]:
        raise ValueError("数据分析智能体至少需要一个数据源")
    for server_id in definition["mcp_server_ids"]:
        server = require_workspace_record("mcp_servers", server_id, item["workspace_id"])
        if not server.get("enabled", True) or server.get("status") != "connected":
            raise ValueError("智能体绑定的 MCP 服务未连接，请先恢复连接")
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
    _check_private_owner(item)
    require_workspace_access(
        item["workspace_id"], write=True if item.get("visibility") == "private" else False,
        owner=item.get("visibility") != "private",
    )
    version = int(body().get("version") or 0)
    old = require_workspace_record("agent_versions", f"{agent_id}:{version}", item["workspace_id"])
    definition = _validated(old["snapshot"], item)
    updated = db().patch("agent_definitions", agent_id, {
        **definition, "status": "draft", "version": int(item["version"]) + 1,
        "published_at": None,
    }, workspace_id=item["workspace_id"])
    return ok(item=updated)


@bp.delete("/api/agents/<agent_id>")
@api_errors
def delete_agent(agent_id: str):
    item = require_workspace_record("agent_definitions", agent_id)
    _check_private_owner(item)
    if item.get("builtin"):
        raise ValueError("内置智能体不可删除")
    require_workspace_access(
        item["workspace_id"], write=True,
        owner=item.get("status") == "published" and item.get("visibility") != "private",
    )
    db().archive("agent_definitions", agent_id, workspace_id=item["workspace_id"])
    db().audit(
        "agent.deleted", workspace_id=item["workspace_id"], actor=current_user_id(),
        object_type="agent_definition", object_id=agent_id,
    )
    return ok(archived=True)
