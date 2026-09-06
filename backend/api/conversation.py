from __future__ import annotations

import hashlib
import json

from flask import Blueprint, Response, current_app, jsonify, request, session as flask_session

from ..agent.contracts import TaskContract
from ..agent.store import RunStore
from ..services.advanced_agent import available_formal_tools
from ..services.hooks import dispatch_hooks
from .common import (
    api_errors, body, db, ok, require_session_access, require_source_access,
    require_workspace_record, workspace_id,
)


bp = Blueprint("conversation", __name__)


def _owned_tool_result(session_id: str, artifact_id: str) -> tuple[dict, dict]:
    session = require_session_access(session_id)
    item = require_workspace_record("tool_results", artifact_id, session["workspace_id"])
    if item.get("session_id") != session_id:
        raise FileNotFoundError(f"tool_results 记录不存在：{artifact_id}")
    return session, item


@bp.get("/api/sessions/<session_id>/messages")
@api_errors
def messages(session_id: str):
    require_session_access(session_id)
    return ok(items=db().messages(session_id, int(request.args.get("limit", "300"))))


@bp.get("/api/sessions/<session_id>/tool-results/<artifact_id>")
@api_errors
def tool_result(session_id: str, artifact_id: str):
    _, item = _owned_tool_result(session_id, artifact_id)
    content = str(item.get("content") or "")
    query = str(request.args.get("query") or "").strip().lower()
    limit = max(1, min(int(request.args.get("limit", "4000")), 4000))
    if query:
        matches = []
        start = 0
        while len(matches) < 20:
            index = content.lower().find(query, start)
            if index < 0:
                break
            matches.append({
                "offset": index,
                "text": content[max(0, index - 160):min(len(content), index + len(query) + 320)],
            })
            start = index + max(1, len(query))
        return ok(artifact_id=artifact_id, matches=matches, total_chars=len(content))
    offset = max(0, int(request.args.get("offset", "0")))
    return ok(
        artifact_id=artifact_id, content=content[offset:offset + limit], offset=offset,
        next_offset=offset + limit if offset + limit < len(content) else None,
        total_chars=len(content),
    )


@bp.get("/api/session/<session_id>/tool-results/<artifact_id>")
@api_errors
def legacy_tool_result(session_id: str, artifact_id: str):
    """Serve the legacy singular-session tool-result contract.

    The modern endpoint above remains paginated JSON.  The compatibility route
    intentionally returns the complete text by default because legacy clients
    use it as an artifact URL, while ownership is still checked against both
    the workspace and session.
    """
    _, item = _owned_tool_result(session_id, artifact_id)
    content = str(item.get("content") or "")
    content_type = str(item.get("content_type") or "text/plain; charset=utf-8")
    digest = str(item.get("sha256") or hashlib.sha256(content.encode("utf-8")).hexdigest())
    record = {
        "version": 1,
        "artifact_id": artifact_id,
        "session_id": session_id,
        "workspace_id": item.get("workspace_id", ""),
        "tool": item.get("tool_name", ""),
        "data": content,
        "content_type": content_type,
        "sha256": digest,
        "total_chars": len(content),
    }
    if request.args.get("format") == "json":
        return jsonify(record)
    return Response(
        content,
        content_type=content_type,
        headers={
            "X-Artifact-Id": artifact_id,
            "X-Tool-Name": str(item.get("tool_name") or ""),
            "X-Content-SHA256": digest,
        },
    )


@bp.post("/api/sessions/<session_id>/messages")
@api_errors
def chat(session_id: str):
    session = require_session_access(session_id)
    payload = body()
    question = str(payload.get("message") or "").strip()
    if not question:
        raise ValueError("消息不能为空")
    if len(question) > 50_000:
        raise ValueError("单条消息不能超过 50000 个字符")
    source_ids = payload.get("source_ids") or session.get("source_ids") or []
    if not isinstance(source_ids, list) or len(source_ids) > 50:
        raise ValueError("单次分析最多关联 50 个数据源")
    provider_id = payload.get("provider_id") or session.get("provider_id")
    wid = session.get("workspace_id", workspace_id())
    for source_id in source_ids:
        require_source_access(str(source_id), wid, action="analyze")
    if provider_id and provider_id != "environment-default":
        require_workspace_record("providers", str(provider_id), wid)

    # Compatibility endpoint is deliberately a thin adapter: it creates the same
    # governed run and returns the required confirmation card. Execution never
    # lives in the HTTP request and never enters the retired fallback loop.
    store = RunStore(db())
    contract = TaskContract.from_payload({
        "objective": question,
        "coverage": payload.get("coverage") or "所选来源的已授权数据范围；请在确认卡核对时间口径",
        "dimensions": payload.get("dimensions") or ["时间", "业务实体", "可用分类属性"],
        "deliverables": payload.get("deliverables") or ["summary", "dashboard", "report"],
        "source_scope": source_ids,
    })
    actor = str(flask_session.get("user_id") or "local-default")
    run, created = store.create_run(
        workspace_id=wid, session_id=session_id, actor_id=actor,
        source_scope=[str(item) for item in source_ids],
        allowed_tool_ids=available_formal_tools(db(), wid, session_id, [str(item) for item in source_ids]),
        provider_id=provider_id, skill_id=payload.get("skill_id"),
        idempotency_key=str(request.headers.get("Idempotency-Key") or "") or None,
    )
    if created:
        db().add_message(session_id, "user", question, {"run_id": run["id"]})
        store.add_contract(run["id"], contract, expected_version=0)
        db().patch("sessions", session_id, {"current_run_id": run["id"]}, workspace_id=wid)
    event = {
        "run_id": run["id"], "status": "waiting_input", "requires_confirmation": True,
        "contract": (store.latest_contract(run["id"]) or {}).get("payload", contract.to_dict()),
        "contract_version": (store.latest_contract(run["id"]) or {}).get("version", 1),
    }
    wire = f"event: contract\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
    wire += f"event: done\ndata: {json.dumps({'ok': True, **event}, ensure_ascii=False)}\n\n"
    return Response(
        wire,
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@bp.post("/api/sessions/<session_id>/stop")
@api_errors
def stop_chat(session_id: str):
    session_record = require_session_access(session_id)
    store = RunStore(db())
    active = next((
        item for item in store.list_runs(session_record["workspace_id"], session_id=session_id, limit=100)
        if item["execution_status"] not in {"finished", "failed", "cancelled"}
    ), None)
    if not active:
        return ok(cancel_requested=False, idempotent=True)
    store.update_status(active["id"], "cancelling", stop_reason="cancel_requested")
    from ..services.jobs import get_job_manager

    job = next((
        item for item in db().list("jobs", workspace_id=session_record["workspace_id"], limit=5000)
        if item.get("run_id") == active["id"] and item.get("status") in {"queued", "running"}
    ), None)
    if job:
        get_job_manager(current_app._get_current_object()).cancel(job["id"])
    return ok(cancel_requested=True, run_id=active["id"])


@bp.post("/api/sessions/<session_id>/compact")
@api_errors
def compact(session_id: str):
    session = require_session_access(session_id)
    wid = session.get("workspace_id", workspace_id())
    messages = db().messages(session_id, 1000)
    keep = max(4, int(body().get("keep_recent", 12)))
    if len(messages) <= keep:
        return ok(compacted=False, messages=len(messages))
    pre_hooks = dispatch_hooks(
        "pre_compact", {"session_id": session_id, "message_count": len(messages)}, wid, database=db(),
    )
    earlier, recent = messages[:-keep], messages[-keep:]
    lines = []
    for item in earlier:
        prefix = "用户" if item["role"] == "user" else "分析助手"
        lines.append(f"{prefix}: {item['content'][:500]}")
    summary = {
        "id": db().new_id("msg"),
        "role": "system",
        "content": "早期会话摘要：\n" + "\n".join(lines)[-8000:],
        "metadata": {"compacted_messages": len(earlier)},
    }
    db().replace_messages(session_id, [summary, *recent])
    db().audit("session.compacted", workspace_id=wid, object_type="session", object_id=session_id, detail={"compacted": len(earlier)})
    post_hooks = dispatch_hooks(
        "post_compact",
        {"session_id": session_id, "compacted_messages": len(earlier), "message_count": len(recent) + 1},
        wid,
        database=db(),
    )
    return ok(compacted=True, messages=len(recent) + 1, hooks=[*pre_hooks, *post_hooks])


@bp.post("/api/sessions/<session_id>/clear")
@api_errors
def clear_conversation(session_id: str):
    session = require_session_access(session_id)
    count = len(db().messages(session_id, 1000))
    db().replace_messages(session_id, [])
    db().audit(
        "session.cleared", workspace_id=session["workspace_id"],
        object_type="session", object_id=session_id, detail={"messages": count},
    )
    return ok(cleared=count)


@bp.post("/api/sessions/<session_id>/commands/<name>/execute")
@bp.post("/api/session/<session_id>/commands/<name>/execute")
@api_errors
def execute_command(session_id: str, name: str):
    session = require_session_access(session_id)
    normalized = str(name or "").lower()
    aliases = {"c": "compact"}
    normalized = aliases.get(normalized, normalized)
    if normalized != "compact":
        raise ValueError(f"/{normalized} 是客户端命令，不能通过后端执行")
    before = len(db().messages(session_id, 1000))
    response = compact(session_id)
    db().put(
        "command_metrics",
        {
            "id": db().new_id("cmd"), "workspace_id": session["workspace_id"],
            "session_id": session_id, "command": normalized, "outcome": "success",
            "messages_before": before,
        },
        workspace_id=session["workspace_id"],
    )
    return response


@bp.get("/api/sessions/<session_id>/command-metrics")
@api_errors
def command_metrics(session_id: str):
    session = require_session_access(session_id)
    items = [
        item for item in db().list("command_metrics", workspace_id=session["workspace_id"], limit=1000)
        if item.get("session_id") == session_id
    ]
    return ok(items=items)
