from __future__ import annotations

import json
import threading

from flask import Blueprint, Response, request, stream_with_context

from ..services.agent_runtime import run_conversation
from ..services.hooks import dispatch_hooks
from .common import api_errors, body, db, ok, require_workspace_record, workspace_id


bp = Blueprint("conversation", __name__)
_cancelled: set[str] = set()
_lock = threading.RLock()


@bp.get("/api/sessions/<session_id>/messages")
@api_errors
def messages(session_id: str):
    require_workspace_record("sessions", session_id)
    return ok(items=db().messages(session_id, int(request.args.get("limit", "300"))))


@bp.get("/api/sessions/<session_id>/tool-results/<artifact_id>")
@api_errors
def tool_result(session_id: str, artifact_id: str):
    session = require_workspace_record("sessions", session_id)
    item = require_workspace_record("tool_results", artifact_id, session["workspace_id"])
    if item.get("session_id") != session_id:
        raise PermissionError("工具结果不属于当前会话")
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


@bp.post("/api/sessions/<session_id>/messages")
@api_errors
def chat(session_id: str):
    session = require_workspace_record("sessions", session_id)
    payload = body()
    question = str(payload.get("message") or "").strip()
    if not question:
        raise ValueError("消息不能为空")
    source_ids = payload.get("source_ids") or session.get("source_ids") or []
    provider_id = payload.get("provider_id") or session.get("provider_id")
    wid = session.get("workspace_id", workspace_id())
    for source_id in source_ids:
        require_workspace_record("sources", str(source_id), wid)
    if provider_id and provider_id != "environment-default":
        require_workspace_record("providers", str(provider_id), wid)

    @stream_with_context
    def generate():
        def should_cancel() -> bool:
            with _lock:
                return session_id in _cancelled

        try:
            for event in run_conversation(
                session_id=session_id,
                workspace_id=wid,
                question=question,
                source_ids=[str(item) for item in source_ids],
                provider_id=provider_id,
                skill_id=payload.get("skill_id"),
                should_cancel=should_cancel,
            ):
                with _lock:
                    if session_id in _cancelled:
                        _cancelled.discard(session_id)
                        yield f"event: cancelled\ndata: {json.dumps({'ok': False, 'cancelled': True}, ensure_ascii=False)}\n\n"
                        return
                yield event
            refreshed = db().get("sessions", session_id) or session
            if refreshed.get("feishu_bot_enabled") and refreshed.get("feishu_chat_id"):
                yield f"event: feishu_sync\ndata: {json.dumps({'status': 'sending'}, ensure_ascii=False)}\n\n"
                try:
                    from ..services.feishu_bot import sync_web_turn

                    assistant = next(
                        (item for item in reversed(db().messages(session_id, 5000)) if item.get("role") == "assistant"),
                        None,
                    )
                    if assistant:
                        sync_web_turn(db(), refreshed, question, assistant["content"])
                    yield f"event: feishu_sync\ndata: {json.dumps({'status': 'sent'}, ensure_ascii=False)}\n\n"
                except Exception:
                    yield f"event: feishu_sync\ndata: {json.dumps({'status': 'failed'}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            yield f"event: error\ndata: {json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False)}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@bp.post("/api/sessions/<session_id>/stop")
@api_errors
def stop_chat(session_id: str):
    require_workspace_record("sessions", session_id)
    with _lock:
        _cancelled.add(session_id)
    return ok(cancel_requested=True)


@bp.post("/api/sessions/<session_id>/compact")
@api_errors
def compact(session_id: str):
    session = require_workspace_record("sessions", session_id)
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
    session = require_workspace_record("sessions", session_id)
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
    session = require_workspace_record("sessions", session_id)
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
    session = require_workspace_record("sessions", session_id)
    items = [
        item for item in db().list("command_metrics", workspace_id=session["workspace_id"], limit=1000)
        if item.get("session_id") == session_id
    ]
    return ok(items=items)
