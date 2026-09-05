from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from flask import Flask, current_app

from ..core.database import Database, utcnow
from .jobs import get_job_manager
from .models import resolve_provider


EXTRACTION_SYSTEM = """你负责提取数据分析助手的长期记忆。仅返回 JSON 对象 {"ops": [...]}。
只记录用户明确表达的长期偏好、纠正、已确认的项目事实、指标口径或持久资源位置。
不要记录一次性任务、闲聊、原始查询数值、密码、Token、连接串或其他秘密。
每项包含 op(create/update)、type(user/feedback/project/reference)、name、title、body，可选 why、how_to_apply。
没有可持久化内容时返回 {"ops": []}，最多 5 项。"""


def _db() -> Database:
    return current_app.extensions["meridian_db"]


def _tokens(text: str) -> set[str]:
    lowered = str(text or "").lower()
    tokens = set(re.findall(r"[a-z0-9_]+", lowered))
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        tokens.update(run[index:index + 2] for index in range(max(0, len(run) - 1)))
    return tokens


def search_memories(query: str, workspace_id: str, limit: int = 12, user_id: str = "") -> list[dict]:
    records = [item for item in _db().list("memories", workspace_id=workspace_id) if item.get("enabled", True)]
    if user_id:
        records = [item for item in records if not item.get("user_id") or item.get("user_id") == user_id]
    if not query.strip():
        return records[:limit]
    query_tokens = _tokens(query)
    ranked = []
    for item in records:
        text = " ".join(str(item.get(key) or "") for key in ("name", "title", "content", "body", "why", "how_to_apply"))
        tokens = _tokens(text)
        score = len(query_tokens & tokens) / max(1, len(query_tokens))
        if query.lower() in text.lower():
            score += 1
        if score:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [{**item, "score": round(score, 4)} for score, item in ranked[: max(1, min(limit, 20))]]


def render_memory_context(workspace_id: str, query: str = "", user_id: str = "") -> str:
    memories = search_memories(query, workspace_id, 20, user_id)
    if not memories:
        return ""
    lines = ["长期记忆（仅在相关时应用，不得扩展或猜测）："]
    for item in memories:
        scope = item.get("type") or item.get("scope") or "project"
        body = item.get("body") or item.get("content") or ""
        lines.append(f"- [{scope}] {item.get('title') or item.get('name')}: {body}")
        if item.get("how_to_apply"):
            lines.append(f"  应用方式：{item['how_to_apply']}")
    return "\n".join(lines)[:12000]


def _needs_extraction(message: str) -> bool:
    return bool(re.search(
        r"请记住|记住这个|以后|从今以后|默认|偏好|我喜欢|我不喜欢|始终|务必|口径.{0,12}(?:是|为|=)|"
        r"定义.{0,8}(?:是|为|=)|公式.{0,8}(?:是|为|=)|财年|本项目|当前项目|纠正",
        message,
        re.IGNORECASE,
    ))


def _slug(value: str) -> str:
    latin = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if latin:
        return latin[:64]
    return "memory-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _deterministic_ops(message: str) -> list[dict]:
    if not _needs_extraction(message):
        return []
    cleaned = " ".join(message.strip().split())[:4000]
    global_scope = bool(re.search(r"以后|从今以后|所有项目|默认|偏好|我喜欢|我不喜欢|始终", cleaned))
    memory_type = "user" if global_scope else "project"
    title = cleaned[:60]
    return [{
        "op": "create", "type": memory_type, "name": _slug(title), "title": title,
        "body": cleaned, "why": "用户明确要求持久记忆",
        "how_to_apply": "在后续相关分析、图表或报告中遵循该规则。",
    }]


def _content(response: Any) -> str:
    message = response.choices[0].message
    value = message.content if not isinstance(message, dict) else message.get("content")
    if isinstance(value, list):
        return "\n".join(str(item.get("text") or "") for item in value if isinstance(item, dict))
    return str(value or "")


def _json_object(text: str) -> dict:
    clean = re.sub(r"```(?:json)?|```", "", text).strip()
    try:
        value = json.loads(clean)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, character in enumerate(clean):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(clean, index)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "ops" in value:
                return value
    return {}


def _apply_ops(ops: list[dict], workspace_id: str, user_id: str) -> list[dict]:
    applied = []
    existing = _db().list("memories", workspace_id=workspace_id, include_archived=True, limit=5000)
    for operation in ops[:5]:
        if not isinstance(operation, dict) or operation.get("op") not in {"create", "update"}:
            continue
        memory_type = str(operation.get("type") or "project")
        if memory_type not in {"user", "feedback", "project", "reference"}:
            continue
        body = str(operation.get("body") or "").strip()
        title = str(operation.get("title") or operation.get("name") or "").strip()
        if not body or not title:
            continue
        lower = body.lower()
        if re.search(r"(?:api[_ -]?key|password|密码|token|secret|连接串)\s*[:=]", lower):
            continue
        name = _slug(str(operation.get("name") or title))
        current = next((item for item in existing if item.get("name") == name), None)
        if not current:
            current = next((item for item in existing if item.get("title") == title), None)
        record = {
            **(current or {}),
            "id": (current or {}).get("id") or _db().new_id("mem"),
            "workspace_id": workspace_id, "user_id": user_id if memory_type in {"user", "feedback"} else "",
            "name": name, "type": memory_type, "scope": "user" if memory_type in {"user", "feedback"} else "workspace",
            "title": title[:120], "body": body[:12000], "content": body[:12000],
            "why": str(operation.get("why") or "")[:1000],
            "how_to_apply": str(operation.get("how_to_apply") or "")[:2000],
            "enabled": True, "last_confirmed_at": utcnow(),
        }
        saved = _db().put("memories", record, workspace_id=workspace_id)
        existing.append(saved)
        applied.append(saved)
    return applied


def schedule_memory_extraction(
    *,
    app: Flask,
    workspace_id: str,
    session_id: str,
    user_id: str,
    user_message: str,
    assistant_message: str,
    provider_id: str | None,
) -> dict | None:
    if not _needs_extraction(user_message):
        return None

    def work(progress, cancel):
        progress(10, "识别可持久化信息")
        if cancel.is_set():
            return {"cancelled": True}
        operations = _deterministic_ops(user_message)
        provider, client = resolve_provider(provider_id)
        if app.config.get("TESTING") and not app.config.get("MEMORY_EXTRACTION_IN_TESTS"):
            client = None
        if client:
            summaries = [
                {key: item.get(key) for key in ("name", "type", "title")}
                for item in _db().list("memories", workspace_id=workspace_id, limit=200)
            ]
            response = client.chat.completions.create(
                model=provider["model"],
                messages=[
                    {"role": "system", "content": EXTRACTION_SYSTEM},
                    {
                        "role": "user",
                        "content": json.dumps({
                            "existing": summaries, "user": user_message[:12000],
                            "assistant": assistant_message[:12000],
                        }, ensure_ascii=False),
                    },
                ],
                temperature=0,
                max_tokens=1600,
            )
            parsed = _json_object(_content(response))
            if isinstance(parsed.get("ops"), list):
                operations = parsed["ops"]
        progress(70, "写入长期记忆")
        applied = _apply_ops(operations, workspace_id, user_id)
        _db().put(
            "memory_notices",
            {
                "id": _db().new_id("memnotice"), "workspace_id": workspace_id,
                "session_id": session_id, "message": f"已更新 {len(applied)} 条长期记忆",
                "memory_ids": [item["id"] for item in applied], "read": False,
            },
            workspace_id=workspace_id,
        )
        return {"applied": len(applied), "memory_ids": [item["id"] for item in applied]}

    with app.app_context():
        return get_job_manager(app).submit(
            workspace_id=workspace_id, session_id=session_id, kind="memory_extraction",
            title=f"记忆提取：{user_message[:50]}", work=work,
        )


def consolidate_memories(workspace_id: str) -> dict:
    records = _db().list("memories", workspace_id=workspace_id, limit=5000)
    groups: dict[str, list[dict]] = {}
    for item in records:
        key = item.get("name") or re.sub(r"\s+", "", str(item.get("title") or "").lower())
        groups.setdefault(str(key), []).append(item)
    merged = archived = 0
    for items in groups.values():
        if len(items) < 2:
            continue
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        keeper = items[0]
        bodies = []
        for item in items:
            body = str(item.get("body") or item.get("content") or "").strip()
            if body and body not in bodies:
                bodies.append(body)
        keeper.update({"body": "\n".join(bodies)[:12000], "content": "\n".join(bodies)[:12000]})
        _db().put("memories", keeper, workspace_id=workspace_id)
        merged += 1
        for duplicate in items[1:]:
            archived += int(_db().archive("memories", duplicate["id"]))
    return {"merged_groups": merged, "archived": archived}
