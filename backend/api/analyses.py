from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, request, stream_with_context

from ..agent.contracts import TaskContract
from ..agent.store import RunStore
from ..services.advanced_agent import available_formal_tools
from ..services.jobs import get_job_manager
from ..services.knowledge import add_document
from ..services.results.manifests import ResultService
from ..services.validation.engine import ValidationEngine
from .common import (
    api_errors, body, current_user_id, db, ok, require_workspace_record, workspace_id,
)


bp = Blueprint("analyses", __name__)


def _store() -> RunStore:
    return RunStore(db())


def _require_run(run_id: str, *, write: bool = False) -> dict[str, Any]:
    run = _store().get_run(run_id, workspace_id=workspace_id())
    if not run or run.get("actor_id") != current_user_id():
        # Analysis runs are private even between users in one workspace.
        raise FileNotFoundError("分析任务不存在")
    if write and run["execution_status"] in {"finished", "cancelled"}:
        raise ValueError("已结束任务不可就地修改，请发起追问、刷新或重新分析")
    return run


def _snapshot(run: dict[str, Any]) -> dict[str, Any]:
    service = ResultService(db())
    publication = service.publication(run["id"], workspace_id=run["workspace_id"])
    manifest = service.manifest(publication["manifest_id"], workspace_id=run["workspace_id"]) if publication else None
    return {
        **run, "contract": _store().latest_contract(run["id"]),
        "plan": _store().latest_plan(run["id"]), "publication": publication, "manifest": manifest,
    }


def _session(payload: dict[str, Any], wid: str) -> dict[str, Any]:
    session_id = str(payload.get("session_id") or "")
    if session_id:
        session = require_workspace_record("sessions", session_id, wid)
        owner_id = str(session.get("owner_id") or current_user_id())
        if owner_id != current_user_id():
            raise FileNotFoundError("分析会话不存在")
        return session
    session = db().put("sessions", {
        "id": db().new_id("ses"), "workspace_id": wid,
        "name": str(payload.get("title") or payload.get("objective") or payload.get("message") or "新分析")[:100],
        "status": "active", "source_ids": [], "provider_id": payload.get("provider_id"),
        "owner_id": current_user_id(), "analysis_mode": "intelligent",
    }, workspace_id=wid)
    return session


def _draft_contract(payload: dict[str, Any], source_ids: list[str]) -> TaskContract:
    raw = payload.get("contract") if isinstance(payload.get("contract"), dict) else payload
    question = str(raw.get("objective") or raw.get("message") or raw.get("question") or "").strip()
    return TaskContract.from_payload({
        **raw,
        "objective": question,
        "coverage": raw.get("coverage") or "所选来源的已授权数据范围；时间口径待在确认卡中核对",
        "dimensions": raw.get("dimensions") or ["时间", "业务实体", "可用分类属性"],
        "deliverables": raw.get("deliverables") or ["summary", "dashboard", "report"],
        "source_scope": source_ids,
    })


@bp.post("/api/analyses")
@api_errors
def create_analysis():
    payload, wid = body(), workspace_id()
    source_ids = list(dict.fromkeys(str(value) for value in payload.get("source_ids") or []))
    if len(source_ids) > 100:
        raise ValueError("单次分析最多选择 100 个来源")
    for source_id in source_ids:
        source = require_workspace_record("sources", source_id, wid)
        allowed_users = source.get("authorized_user_ids")
        if isinstance(allowed_users, list) and current_user_id() not in allowed_users:
            raise PermissionError("当前用户无权使用选中来源")
    provider_id = str(payload.get("provider_id") or "") or None
    if provider_id and provider_id != "environment-default":
        require_workspace_record("providers", provider_id, wid)
    skill_id = str(payload.get("skill_id") or "") or None
    if skill_id:
        from ..services.skills import get_skill

        skill = get_skill(skill_id, wid)
        if not skill or (skill.get("status") and skill.get("status") != "published"):
            raise ValueError("只能使用当前已发布的 Skill")
    session = _session(payload, wid)
    contract = _draft_contract(payload, source_ids)
    allowed_tools = available_formal_tools(db(), wid, session["id"], source_ids)
    idempotency_key = str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or "") or None
    run, created = _store().create_run(
        workspace_id=wid, session_id=session["id"], actor_id=current_user_id(),
        source_scope=source_ids, allowed_tool_ids=allowed_tools,
        provider_id=provider_id or session.get("provider_id"), parent_run_id=payload.get("parent_run_id"),
        skill_id=skill_id, run_kind=str(payload.get("run_kind") or "analysis"), budget=payload.get("budget"),
        idempotency_key=idempotency_key,
    )
    if created:
        db().patch("sessions", session["id"], {
            "source_ids": source_ids, "provider_id": provider_id or session.get("provider_id"),
            "owner_id": current_user_id(), "current_run_id": run["id"],
        }, workspace_id=wid)
        db().add_message(session["id"], "user", contract.objective, {"run_id": run["id"]})
        _store().add_contract(run["id"], contract, expected_version=0)
        run = _store().get_run(run["id"]) or run
        db().audit(
            "analysis.created", workspace_id=wid, actor=current_user_id(),
            object_type="agent_run", object_id=run["id"], detail={"source_ids": source_ids},
        )
    return ok(item=_snapshot(run), created=created), 201 if created else 200


@bp.get("/api/analyses")
@api_errors
def list_analyses():
    items = [
        _snapshot(item) for item in _store().list_runs(
            workspace_id(), session_id=str(request.args.get("session_id") or "") or None,
            limit=int(request.args.get("limit", 100)),
        ) if item.get("actor_id") == current_user_id()
    ]
    return ok(items=items)


@bp.get("/api/analyses/<run_id>")
@api_errors
def get_analysis(run_id: str):
    return ok(item=_snapshot(_require_run(run_id)))


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
    items = []
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
        document = add_document(file, run["workspace_id"], tags)
        attachment = db().put("analysis_attachments", {
            "id": db().new_id("attachment"), "workspace_id": run["workspace_id"],
            "run_id": run_id, "owner_id": current_user_id(), "document_id": document["id"],
            "filename": document["filename"], "format": document["format"], "size": size,
            "tags": tags, "evidence_locations": document.get("evidence_locations", False),
            "visual_only_pages": document.get("visual_only_pages") or [],
        }, workspace_id=run["workspace_id"])
        items.append(attachment)
    _store().append_event(run_id, "attachments.added", {
        "items": [{"id": item["id"], "filename": item["filename"], "tags": item["tags"]} for item in items],
    })
    return ok(items=items), 201


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
    confirmed = _store().add_contract(
        run_id, contract, expected_version=expected, confirmed_by=current_user_id(),
    )
    current = _store().get_run(run_id) or run
    _store().add_plan(run_id, {
        "tasks": [{
            "id": "evidence_driven_analysis", "title": "根据证据动态选择查询、验证与分析动作",
            "status": "open", "depends_on": [],
        }],
    }, reason="contract_confirmed", expected_version=int(current["plan_version"]))
    job = get_job_manager(current_app._get_current_object()).submit_spec(
        workspace_id=run["workspace_id"], session_id=run["session_id"],
        job_type="analysis_run", title=contract.objective[:100], spec={"run_id": run_id}, run_id=run_id,
    )
    db().audit(
        "contract.confirmed", workspace_id=run["workspace_id"], actor=current_user_id(),
        object_type="agent_run", object_id=run_id,
        detail={"contract_version": confirmed["version"], "job_id": job["id"]},
    )
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


def _active_job(run_id: str) -> dict[str, Any] | None:
    return next((
        item for item in db().list("jobs", workspace_id=workspace_id(), limit=5000)
        if item.get("run_id") == run_id and item.get("status") in {"queued", "running"}
    ), None)


@bp.post("/api/analyses/<run_id>/control")
@api_errors
def control_analysis(run_id: str):
    run = _require_run(run_id)
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
        updated = _store().update_status(run_id, "cancelling", stop_reason="cancel_requested")
        if job:
            manager.cancel(job["id"])
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
    payload = body()
    mode = str(payload.get("mode") or "followup")
    if mode not in {"followup", "refresh", "reproduce", "reanalyze"}:
        raise ValueError("mode 无效")
    branch_payload = _branch(run, mode, str(payload.get("prompt") or "").strip())
    # Reuse the same validated endpoint implementation without issuing an internal HTTP request.
    source_ids = branch_payload["source_ids"]
    for source_id in source_ids:
        source = require_workspace_record("sources", str(source_id), run["workspace_id"])
        allowed_users = source.get("authorized_user_ids")
        if isinstance(allowed_users, list) and current_user_id() not in allowed_users:
            raise PermissionError("历史任务的来源授权已变化，不能继续分支")
    contract = TaskContract.from_payload(branch_payload["contract"] | {"source_scope": source_ids})
    child, _ = _store().create_run(
        workspace_id=run["workspace_id"], session_id=run["session_id"], actor_id=current_user_id(),
        source_scope=source_ids,
        allowed_tool_ids=available_formal_tools(db(), run["workspace_id"], run["session_id"], source_ids),
        provider_id=run.get("provider_id"), parent_run_id=run["id"], run_kind=mode,
    )
    _store().add_contract(child["id"], contract, expected_version=0)
    db().add_message(run["session_id"], "user", contract.objective, {"run_id": child["id"], "parent_run_id": run["id"]})
    _store().append_event(child["id"], "analysis.branched", {"parent_run_id": run["id"], "mode": mode})
    return ok(item=_snapshot(_store().get_run(child["id"]) or child)), 201
