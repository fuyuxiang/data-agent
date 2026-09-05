from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Any

from flask import current_app

from ..core.database import Database, utcnow
from .agent_tools import AgentToolContext, execute_tool, model_text, tool_schemas
from .jobs import get_job_manager
from .models import resolve_provider
from .usage import ensure_quota, record_usage


DEFAULT_MEMBER_TOOLS = {
    "get_schema", "profile_data", "query_data", "query_knowledge", "memory_read", "run_analysis",
}
TOOL_ALIASES = {
    "query": {"get_schema", "profile_data", "query_data"},
    "analysis": {"run_analysis"},
    "knowledge": {"query_knowledge"},
    "memory": {"memory_read"},
}


def _db() -> Database:
    return current_app.extensions["meridian_db"]


def _allowed_tools(member: dict, profile: dict | None) -> set[str]:
    configured = member.get("tools") or (profile or {}).get("tools") or (profile or {}).get("allowed_tools")
    if not configured:
        return set(DEFAULT_MEMBER_TOOLS)
    output = set()
    for name in configured:
        output.update(TOOL_ALIASES.get(str(name), {str(name)}))
    return output & DEFAULT_MEMBER_TOOLS


def _message_value(message: Any, key: str, default=None):
    return message.get(key, default) if isinstance(message, dict) else getattr(message, key, default)


def _usage(response: Any, model: str) -> dict:
    usage = getattr(response, "usage", None)
    getter = usage.get if isinstance(usage, dict) else lambda key, default=0: getattr(usage, key, default)
    result = {
        "model": model,
        "prompt_tokens": int(getter("prompt_tokens", 0) or 0),
        "completion_tokens": int(getter("completion_tokens", 0) or 0),
        "total_tokens": int(getter("total_tokens", 0) or 0),
    }
    result["total_tokens"] = result["total_tokens"] or result["prompt_tokens"] + result["completion_tokens"]
    return result


def _consume_mailbox(team: dict, member_name: str) -> list[dict]:
    messages = []
    for item in reversed(_db().list("team_messages", workspace_id=team["workspace_id"], limit=5000)):
        if item.get("team_id") != team["id"]:
            continue
        recipients = item.get("recipients") or []
        if recipients and member_name not in recipients and "*" not in recipients:
            continue
        read_by = list(item.get("read_by") or [])
        if member_name in read_by:
            continue
        read_by.append(member_name)
        _db().patch("team_messages", item["id"], {"read_by": read_by})
        messages.append(item)
    return messages


def _local_member_result(member: dict, task: str, context: AgentToolContext) -> dict:
    evidence = []
    summaries = []
    if context.source_ids:
        schema, _ = execute_tool("get_schema", {}, context)
        evidence.append({"tool": "get_schema", "ok": True})
        summaries.append(f"已检查 {len(schema.get('sources', []))} 个数据源的结构。")
        for source_id in context.source_ids[:3]:
            result, _ = execute_tool("profile_data", {"source_id": source_id}, context)
            evidence.append({"tool": "profile_data", "ok": True, "source_id": source_id})
            profile = result.get("profile", {})
            summaries.append(
                f"{result.get('source', {}).get('name', source_id)}：{profile.get('rows', 0)} 行，"
                f"质量分 {profile.get('quality_score', '未知')}。"
            )
    else:
        summaries.append("当前任务没有绑定数据源，无法形成数据证据；已保留为待模型分析项。")
    role = member.get("role") or member.get("name") or "分析顾问"
    return {
        "member": member.get("name") or role, "role": role, "task": task,
        "content": "\n".join(summaries), "mode": "local", "tool_evidence": evidence,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "model": "local"},
    }


def run_team_member(
    team: dict,
    member: dict,
    task: str,
    shared_context: str,
    source_ids: list[str],
    session_id: str,
    cancel,
) -> dict:
    profile = _db().get("agent_profiles", str(member.get("profile_id") or ""))
    effective = {**(profile or {}), **member}
    provider, client = resolve_provider(effective.get("provider_id"), team["workspace_id"])
    context = AgentToolContext(
        database=_db(), workspace_id=team["workspace_id"], session_id=session_id,
        source_ids=source_ids,
    )
    if source_ids:
        context.sources()
    if not client:
        return _local_member_result(effective, task, context)
    allowed = _allowed_tools(effective, profile)
    schemas = [item for item in tool_schemas(context) if item["function"]["name"] in allowed]
    name = effective.get("name") or effective.get("role") or "分析顾问"
    mailbox = _consume_mailbox(team, str(name))
    mailbox_text = "\n".join(
        f"- {item.get('sender', '成员')}: {item.get('content', '')}" for item in mailbox
    )
    messages = [
        {
            "role": "system",
            "content": (
                f"你是团队成员“{name}”，职责：{effective.get('role') or '独立分析'}。"
                "你必须用可用工具核对数据，不得编造字段和数值；明确事实、假设、风险和建议。"
                f"\n附加指令：{effective.get('instructions') or '无'}"
            ),
        },
        {
            "role": "user",
            "content": f"任务：{task}\n共享上下文：{shared_context[:16000]}\n未读团队消息：{mailbox_text or '无'}",
        },
    ]
    tool_evidence = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "model": provider["model"]}
    consecutive_errors = 0
    for _turn in range(12):
        if cancel.is_set():
            raise RuntimeError("团队任务已取消")
        quota = ensure_quota(_db(), team["workspace_id"])
        max_tokens = min(
            int(provider.get("max_output_tokens") or current_app.config["SETTINGS"].default_max_output_tokens),
            quota["remaining"],
        )
        response = client.chat.completions.create(
            model=provider["model"], messages=messages, tools=schemas, tool_choice="auto",
            temperature=float(provider.get("temperature", 0.2)), max_tokens=max(1, max_tokens),
        )
        current_usage = _usage(response, provider["model"])
        record_usage(
            _db(), team["workspace_id"], current_usage,
            session_id=session_id, operation="team_member",
        )
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            usage_total[key] += current_usage[key]
        message = response.choices[0].message
        content = str(_message_value(message, "content", "") or "")
        raw_calls = _message_value(message, "tool_calls", []) or []
        calls = []
        for call in raw_calls:
            function = _message_value(call, "function", {})
            calls.append({
                "id": str(_message_value(call, "id", _db().new_id("call"))), "type": "function",
                "function": {
                    "name": str(_message_value(function, "name", "")),
                    "arguments": str(_message_value(function, "arguments", "{}") or "{}"),
                },
            })
        if not calls:
            if not content.strip():
                raise RuntimeError(f"团队成员 {name} 未返回结果")
            return {
                "member": name, "role": effective.get("role") or name, "task": task,
                "content": content.strip(), "mode": "model", "tool_evidence": tool_evidence,
                "usage": usage_total,
            }
        messages.append({"role": "assistant", "content": content or None, "tool_calls": calls})
        for call in calls:
            tool_name = call["function"]["name"]
            if tool_name not in allowed:
                result, ok = {"ok": False, "error": "团队成员无权调用该工具"}, False
            else:
                try:
                    arguments = json.loads(call["function"]["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError("工具参数必须是对象")
                    result, _events = execute_tool(tool_name, arguments, context)
                    ok = True
                    consecutive_errors = 0
                except Exception as exc:
                    result, ok = {"ok": False, "error": str(exc)}, False
                    consecutive_errors += 1
            tool_evidence.append({
                "tool": tool_name, "ok": ok,
                "result_id": result.get("id") if isinstance(result, dict) else None,
                "error": result.get("error") if isinstance(result, dict) else None,
            })
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": model_text(result)})
        if consecutive_errors >= 3:
            raise RuntimeError(f"团队成员 {name} 连续工具调用失败")
    raise RuntimeError(f"团队成员 {name} 达到最大推理轮数")


def _validate_assignments(team: dict, assignments: list[dict]) -> list[dict]:
    if not 1 <= len(assignments) <= 8:
        raise ValueError("团队计划必须包含 1–8 个任务")
    member_names = {str(item.get("name") or item.get("role")) for item in team["members"]}
    seen = set()
    normalized = []
    for index, item in enumerate(assignments, 1):
        task_id = str(item.get("task_id") or f"task_{index}")[:64]
        member_name = str(item.get("member_name") or "")
        if not task_id or task_id in seen or member_name not in member_names or not str(item.get("prompt") or "").strip():
            raise ValueError("团队任务 ID、成员或提示无效")
        seen.add(task_id)
        normalized.append({
            "id": task_id, "member_name": member_name, "prompt": str(item["prompt"])[:12000],
            "description": str(item.get("description") or task_id)[:240],
            "depends_on": [str(value) for value in item.get("depends_on", [])],
            "status": "pending", "attempt": 1, "result": None, "error": "",
        })
    if any(dependency not in seen for item in normalized for dependency in item["depends_on"]):
        raise ValueError("团队任务依赖不存在")
    unresolved = {item["id"]: set(item["depends_on"]) for item in normalized}
    resolved = set()
    while unresolved:
        ready = [task_id for task_id, dependencies in unresolved.items() if dependencies <= resolved]
        if not ready:
            raise ValueError("团队任务依赖存在环路")
        for task_id in ready:
            resolved.add(task_id)
            unresolved.pop(task_id)
    return normalized


def create_team_plan(team: dict, payload: dict) -> dict:
    """Persist a validated plan separately from execution, matching the reference confirmation flow."""
    assignments = payload.get("assignments")
    if not isinstance(assignments, list):
        raise ValueError("团队计划 assignments 必须是数组")
    tasks = _validate_assignments(team, assignments)
    return _db().put(
        "team_plans",
        {
            "id": _db().new_id("teamplan"), "workspace_id": team["workspace_id"],
            "team_id": team["id"], "team_name": team["name"],
            "goal": str(payload.get("goal") or "").strip()[:12000],
            "tasks": tasks, "source_ids": [str(value) for value in payload.get("source_ids") or []],
            "status": "planned", "run_id": None,
        },
        workspace_id=team["workspace_id"],
    )


def start_team_plan(team: dict, plan: dict, payload: dict | None = None) -> tuple[dict, dict, dict]:
    payload = payload or {}
    if plan.get("team_id") != team["id"] or plan.get("workspace_id") != team["workspace_id"]:
        raise PermissionError("团队计划不属于当前团队")
    if plan.get("status") not in {"planned", "failed", "needs_review", "partial_failed"}:
        raise ValueError("团队计划当前状态不可执行")
    assignments = [
        {
            "task_id": item["id"], "member_name": item["member_name"],
            "prompt": item["prompt"], "description": item.get("description"),
            "depends_on": item.get("depends_on") or [],
        }
        for item in plan.get("tasks") or []
    ]
    run, job = start_team_run(team, {
        "task": plan.get("goal"), "assignments": assignments,
        "source_ids": payload.get("source_ids") or plan.get("source_ids") or [],
        "session_id": payload.get("session_id"), "context": payload.get("context", ""),
    })
    plan = _db().patch("team_plans", plan["id"], {"status": "running", "run_id": run["id"]}) or plan
    return plan, run, job


def delegate_once(
    *, team: dict | None, member: dict | None, prompt: str, description: str,
    workspace_id: str, source_ids: list[str], session_id: str,
) -> dict:
    """Execute one delegated sub-agent synchronously so the parent can consume its evidence."""
    effective_member = member or {
        "name": "独立分析顾问", "role": description or "独立分析",
        "instructions": "独立核对任务，只使用工具返回的证据。",
    }
    effective_team = team or {
        "id": f"delegate-{session_id}", "workspace_id": workspace_id,
        "name": "临时委派", "members": [effective_member],
    }
    result = run_team_member(
        effective_team, effective_member, prompt, description, source_ids,
        session_id, threading.Event(),
    )
    review = _quality_review(effective_team, [result], source_ids)
    return {"result": result, "review": review}


def _quality_review(team: dict, responses: list[dict], source_ids: list[str]) -> dict:
    issues = []
    for response in responses:
        if response.get("error"):
            issues.append(f"{response.get('member')} 执行失败：{response['error']}")
        if source_ids and not any(item.get("ok") for item in response.get("tool_evidence", [])):
            issues.append(f"{response.get('member')} 未提供可复核的数据/知识工具证据")
        if any(not item.get("ok") for item in response.get("tool_evidence", [])):
            issues.append(f"{response.get('member')} 存在失败的工具调用")
    return {
        "reviewer": "固定证据复核员", "status": "blocked" if issues else "passed",
        "issues": issues, "summary": "；".join(issues) if issues else "成员结果具备基本证据链，可以交付。",
    }


def _synthesize(team: dict, task: str, responses: list[dict]) -> str:
    lead = next(
        (item for item in team["members"] if item.get("profile_id") == team.get("lead_profile_id")),
        team["members"][0],
    )
    provider, client = resolve_provider(lead.get("provider_id"), team["workspace_id"])
    material = "\n\n".join(f"### {item['member']}\n{item.get('content') or item.get('error')}" for item in responses)
    if not client:
        return material
    quota = ensure_quota(_db(), team["workspace_id"])
    max_tokens = min(
        int(provider.get("max_output_tokens") or current_app.config["SETTINGS"].default_max_output_tokens),
        quota["remaining"],
    )
    response = client.chat.completions.create(
        model=provider["model"],
        messages=[
            {"role": "system", "content": "你是团队负责人。综合成员结论，保留证据差异，禁止新增未经验证的数字。"},
            {"role": "user", "content": f"总任务：{task}\n成员结果：\n{material[:30000]}"},
        ],
        temperature=float(provider.get("temperature", 0.2)), max_tokens=max(1, max_tokens),
    )
    record_usage(
        _db(), team["workspace_id"], _usage(response, provider["model"]),
        operation="team_synthesis",
    )
    return str(response.choices[0].message.content or material)


def start_team_run(team: dict, payload: dict, *, existing_run: dict | None = None) -> tuple[dict, dict]:
    task = str(payload.get("task") or (existing_run or {}).get("task") or team.get("objective") or "").strip()
    if not task:
        raise ValueError("协作任务不能为空")
    source_ids = [str(item) for item in payload.get("source_ids") or (existing_run or {}).get("source_ids") or []]
    for source_id in source_ids:
        source = _db().get("sources", source_id)
        if not source or source.get("workspace_id", "default") != team["workspace_id"]:
            raise PermissionError("团队任务引用的数据源不属于当前工作空间")
    if existing_run:
        run = existing_run
    else:
        assignments = payload.get("assignments")
        if not assignments:
            assignments = [
                {
                    "task_id": f"task_{index}", "member_name": member.get("name") or member.get("role"),
                    "prompt": task, "description": member.get("role") or task, "depends_on": [],
                }
                for index, member in enumerate(team["members"], 1)
            ]
        tasks = _validate_assignments(team, assignments)
        run = _db().put(
            "team_runs",
            {
                "id": _db().new_id("teamrun"), "workspace_id": team["workspace_id"], "team_id": team["id"],
                "task": task, "context": str(payload.get("context") or "")[:12000], "source_ids": source_ids,
                "session_id": str(payload.get("session_id") or ""), "status": "queued", "tasks": tasks,
                "responses": [], "review": None, "summary": "",
            },
            workspace_id=team["workspace_id"],
        )
    app = current_app._get_current_object()

    def work(progress, cancel):
        with app.app_context():
            stored = _db().patch("team_runs", run["id"], {"status": "running", "started_at": utcnow()}) or run
            tasks_by_id = {item["id"]: item for item in stored["tasks"]}
            members = {str(item.get("name") or item.get("role")): item for item in team["members"]}
            responses: list[dict] = list(stored.get("responses") or [])
            completed = {item["id"] for item in stored["tasks"] if item.get("status") == "completed"}
            while len(completed) < len(tasks_by_id):
                if cancel.is_set():
                    stored["status"] = "cancelled"
                    break
                ready = [
                    item for item in tasks_by_id.values()
                    if item["status"] == "pending" and set(item["depends_on"]) <= completed
                ]
                if not ready:
                    failed = [item for item in tasks_by_id.values() if item["status"] == "failed"]
                    stored["status"] = "partial_failed" if failed else "failed"
                    break

                def execute(item: dict) -> tuple[dict, dict]:
                    with app.app_context():
                        dependency_context = "\n".join(
                            str(tasks_by_id[dependency].get("result", {}).get("content", ""))
                            for dependency in item["depends_on"]
                        )
                        result = run_team_member(
                            team, members[item["member_name"]], item["prompt"],
                            f"{stored['context']}\n{dependency_context}", source_ids,
                            stored.get("session_id") or stored["id"], cancel,
                        )
                        return item, result

                with ThreadPoolExecutor(max_workers=min(6, len(ready)), thread_name_prefix="meridian-team") as pool:
                    futures = {pool.submit(execute, item): item for item in ready}
                    for future in as_completed(futures):
                        item = futures[future]
                        try:
                            _, response = future.result()
                            item.update({"status": "completed", "result": response, "finished_at": utcnow()})
                            responses.append(response)
                            completed.add(item["id"])
                        except Exception as exc:
                            item.update({"status": "failed", "error": str(exc), "finished_at": utcnow()})
                            responses.append({
                                "member": item["member_name"], "task": item["prompt"],
                                "content": "", "error": str(exc), "tool_evidence": [],
                            })
                        progress(
                            len([value for value in tasks_by_id.values() if value["status"] in {"completed", "failed"}])
                            / len(tasks_by_id) * 85,
                            f"团队任务 {item['id']}：{item['status']}",
                        )
                        stored.update({"tasks": list(tasks_by_id.values()), "responses": responses})
                        _db().put("team_runs", stored, workspace_id=team["workspace_id"])
            if stored.get("status") == "cancelled":
                final = _db().put("team_runs", stored, workspace_id=team["workspace_id"])
                return {"status": "cancelled", "run_id": final["id"]}
            review = _quality_review(team, responses, source_ids)
            summary = _synthesize(team, task, responses)
            failed = any(item["status"] == "failed" for item in tasks_by_id.values())
            status = "partial_failed" if failed else ("needs_review" if review["status"] == "blocked" else "completed")
            stored.update({
                "status": status, "tasks": list(tasks_by_id.values()), "responses": responses,
                "review": review, "summary": summary, "finished_at": utcnow(),
                "budget": {
                    key: sum(int(item.get("usage", {}).get(key, 0)) for item in responses)
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                },
            })
            _db().put("team_runs", stored, workspace_id=team["workspace_id"])
            return {"run_id": stored["id"], "status": status, "responses": responses, "review": review, "summary": summary}

    job = get_job_manager(app).submit(
        workspace_id=team["workspace_id"], session_id=run.get("session_id"), kind="team",
        title=f"协作分析：{team['name']}", work=work,
    )
    _db().patch("team_runs", run["id"], {"job_id": job["id"]})
    return _db().get("team_runs", run["id"]), job


def retry_team_run(run: dict, task_ids: list[str] | None = None) -> dict:
    selected = set(task_ids or [item["id"] for item in run.get("tasks", []) if item.get("status") == "failed"])
    changed = True
    while changed:
        before = len(selected)
        selected.update(
            item["id"] for item in run.get("tasks", []) if any(dep in selected for dep in item.get("depends_on", []))
        )
        changed = len(selected) != before
    if not selected:
        raise ValueError("没有可重试的团队任务")
    for item in run["tasks"]:
        if item["id"] in selected:
            item.update({
                "status": "pending", "attempt": int(item.get("attempt", 1)) + 1,
                "result": None, "error": "", "finished_at": None,
            })
    run["responses"] = [
        item for item in run.get("responses", [])
        if not any(task["member_name"] == item.get("member") and task["id"] in selected for task in run["tasks"])
    ]
    run.update({"status": "queued", "review": None, "finished_at": None})
    return _db().put("team_runs", run, workspace_id=run["workspace_id"])


def team_run_to_workflow(team: dict, run: dict) -> dict:
    if run.get("status") != "completed":
        raise ValueError("只有已通过复核的团队计划可以转为工作流")
    steps = []
    for task in run.get("tasks", []):
        member = next(item for item in team["members"] if (item.get("name") or item.get("role")) == task["member_name"])
        steps.append({
            "id": task["id"], "name": task.get("description") or task["id"], "type": "agent",
            "depends_on": deepcopy(task.get("depends_on") or []),
            "config": {
                "prompt": task["prompt"], "agent_profile_id": member.get("profile_id"),
                "provider_id": member.get("provider_id"),
            },
        })
    return _db().put(
        "workflows",
        {
            "id": _db().new_id("flow"), "workspace_id": team["workspace_id"],
            "name": f"{team['name']} · 团队计划", "description": f"由团队运行 {run['id']} 转换",
            "definition": {"steps": steps}, "status": "draft", "version": 0,
            "draft_revision": 1, "published_definition": None, "current_version_id": None,
        },
        workspace_id=team["workspace_id"],
    )
