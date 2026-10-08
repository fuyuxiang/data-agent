from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, request, stream_with_context
from werkzeug.datastructures import FileStorage

from ..agent.contracts import TaskContract
from ..agent.store import RunStore
from ..core.database import utcnow
from ..services.advanced_agent import _source_authorized, available_formal_tools
from ..services.authorization import require_sources_access
from ..services.authorization import actor_role
from ..services.jobs import get_job_manager
from ..services.intent import suggest_contract
from ..services.knowledge import add_document
from ..services.results.manifests import ResultService
from ..services.product import assert_feature_enabled
from ..services.validation.engine import ValidationEngine
from .common import (
    api_errors, body, current_user_id, db, ok, require_session_access,
    require_source_access, require_workspace_access, require_workspace_record, workspace_id,
    workspace_membership,
)


bp = Blueprint("analyses", __name__)


def _store() -> RunStore:
    return RunStore(db())


def _require_analyzer(wid: str) -> None:
    if actor_role(db(), wid, current_user_id()) not in {"owner", "editor", "analyst"}:
        raise PermissionError("当前成员只有只读权限")


def _require_run(run_id: str, *, write: bool = False) -> dict[str, Any]:
    run = _store().get_run(run_id, workspace_id=workspace_id())
    if not run or run.get("actor_id") != current_user_id():
        # Analysis runs are private even between users in one workspace.
        raise FileNotFoundError("分析任务不存在")
    require_sources_access(
        db(), run.get("source_scope") or [], workspace_id=run["workspace_id"],
        actor_id=current_user_id(), action="analyze" if write else "read",
    )
    if not _source_authorized(db(), run):
        raise PermissionError("数据权限已变更，请重新发起分析")
    if write:
        _require_analyzer(run["workspace_id"])
    if write and run["execution_status"] in {"finished", "cancelled"}:
        raise ValueError("已结束任务不可就地修改，请发起追问、刷新或重新分析")
    return run


def _snapshot(run: dict[str, Any]) -> dict[str, Any]:
    service = ResultService(db())
    publication = service.publication(run["id"], workspace_id=run["workspace_id"])
    manifest = service.manifest(publication["manifest_id"], workspace_id=run["workspace_id"]) if publication else None
    analysis_context = db().get("analysis_context", run["id"], workspace_id=run["workspace_id"]) or {}
    return {
        **run, "contract": _store().latest_contract(run["id"]),
        "plan": _store().latest_plan(run["id"]), "publication": publication, "manifest": manifest,
        "agent_id": (analysis_context.get("agent_snapshot") or {}).get("id"),
        "agent_version": (analysis_context.get("agent_snapshot") or {}).get("version"),
    }


def _session(payload: dict[str, Any], wid: str) -> dict[str, Any]:
    session_id = str(payload.get("session_id") or "")
    if session_id:
        return require_session_access(session_id, wid)
    session = db().put("sessions", {
        "id": db().new_id("ses"), "workspace_id": wid,
        "name": str(payload.get("title") or payload.get("objective") or payload.get("message") or "新分析")[:100],
        "status": "active", "source_ids": [], "provider_id": payload.get("provider_id"),
        "owner_id": current_user_id(), "analysis_mode": "intelligent",
    }, workspace_id=wid)
    return session


def _resolved_skill_ids(
    wid: str, agent: dict[str, Any] | None, payload: dict[str, Any], source_ids: list[str],
) -> list[str]:
    """Work out which skills a run should use.

    An Agent's published skill binding bounds invocation. For a general run,
    an explicit mention wins over caller hints and automatic resolution.
    """
    from ..skills.models import FORMAL_AGENT_TOOLS
    from ..skills.registry import SkillRegistry
    from ..skills.resolver import SkillResolver, extract_explicit, match_known
    from ..skills.permissions import available_resources, filter_visible, unavailable_reason

    registry = SkillRegistry(db(), wid)
    available = available_resources(db(), wid, current_user_id(), source_ids=source_ids)
    if payload.get("skill_ids") is not None and not isinstance(payload["skill_ids"], list):
        raise ValueError("skill_ids 必须是数组")
    known = registry.definitions()
    mentions = [
        match for match in (
            match_known(token, known)
            for token in extract_explicit(
                str(payload.get("objective") or payload.get("message") or "")
            )
        ) if match
    ]
    bound = list(agent.get("skill_ids") or ([agent["skill_id"]] if agent.get("skill_id") else [])) if agent else []
    if bound:
        if set(mentions) - set(bound):
            raise PermissionError("显式指定的技能不在智能体已发布的能力范围内")
        explicit = mentions or bound
    else:
        explicit = mentions or payload.get("skill_ids") or []
        if not explicit and payload.get("skill_id"):
            explicit = [str(payload["skill_id"])]
    if not explicit:
        visible = filter_visible(registry.published(), available)
        question = str(payload.get("objective") or payload.get("message") or "")
        explicit = [item.id for item in SkillResolver(visible).resolve(question).selected]

    resolved: list[str] = []
    for skill_id in dict.fromkeys(str(value) for value in explicit if value):
        definition = registry.get(skill_id)
        if definition is None:
            raise ValueError(f"技能不存在：{skill_id}")
        if definition.status != "published":
            raise ValueError("只能使用已发布的 Skill")
        if reason := unavailable_reason(definition, available):
            raise PermissionError(reason)
        unsupported = sorted(set(definition.allowed_tools) - FORMAL_AGENT_TOOLS)
        if unsupported:
            raise ValueError(f"Skill 使用了正式分析不支持的工具：{', '.join(unsupported)}")
        resolved.append(definition.id)
    return resolved


def _draft_contract(payload: dict[str, Any], source_ids: list[str]) -> TaskContract:
    raw = payload.get("contract") if isinstance(payload.get("contract"), dict) else payload
    question = str(raw.get("objective") or raw.get("message") or raw.get("question") or "").strip()
    return TaskContract.from_payload({
        **raw,
        "objective": question,
        "coverage": raw.get("coverage") or "所选数据源中的全部授权记录；如需限定时间或对象，请在确认前填写",
        "dimensions": raw.get("dimensions") or ["整体"],
        "deliverables": raw.get("deliverables") or ["summary", "dashboard", "report"],
        "source_scope": source_ids,
    })


def _analysis_scope(payload: dict[str, Any], session: dict[str, Any], wid: str) -> tuple[list[str], str | None]:
    if payload.get("business_space_id"):
        raise ValueError("业务数据空间已停用，请直接选择数据源")
    selected = payload["source_ids"] if "source_ids" in payload else session.get("source_ids") or []
    if not isinstance(selected, list):
        raise ValueError("source_ids 必须是数组")
    source_ids = [str(value) for value in selected]
    source_ids = list(dict.fromkeys(source_ids))
    if len(source_ids) > 100:
        raise ValueError("单次分析最多选择 100 个来源")
    for source_id in source_ids:
        require_source_access(source_id, wid, action="analyze")
    return source_ids, None


def _auto_confirm_requested(payload: dict[str, Any], objective: str) -> bool:
    if payload.get("confirm_required") is True:
        return False
    mode = str(payload.get("execution_mode") or "auto")
    if mode == "deep":
        return False
    if mode == "quick":
        return True
    if not payload.get("auto_confirm"):
        return False
    complex_terms = ("为什么", "归因", "预测", "复盘", "完整报告", "方案", "深度", "建模")
    return not any(term in objective for term in complex_terms)


def _analysis_budget(payload: dict[str, Any]) -> dict[str, Any]:
    """Give interactive analyses enough room for multi-step evidence work.

    Model-token usage is cumulative input plus output across every reasoning
    round. A 100k run cap is therefore too small for a normal analysis that
    needs schema discovery, several queries, validation and charts. Explicit
    budgets remain supported for governed API callers.
    """
    configured = payload.get("budget")
    if isinstance(configured, dict):
        return configured
    mode = str(payload.get("execution_mode") or "auto")
    budget = RunStore.default_budget()
    budget["model_tokens"] = {
        "quick": 100_000,
        "auto": 160_000,
        "deep": 240_000,
    }.get(mode, 160_000)
    return budget


def _confirm_and_enqueue(run: dict[str, Any], contract: TaskContract, expected_version: int) -> tuple[dict, dict]:
    confirmed = _store().add_contract(
        run["id"], contract, expected_version=expected_version, confirmed_by=current_user_id(),
    )
    current = _store().get_run(run["id"]) or run
    _store().add_plan(run["id"], {
        "tasks": [{
            "id": "evidence_driven_analysis", "title": "根据证据动态选择查询、验证与分析动作",
            "status": "open", "depends_on": [],
        }],
    }, reason="contract_confirmed", expected_version=int(current["plan_version"]))
    job = get_job_manager(current_app._get_current_object()).submit_spec(
        workspace_id=run["workspace_id"], session_id=run["session_id"],
        job_type="analysis_run", title=contract.objective[:100], spec={"run_id": run["id"]}, run_id=run["id"],
    )
    db().audit(
        "contract.confirmed", workspace_id=run["workspace_id"], actor=current_user_id(),
        object_type="agent_run", object_id=run["id"],
        detail={"contract_version": confirmed["version"], "job_id": job["id"]},
    )
    return confirmed, job


@bp.post("/api/analyses")
@api_errors
def create_analysis():
    payload, wid = body(), workspace_id()
    require_workspace_access(wid)
    _require_analyzer(wid)
    assert_feature_enabled(db(), wid, "governed_agent")
    # Validate the request before creating a new session, so denied Agent or
    # source selections do not leave empty conversations in the sidebar.
    session = _session(payload, wid) if payload.get("session_id") else {
        "source_ids": [], "provider_id": payload.get("provider_id"),
    }
    source_ids, business_space_id = _analysis_scope(payload, session, wid)
    agent_id = str(payload.get("agent_id") or "")
    agent = require_workspace_record("agent_definitions", agent_id, wid) if agent_id else None
    preview = payload.get("agent_preview")
    if preview is not None:
        if agent_id or not isinstance(preview, dict):
            raise ValueError("智能体测试配置无效")
        if actor_role(db(), wid, current_user_id()) not in {"owner", "editor"}:
            raise PermissionError("只有管理员可以测试未发布的智能体")
        from .agents import _validated

        agent = {
            **_validated(preview), "id": "preview", "version": 0,
            "status": "preview", "created_by": current_user_id(),
        }
    if agent:
        if agent.get("status") not in {"published", "preview"}:
            raise PermissionError("只能使用已发布的智能体")
        if agent.get("visibility") == "private" and agent.get("created_by") != current_user_id():
            raise FileNotFoundError("智能体不存在")
        agent_source_list = list(dict.fromkeys(str(value) for value in agent.get("source_ids") or []))
        agent_source_ids = set(agent_source_list)
        if not source_ids and "source_ids" not in payload:
            source_ids = agent_source_list
            for source_id in source_ids:
                require_source_access(source_id, wid, action="analyze")
        if not set(source_ids).issubset(agent_source_ids):
            raise PermissionError("所选数据源超出智能体已发布范围")
    selected_knowledge = payload.get("knowledge_document_ids")
    if agent:
        permitted_knowledge = {str(value) for value in agent.get("knowledge_document_ids") or []}
        if selected_knowledge is None:
            selected_knowledge = list(permitted_knowledge)
        elif not set(str(value) for value in selected_knowledge).issubset(permitted_knowledge):
            raise PermissionError("所选知识超出智能体已发布范围")
    if selected_knowledge is not None and not isinstance(selected_knowledge, list):
        raise ValueError("knowledge_document_ids 必须是数组")
    knowledge_ids = list(dict.fromkeys(str(value) for value in selected_knowledge or []))
    if len(knowledge_ids) > 100:
        raise ValueError("单次分析最多选择 100 份知识文档")
    for document_id in knowledge_ids:
        document = db().get("knowledge_documents", document_id, workspace_id=wid)
        if not document or not document.get("enabled", True) or document.get("visibility") == "analysis_attachment":
            raise ValueError("所选知识文档不存在或已停用")
    provider_id = str((agent or {}).get("provider_id") or payload.get("provider_id") or "") or None
    if provider_id and provider_id != "environment-default":
        require_workspace_record("providers", provider_id, wid)
    skill_ids = _resolved_skill_ids(wid, agent, payload, source_ids)
    skill_id = skill_ids[0] if skill_ids else None
    contract = _draft_contract(payload, source_ids)
    if not payload.get("session_id"):
        session = _session(payload, wid)
    allowed_tools = available_formal_tools(db(), wid, session["id"], source_ids)
    idempotency_key = str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or "") or None
    run, created = _store().create_run(
        workspace_id=wid, session_id=session["id"], actor_id=current_user_id(),
        source_scope=source_ids, allowed_tool_ids=allowed_tools,
        provider_id=provider_id or session.get("provider_id"), parent_run_id=payload.get("parent_run_id"),
        skill_id=skill_id, run_kind=str(payload.get("run_kind") or "analysis"),
        budget=_analysis_budget(payload),
        idempotency_key=idempotency_key,
    )
    if created:
        # 技能解析先记一笔：即使运行因为没有模型而提前结束，运行详情也说得清
        # 用户当时请求了什么，而不是一片空白。
        db().put("skill_resolutions", {
            "id": f"skr_{run['id']}", "workspace_id": wid, "run_id": run["id"],
            "session_id": session["id"], "actor_id": current_user_id(),
            "skill_ids": [], "requested_skill_ids": skill_ids,
            "allowed_tools": [], "warnings": [],
        }, workspace_id=wid)
        db().put("analysis_context", {
            "id": run["id"], "workspace_id": wid,
            "knowledge_document_ids": knowledge_ids,
            "knowledge_selection_explicit": selected_knowledge is not None,
            "skill_ids": skill_ids,
            "requested_skill_ids": skill_ids,
            "agent_snapshot": {
                "id": agent["id"], "version": agent["version"],
                "name": agent["name"], "instruction": agent.get("instruction") or "",
                "metric_ids": list(agent.get("metric_ids") or []),
                "mcp_server_ids": list(agent.get("mcp_server_ids") or []),
            } if agent else None,
        }, workspace_id=wid)
        db().patch("sessions", session["id"], {
            "source_ids": source_ids, "provider_id": provider_id or session.get("provider_id"),
            "business_space_id": business_space_id, "owner_id": current_user_id(), "current_run_id": run["id"],
        }, workspace_id=wid)
        db().add_message(session["id"], "user", contract.objective, {"run_id": run["id"]})
        _store().add_contract(run["id"], contract, expected_version=0)
        run = _store().get_run(run["id"]) or run
        db().audit(
            "analysis.created", workspace_id=wid, actor=current_user_id(),
            object_type="agent_run", object_id=run["id"], detail={"source_ids": source_ids},
        )
        if _auto_confirm_requested(payload, contract.objective):
            _confirmed, job = _confirm_and_enqueue(run, contract, expected_version=1)
            run = _store().get_run(run["id"]) or run
            return ok(item=_snapshot(run), created=True, auto_confirmed=True, job=job), 201
    return ok(item=_snapshot(run), created=created, auto_confirmed=False), 201 if created else 200


@bp.post("/api/analyses/<run_id>/contract/suggest")
@api_errors
def suggest_analysis_contract(run_id: str):
    run = _require_run(run_id, write=True)
    latest = _store().latest_contract(run_id)
    if not latest or latest.get("confirmed_at"):
        raise ValueError("只能为待确认任务生成需求草稿")
    source_names = [
        str(source.get("name") or source["id"])
        for source_id in run.get("source_scope") or []
        if (source := db().get("sources", source_id, workspace_id=run["workspace_id"]))
    ]
    attachment_names = [
        str(item.get("filename") or "")
        for item in db().list("analysis_attachments", workspace_id=run["workspace_id"], limit=5000)
        if item.get("run_id") == run_id and item.get("owner_id") == current_user_id()
    ]
    try:
        suggested = suggest_contract(
            question=str(latest["payload"]["objective"]), source_names=source_names,
            attachment_names=attachment_names, provider_id=run.get("provider_id"),
            workspace_id=run["workspace_id"],
        )
    except Exception:
        # An unavailable drafting model must not prevent the user from editing
        # and confirming the deterministic contract already on screen.
        suggested = None
    if not suggested:
        return ok(item=_snapshot(run), suggested=False)
    merged = {**latest["payload"], **suggested, "source_scope": run["source_scope"]}
    merged["dimensions"] = suggested.get("dimensions") or latest["payload"]["dimensions"]
    merged["deliverables"] = suggested.get("deliverables") or latest["payload"]["deliverables"]
    contract = TaskContract.from_payload(merged)
    _store().add_contract(run_id, contract, expected_version=int(latest["version"]))
    _store().append_event(run_id, "contract.suggested", {"source": "model", "editable": True})
    return ok(item=_snapshot(_store().get_run(run_id) or run), suggested=True)


@bp.get("/api/analyses")
@api_errors
def list_analyses():
    items = [
        _snapshot(item) for item in _store().list_runs(
            workspace_id(), session_id=str(request.args.get("session_id") or "") or None,
            limit=int(request.args.get("limit", 100)),
        ) if item.get("actor_id") == current_user_id() and _source_authorized(db(), item)
    ]
    return ok(items=items)


@bp.get("/api/analyses/<run_id>")
@api_errors
def get_analysis(run_id: str):
    return ok(item=_snapshot(_require_run(run_id)))


@bp.delete("/api/analyses/<run_id>")
@api_errors
def archive_analysis(run_id: str):
    run = _require_run(run_id)
    if not _store().archive_run(
        run_id, workspace_id=run["workspace_id"], session_id=run["session_id"],
    ):
        raise FileNotFoundError("分析任务不存在")
    return ok(archived=True)


@bp.get("/api/analyses/<run_id>/execution")
@api_errors
def get_analysis_execution(run_id: str):
    """A bounded, owner-only explanation of an analysis run."""
    run = _require_run(run_id)
    store = _store()
    resolution = db().get("skill_resolutions", f"skr_{run_id}", workspace_id=run["workspace_id"]) or {}
    return ok(item={
        "skills": list(resolution.get("skill_ids") or resolution.get("requested_skill_ids") or []),
        "actions": [{
            "tool_id": item.get("tool_id"), "status": item.get("status"),
            "error_code": item.get("error_code"), "arguments": item.get("arguments") or {},
        } for item in store.actions(run_id)[-200:]],
        "decisions": [{
            "sequence": item.get("sequence"), "model": item.get("model_name"),
            "tool_call_count": len(item.get("tool_calls") or []),
        } for item in store.decisions(run_id)[-200:]],
    })


@bp.post("/api/analyses/<run_id>/attachments")
@api_errors
def add_analysis_attachments(run_id: str):
    run = _require_run(run_id, write=True)
    latest = _store().latest_contract(run_id)
    if latest and latest.get("confirmed_at"):
        raise ValueError("已确认后不能悄悄改变证据范围，请创建追问子任务")
    files = request.files.getlist("files") or ([request.files["file"]] if "file" in request.files else [])
    if not files:
        raise ValueError("没有收到附件")
    if len(files) > 20:
        raise ValueError("单次最多上传 20 个分析附件")
    allowed = {".docx", ".xlsx", ".pdf", ".md", ".txt"}
    tags = [value.strip()[:80] for value in request.form.get("tags", "").split(",") if value.strip()]
    prepared = []
    for file in files:
        suffix = Path(str(file.filename or "")).suffix.lower()
        if suffix not in allowed:
            raise ValueError("分析入口仅支持 docx、xlsx、pdf、md、txt")
        stream = file.stream
        position = stream.tell()
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(position)
        if size > 50 * 1024 * 1024:
            raise ValueError(f"附件 {file.filename} 超过 50MB")
        prepared.append((file, size))
    items = []
    created_documents = []
    try:
        for file, size in prepared:
            document = add_document(file, run["workspace_id"], tags)
            created_documents.append(document["id"])
            db().patch("knowledge_documents", document["id"], {
                "visibility": "analysis_attachment", "owner_id": current_user_id(), "run_id": run_id,
            }, workspace_id=run["workspace_id"])
            attachment = db().put("analysis_attachments", {
                "id": db().new_id("attachment"), "workspace_id": run["workspace_id"],
                "run_id": run_id, "owner_id": current_user_id(), "document_id": document["id"],
                "filename": document["filename"], "format": document["format"], "size": size,
                "tags": tags, "evidence_locations": document.get("evidence_locations", False),
                "visual_only_pages": document.get("visual_only_pages") or [],
            }, workspace_id=run["workspace_id"])
            items.append(attachment)
    except Exception:
        for item in items:
            db().archive("analysis_attachments", item["id"], workspace_id=run["workspace_id"])
        for document_id in created_documents:
            record = db().get("knowledge_documents", document_id, workspace_id=run["workspace_id"])
            if record:
                Path(record["path"]).unlink(missing_ok=True)
                db().archive("knowledge_documents", document_id, workspace_id=run["workspace_id"])
        raise
    _store().append_event(run_id, "attachments.added", {
        "items": [{"id": item["id"], "filename": item["filename"], "tags": item["tags"]} for item in items],
    })
    return ok(items=items), 201


@bp.post("/api/analyses/<run_id>/attachments/library")
@api_errors
def add_library_attachment(run_id: str):
    """Reuse an owned library document as evidence before contract confirmation."""
    run = _require_run(run_id, write=True)
    latest = _store().latest_contract(run_id)
    if latest and latest.get("confirmed_at"):
        raise ValueError("已确认任务的证据范围已锁定")
    from .library import _resolve
    from .common import safe_child

    record_id = str(body().get("record_id") or "")
    record, _collection = _resolve(record_id)
    suffix = Path(str(record.get("filename") or "")).suffix.lower()
    if suffix not in {".docx", ".xlsx", ".pdf", ".md", ".txt"}:
        raise ValueError("该资料格式不能直接分析，请选择 Word、Excel、PDF 或文本文件")
    path = safe_child(current_app.config["SETTINGS"].export_dir, Path(str(record.get("path") or "")))
    if not path.is_file() or path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("资料不存在或超过 50MB")
    with path.open("rb") as stream:
        document = add_document(FileStorage(stream=stream, filename=record["filename"]), run["workspace_id"])
    try:
        db().patch("knowledge_documents", document["id"], {
            "visibility": "analysis_attachment", "owner_id": current_user_id(), "run_id": run_id,
        }, workspace_id=run["workspace_id"])
        item = db().put("analysis_attachments", {
            "id": db().new_id("attachment"), "workspace_id": run["workspace_id"],
            "run_id": run_id, "owner_id": current_user_id(), "document_id": document["id"],
            "filename": document["filename"], "format": document["format"],
            "size": path.stat().st_size, "tags": [],
            "evidence_locations": document.get("evidence_locations", False),
            "visual_only_pages": document.get("visual_only_pages") or [],
        }, workspace_id=run["workspace_id"])
    except Exception:
        stored = db().get("knowledge_documents", document["id"], workspace_id=run["workspace_id"])
        if stored:
            Path(stored["path"]).unlink(missing_ok=True)
            db().archive("knowledge_documents", document["id"], workspace_id=run["workspace_id"])
        raise
    _store().append_event(run_id, "attachments.added", {
        "items": [{"id": item["id"], "filename": item["filename"], "tags": []}],
    })
    return ok(item=item), 201


@bp.get("/api/analyses/<run_id>/attachments")
@api_errors
def list_analysis_attachments(run_id: str):
    run = _require_run(run_id)
    return ok(items=[
        item for item in db().list("analysis_attachments", workspace_id=run["workspace_id"], limit=5000)
        if item.get("run_id") == run_id and item.get("owner_id") == current_user_id()
    ])


@bp.delete("/api/analyses/<run_id>/attachments/<attachment_id>")
@api_errors
def remove_analysis_attachment(run_id: str, attachment_id: str):
    run = _require_run(run_id, write=True)
    latest = _store().latest_contract(run_id)
    if latest and latest.get("confirmed_at"):
        raise ValueError("已确认任务的证据范围已锁定")
    item = require_workspace_record("analysis_attachments", attachment_id, run["workspace_id"])
    if item.get("run_id") != run_id or item.get("owner_id") != current_user_id():
        raise FileNotFoundError("附件不存在")
    db().archive("analysis_attachments", attachment_id, workspace_id=run["workspace_id"])
    db().archive("knowledge_documents", item["document_id"], workspace_id=run["workspace_id"])
    _store().append_event(run_id, "attachment.removed", {"attachment_id": attachment_id})
    return ok(archived=True)


@bp.put("/api/analyses/<run_id>/contract")
@api_errors
def revise_contract(run_id: str):
    run = _require_run(run_id, write=True)
    payload = body()
    latest = _store().latest_contract(run_id)
    if latest and latest.get("confirmed_at"):
        raise ValueError("已确认契约已锁定；改变目标或口径请创建子任务")
    expected = int(payload.get("expected_version", -1))
    contract = TaskContract.from_payload(payload.get("contract") or payload)
    if set(contract.source_scope) - set(run["source_scope"]):
        raise PermissionError("契约不得扩大创建任务时选定的来源范围")
    item = _store().add_contract(run_id, contract, expected_version=expected)
    return ok(contract=item, item=_snapshot(_store().get_run(run_id) or run))


@bp.post("/api/analyses/<run_id>/contract/confirm")
@api_errors
def confirm_contract(run_id: str):
    run = _require_run(run_id, write=True)
    payload = body()
    latest = _store().latest_contract(run_id)
    if not latest:
        raise ValueError("任务契约不存在")
    if latest.get("confirmed_at"):
        return ok(item=_snapshot(run), already_confirmed=True)
    expected = int(payload.get("expected_version", -1))
    if expected != int(latest["version"]):
        raise ValueError(f"任务契约版本冲突：当前为 {latest['version']}")
    contract = TaskContract.from_payload(payload.get("contract") or latest["payload"])
    if set(contract.source_scope) - set(run["source_scope"]):
        raise PermissionError("确认时不得扩大数据来源范围")
    _confirmed, job = _confirm_and_enqueue(run, contract, expected)
    return ok(item=_snapshot(_store().get_run(run_id) or run), job=job)


@bp.get("/api/analyses/<run_id>/events")
@api_errors
def analysis_events(run_id: str):
    _require_run(run_id)
    after = int(request.args.get("after") or request.headers.get("Last-Event-ID") or 0)
    if "text/event-stream" not in str(request.headers.get("Accept") or ""):
        events = _store().events(run_id, after=after, limit=int(request.args.get("limit", 500)))
        return ok(items=events, next_cursor=events[-1]["sequence"] if events else after)

    @stream_with_context
    def generate():
        cursor = after
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            current = _require_run(run_id)
            events = _store().events(run_id, after=cursor, limit=200)
            for event in events:
                cursor = int(event["sequence"])
                yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if current["execution_status"] in {"finished", "failed", "cancelled"} and not events:
                return
            if not events:
                yield ": heartbeat\n\n"
            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
    })


@bp.post("/api/feedback")
@api_errors
def create_analysis_feedback():
    payload = body()
    run = _require_run(str(payload.get("run_id") or ""))
    if not ResultService(db()).publication(run["id"], workspace_id=run["workspace_id"]):
        raise ValueError("只有已发布的分析结果可以提交质量反馈")
    rating = str(payload.get("rating") or "")
    if rating not in {"correct", "partially_correct", "incorrect"}:
        raise ValueError("反馈类型无效")
    comment = str(payload.get("category") or payload.get("comment") or "").strip()[:2000]
    if rating in {"partially_correct", "incorrect"} and not comment:
        raise ValueError("请说明需要纠正的问题")
    previous = next((
        item for item in db().list("analysis_feedback", workspace_id=run["workspace_id"], limit=5000)
        if item.get("run_id") == run["id"] and item.get("actor_id") == current_user_id()
    ), None)
    if previous:
        item = db().patch("analysis_feedback", previous["id"], {
            "rating": rating, "comment": comment, "status": "open", "updated_at": utcnow(),
        }, workspace_id=run["workspace_id"])
    else:
        item = db().put("analysis_feedback", {
            "id": db().new_id("feedback"), "workspace_id": run["workspace_id"],
            "run_id": run["id"], "actor_id": current_user_id(), "rating": rating,
            "comment": comment, "status": "open", "created_at": utcnow(),
        }, workspace_id=run["workspace_id"])
    db().audit(
        "analysis.feedback", workspace_id=run["workspace_id"], actor=current_user_id(),
        object_type="agent_run", object_id=run["id"], detail={"rating": rating},
    )
    return ok(item=item), 200 if previous else 201


@bp.get("/api/analyses/<run_id>/feedback")
@api_errors
def get_analysis_feedback(run_id: str):
    run = _require_run(run_id)
    item = next((
        record for record in db().list("analysis_feedback", workspace_id=run["workspace_id"], limit=5000)
        if record.get("run_id") == run_id and record.get("actor_id") == current_user_id()
    ), None)
    return ok(item=item)


@bp.get("/api/feedback")
@api_errors
def list_analysis_feedback():
    wid = workspace_id()
    membership = workspace_membership(wid)
    if not membership or membership.get("role") not in {"owner", "editor"}:
        raise PermissionError("只有管理员可以查看分析反馈")
    return ok(items=db().list("analysis_feedback", workspace_id=wid, limit=500))


@bp.patch("/api/feedback/<feedback_id>")
@api_errors
def review_analysis_feedback(feedback_id: str):
    wid = workspace_id()
    membership = workspace_membership(wid)
    if not membership or membership.get("role") not in {"owner", "editor"}:
        raise PermissionError("只有管理员可以处理分析反馈")
    require_workspace_record("analysis_feedback", feedback_id, wid)
    status = str(body().get("status") or "")
    if status not in {"reviewing", "resolved", "dismissed"}:
        raise ValueError("反馈处理状态无效")
    review_note = str(body().get("review_note") or "").strip()[:2000]
    if status in {"resolved", "dismissed"} and not review_note:
        raise ValueError("请记录处理结论或未采纳原因")
    item = db().patch("analysis_feedback", feedback_id, {
        "status": status, "reviewed_by": current_user_id(),
        "review_note": review_note, "reviewed_at": utcnow(),
    }, workspace_id=wid)
    db().audit(
        "analysis.feedback.reviewed", workspace_id=wid, actor=current_user_id(),
        object_type="analysis_feedback", object_id=feedback_id, detail={"status": status},
    )
    return ok(item=item)


def _active_job(run_id: str) -> dict[str, Any] | None:
    return next((
        item for item in db().list("jobs", workspace_id=workspace_id(), limit=5000)
        if item.get("run_id") == run_id and item.get("status") in {"queued", "running"}
    ), None)


@bp.post("/api/analyses/<run_id>/control")
@api_errors
def control_analysis(run_id: str):
    run = _require_run(run_id)
    _require_analyzer(run["workspace_id"])
    payload = body()
    action = str(payload.get("action") or "")
    expected = payload.get("expected_version")
    if expected is not None and int(expected) != int(run["version"]):
        raise ValueError(f"任务版本冲突：当前为 {run['version']}")
    manager = get_job_manager(current_app._get_current_object())
    job = _active_job(run_id)
    if action == "pause":
        if run["execution_status"] in {"paused", "finished", "failed", "cancelled"}:
            return ok(item=_snapshot(run), idempotent=True)
        updated = _store().update_status(run_id, "paused", stop_reason="user_paused")
    elif action == "cancel":
        if run["execution_status"] == "cancelled":
            return ok(item=_snapshot(run), idempotent=True)
        if not job:
            updated = _store().update_status(
                run_id, "cancelled", outcome="cancelled", stop_reason="user_cancelled",
            )
        else:
            updated = _store().update_status(run_id, "cancelling", stop_reason="cancel_requested")
            if not manager.cancel(job["id"]):
                updated = _store().update_status(
                    run_id, "cancelled", outcome="cancelled", stop_reason="user_cancelled",
                )
    elif action == "resume":
        if run["execution_status"] not in {"paused", "waiting_input"}:
            raise ValueError("只有已暂停或等待澄清的任务可继续；远程作业由调度器自动恢复")
        updated = _store().update_status(run_id, "queued", stop_reason="user_resumed")
        job = manager.submit_spec(
            workspace_id=run["workspace_id"], session_id=run["session_id"],
            job_type="analysis_run", title="继续分析", spec={"run_id": run_id}, run_id=run_id,
        )
    else:
        raise ValueError("action 必须是 pause、resume 或 cancel")
    return ok(item=_snapshot(updated), job=job)


@bp.post("/api/analyses/<run_id>/clarifications")
@api_errors
def answer_clarification(run_id: str):
    run = _require_run(run_id)
    _require_analyzer(run["workspace_id"])
    if run["execution_status"] != "waiting_input" or run.get("stop_reason") != "clarification_required":
        raise ValueError("当前任务没有等待澄清")
    answer = str(body().get("answer") or "").strip()
    if not answer:
        raise ValueError("澄清回答不能为空")
    db().add_message(run["session_id"], "user", answer, {"run_id": run_id, "kind": "clarification"})
    _store().append_event(run_id, "clarification.answered", {"answer": answer})
    _store().update_status(run_id, "queued", stop_reason="clarification_answered")
    job = get_job_manager(current_app._get_current_object()).submit_spec(
        workspace_id=run["workspace_id"], session_id=run["session_id"], job_type="analysis_run",
        title="继续分析", spec={"run_id": run_id}, run_id=run_id,
    )
    return ok(item=_snapshot(_store().get_run(run_id) or run), job=job)


@bp.get("/api/analyses/<run_id>/evidence")
@api_errors
def evidence(run_id: str):
    run = _require_run(run_id)
    return ok(actions=_store().actions(run_id), decisions=_store().decisions(run_id), claims=ResultService(db()).claims(
        run_id, workspace_id=run["workspace_id"],
    ))


@bp.get("/api/analyses/<run_id>/evidence/claims/<claim_id>/cells/<int:cell_index>")
@api_errors
def replay_evidence_cell(run_id: str, claim_id: str, cell_index: int):
    run = _require_run(run_id)
    return ok(item=ResultService(db()).replay_cell(
        run_id, claim_id, cell_index, workspace_id=run["workspace_id"],
    ))


@bp.get("/api/analyses/<run_id>/validations")
@api_errors
def validations(run_id: str):
    run = _require_run(run_id)
    return ok(items=ValidationEngine(db(), []).list_for_run(run_id, workspace_id=run["workspace_id"]))


@bp.post("/api/analyses/<run_id>/replay")
@api_errors
def replay(run_id: str):
    run = _require_run(run_id)
    return ok(item=_snapshot(run), events=_store().events(run_id, after=0, limit=2000), mode="replay", scanned=False)


def _branch(run: dict[str, Any], mode: str, prompt: str) -> dict[str, Any]:
    latest = _store().latest_contract(run["id"])
    if not latest:
        raise ValueError("父任务契约不存在")
    if mode == "reproduce":
        refs = ResultService(db()).publication(run["id"], workspace_id=run["workspace_id"])
        if not refs:
            raise ValueError("没有已发布的历史快照，不能用最新数据冒充精确复现")
    raw = dict(latest["payload"])
    raw["objective"] = prompt or raw["objective"]
    payload = {
        "session_id": run["session_id"], "source_ids": run["source_scope"],
        "provider_id": run.get("provider_id"), "parent_run_id": run["id"], "run_kind": mode,
        "contract": raw,
    }
    return payload


@bp.post("/api/analyses/<run_id>/branch")
@api_errors
def branch_analysis(run_id: str):
    run = _require_run(run_id)
    _require_analyzer(run["workspace_id"])
    assert_feature_enabled(db(), run["workspace_id"], "governed_agent")
    payload = body()
    mode = str(payload.get("mode") or "followup")
    if mode not in {"followup", "refresh", "reproduce", "reanalyze"}:
        raise ValueError("mode 无效")
    branch_payload = _branch(run, mode, str(payload.get("prompt") or "").strip())
    # Reuse the same validated endpoint implementation without issuing an internal HTTP request.
    source_ids = branch_payload["source_ids"]
    for source_id in source_ids:
        require_source_access(str(source_id), run["workspace_id"], action="analyze")
    contract = TaskContract.from_payload(branch_payload["contract"] | {"source_scope": source_ids})
    child, _ = _store().create_run(
        workspace_id=run["workspace_id"], session_id=run["session_id"], actor_id=current_user_id(),
        source_scope=source_ids,
        allowed_tool_ids=sorted(set(run.get("allowed_tool_ids") or []) & set(available_formal_tools(
            db(), run["workspace_id"], run["session_id"], source_ids,
        ))),
        provider_id=run.get("provider_id"), parent_run_id=run["id"], run_kind=mode,
        skill_id=run.get("skill_id"),
    )
    parent_context = db().get("analysis_context", run["id"], workspace_id=run["workspace_id"]) or {}
    db().put("analysis_context", {
        "id": child["id"], "workspace_id": run["workspace_id"],
        "knowledge_document_ids": parent_context.get("knowledge_document_ids") or [],
        "knowledge_selection_explicit": bool(parent_context.get("knowledge_selection_explicit")),
        "skill_ids": parent_context.get("skill_ids") or ([run["skill_id"]] if run.get("skill_id") else []),
        "requested_skill_ids": parent_context.get("requested_skill_ids") or [],
        "agent_snapshot": parent_context.get("agent_snapshot"),
    }, workspace_id=run["workspace_id"])
    _store().add_contract(child["id"], contract, expected_version=0)
    db().add_message(run["session_id"], "user", contract.objective, {"run_id": child["id"], "parent_run_id": run["id"]})
    _store().append_event(child["id"], "analysis.branched", {"parent_run_id": run["id"], "mode": mode})
    return ok(item=_snapshot(_store().get_run(child["id"]) or child)), 201
