from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Any

from flask import current_app

from ..agent.contracts import TaskContract
from ..agent.loop import AgentLoop
from ..agent.model import build_model_adapter
from ..agent.store import RunStore
from ..core.database import Database, utcnow
from .advanced_agent import _source_authorized, available_formal_tools, build_executor
from .authorization import require_session_access, require_sources_access
from .jobs import get_job_manager, register_job_handler
from .models import resolve_provider
from .results.manifests import ResultService
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


def run_team_member(
    team: dict,
    member: dict,
    task: str,
    shared_context: str,
    source_ids: list[str],
    session_id: str,
    cancel,
    result_max_tokens: int = 0,
    timeout_seconds: int = 300,
    parent_run_id: str | None = None,
    child_budget: dict[str, Any] | None = None,
    actor_id: str | None = None,
) -> dict:
    profile = _db().get("agent_profiles", str(member.get("profile_id") or ""))
    effective = {**(profile or {}), **member}
    provider, client = resolve_provider(effective.get("provider_id"), team["workspace_id"])
    name = effective.get("name") or effective.get("role") or "分析顾问"
    if not provider or not client:
        raise RuntimeError(f"团队成员 {name} 没有可用的模型配置")
    database = _db()
    session = database.get("sessions", session_id) or {}
    actor_id = str(actor_id or session.get("owner_id") or "local-default")
    if session:
        session = require_session_access(
            database, session_id, workspace_id=team["workspace_id"], actor_id=actor_id,
        )
    requested = _allowed_tools(effective, profile) | {"validate_result", "update_plan"}
    available = set(available_formal_tools(database, team["workspace_id"], session_id, source_ids))
    allowed = sorted(requested & available)
    store = RunStore(database)
    run, _created = store.create_run(
        workspace_id=team["workspace_id"], session_id=session_id, actor_id=actor_id,
        source_scope=source_ids, allowed_tool_ids=allowed, provider_id=effective.get("provider_id"),
        parent_run_id=parent_run_id, run_kind="team_member",
        budget=child_budget or {
            **RunStore.default_budget(),
            "model_tokens": max(4_000, int(result_max_tokens or 1200) * 12),
        },
    )
    contract = TaskContract.from_payload({
        "objective": task, "coverage": "团队任务中分配的子问题与已授权来源",
        "dimensions": ["分配子问题", "证据冲突", "数据限制"],
        "deliverables": ["team_member_response"], "source_scope": source_ids,
    })
    store.add_contract(run["id"], contract, expected_version=0, confirmed_by=actor_id)
    store.add_plan(run["id"], {
        "tasks": [{"id": "member_analysis", "title": task[:200], "status": "open", "depends_on": []}],
    }, reason="delegated_by_parent", expected_version=0)
    mailbox = _consume_mailbox(team, str(name))
    mailbox_text = "\n".join(
        f"- {item.get('sender', '成员')}: {item.get('content', '')}" for item in mailbox
    )
    history = [{
        "role": "user",
        "content": (
            f"你是团队成员“{name}”，职责：{effective.get('role') or '独立分析'}。"
            "必须用工具核对数据，区分事实、假设、风险和建议。"
            f"\n附加指令：{effective.get('instructions') or '无'}"
            f"\n任务：{task}\n共享上下文：{shared_context[:16000]}"
            f"\n未读团队消息：{mailbox_text or '无'}"
        ),
    }]
    loop = AgentLoop(
        store=store, model=build_model_adapter(client, provider),
        tools=build_executor(database, store.get_run(run["id"]) or run),
        finalizer=ResultService(database, authorize=lambda current: _source_authorized(database, current)).finalize,
        context_window=int(provider.get("context_window") or 32_768),
        max_output_tokens=min(
            int(provider.get("max_output_tokens") or 4_096), int(result_max_tokens or 1_200),
        ),
        max_iterations=12, max_run_seconds=max(10, min(300, int(timeout_seconds or 300))),
    )
    result = loop.run(
        run["id"], runner_id=f"team-member:{run['id']}", history=history,
        child_tools=set(allowed), should_cancel=cancel.is_set,
    )
    if not result.answer:
        raise RuntimeError(f"团队成员 {name} 未产生可交付回答：{result.stop_reason}")
    completed_run = store.get_run(run["id"]) or run
    tool_evidence = [{
        "tool": item["tool_id"], "ok": item["status"] == "succeeded",
        "result_id": ((item.get("result") or {}).get("value") or {}).get("result_id"),
        "error": item.get("error_code"),
    } for item in store.actions(run["id"])]
    return {
        "member": name, "role": effective.get("role") or name, "task": task,
        "content": result.answer, "mode": "agent_loop", "agent_run_id": run["id"],
        "outcome": result.outcome, "publication_id": result.publication_id,
        "tool_evidence": tool_evidence, "usage": completed_run["usage"],
    }


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
    """Persist a validated plan separately from execution for explicit confirmation."""
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
        "plan_id": plan["id"],
        "actor_id": payload.get("actor_id"),
        "timeout_seconds": payload.get("timeout_seconds"),
        "max_concurrency": payload.get("max_concurrency"),
        "result_max_tokens": payload.get("result_max_tokens"),
    })
    plan = _db().patch("team_plans", plan["id"], {"status": "running", "run_id": run["id"]}) or plan
    return plan, run, job


def delegate_once(
    *, team: dict | None, member: dict | None, prompt: str, description: str,
    workspace_id: str, source_ids: list[str], session_id: str, actor_id: str | None = None,
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
        session_id, threading.Event(), actor_id=actor_id,
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
        if source_ids and not response.get("publication_id"):
            issues.append(f"{response.get('member')} 的结论未通过正式结果发布门禁")
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
    if not provider or not client:
        raise RuntimeError("团队负责人没有可用的模型配置")
    quota = ensure_quota(_db(), team["workspace_id"])
    max_tokens = min(
        int(provider.get("max_output_tokens") or current_app.config["SETTINGS"].default_max_output_tokens),
        quota["remaining"],
    )
    response = build_model_adapter(client, provider).complete(
        [
            {"role": "system", "content": "你是团队负责人。综合成员结论，保留证据差异，禁止新增未经验证的数字。"},
            {"role": "user", "content": f"总任务：{task}\n成员结果：\n{material[:30000]}"},
        ],
        [], max_output_tokens=max(1, max_tokens),
    )
    record_usage(
        _db(), team["workspace_id"], {**response.usage, "model": provider["model"]},
        operation="team_synthesis",
    )
    if not response.content:
        raise RuntimeError("团队负责人未返回可交付的综合结论")
    return response.content


def _team_limits(payload: dict, current: dict | None, task_count: int) -> dict:
    existing = dict((current or {}).get("limits") or {})
    return {
        "timeout_seconds": max(10, min(300, int(payload.get("timeout_seconds") or existing.get("timeout_seconds") or 300))),
        "max_concurrency": max(1, min(8, int(payload.get("max_concurrency") or existing.get("max_concurrency") or min(6, task_count)))),
        "result_max_tokens": max(400, min(2500, int(payload.get("result_max_tokens") or existing.get("result_max_tokens") or 1200))),
    }


def start_team_run(team: dict, payload: dict, *, existing_run: dict | None = None) -> tuple[dict, dict]:
    task = str(payload.get("task") or (existing_run or {}).get("task") or team.get("objective") or "").strip()
    if not task:
        raise ValueError("协作任务不能为空")
    source_ids = [str(item) for item in payload.get("source_ids") or (existing_run or {}).get("source_ids") or []]
    requested_actor = str((existing_run or {}).get("actor_id") or payload.get("actor_id") or "")
    if not requested_actor:
        requested_session = _db().get("sessions", str(payload.get("session_id") or "")) or {}
        requested_actor = str(requested_session.get("owner_id") or "local-default")
    requested_session_id = str(payload.get("session_id") or (existing_run or {}).get("session_id") or "")
    if requested_session_id:
        require_session_access(
            _db(), requested_session_id, workspace_id=team["workspace_id"],
            actor_id=requested_actor,
        )
    require_sources_access(
        _db(), source_ids, workspace_id=team["workspace_id"], actor_id=requested_actor,
        action="analyze",
    )
    if existing_run:
        run = _db().patch(
            "team_runs", existing_run["id"],
            {"limits": _team_limits(payload, existing_run, len(existing_run.get("tasks") or []))},
            workspace_id=team["workspace_id"],
        ) or existing_run
    else:
        assignments = payload.get("assignments") or [{
            "task_id": f"task_{index}", "member_name": member.get("name") or member.get("role"),
            "prompt": task, "description": member.get("role") or task, "depends_on": [],
        } for index, member in enumerate(team["members"], 1)]
        tasks = _validate_assignments(team, assignments)
        session_id = str(payload.get("session_id") or "")
        session = _db().get("sessions", session_id) or {}
        actor_id = str(payload.get("actor_id") or session.get("owner_id") or "local-default")
        parent_store = RunStore(_db())
        parent, _ = parent_store.create_run(
            workspace_id=team["workspace_id"], session_id=session_id or f"team:{team['id']}",
            actor_id=actor_id, source_scope=source_ids,
            allowed_tool_ids=available_formal_tools(_db(), team["workspace_id"], session_id, source_ids),
            run_kind="team", budget=RunStore.default_budget(),
        )
        parent_store.add_contract(parent["id"], TaskContract.from_payload({
            "objective": task, "coverage": "已确认团队计划与选定数据范围",
            "dimensions": [item["description"] for item in tasks],
            "deliverables": ["team_synthesis"], "source_scope": source_ids,
        }), expected_version=0, confirmed_by=actor_id)
        parent_store.add_plan(parent["id"], {"tasks": [{
            "id": item["id"], "title": item["description"], "status": "open",
            "depends_on": item["depends_on"],
        } for item in tasks]}, reason="team_plan_confirmed", expected_version=0)
        run = _db().put("team_runs", {
            "id": _db().new_id("teamrun"), "workspace_id": team["workspace_id"], "team_id": team["id"],
            "agent_run_id": parent["id"], "task": task,
            "actor_id": actor_id,
            "context": str(payload.get("context") or "")[:12000], "source_ids": source_ids,
            "session_id": session_id, "status": "queued", "tasks": tasks,
            "responses": [], "review": None, "summary": "",
            "limits": _team_limits(payload, None, len(tasks)), "plan_id": str(payload.get("plan_id") or ""),
        }, workspace_id=team["workspace_id"])
    app = current_app._get_current_object()
    job = get_job_manager(app).submit_spec(
        workspace_id=team["workspace_id"], session_id=run.get("session_id"), job_type="team_run",
        title=f"协作分析：{team['name']}", spec={"team_run_id": run["id"]},
        run_id=run.get("agent_run_id"),
    )
    _db().patch("team_runs", run["id"], {"job_id": job["id"]}, workspace_id=team["workspace_id"])
    return _db().get("team_runs", run["id"]), job


def _team_job_handler(app, spec, progress, cancel):
    stored = _db().get("team_runs", str(spec.get("team_run_id") or ""))
    if not stored:
        raise FileNotFoundError("团队运行不存在")
    team = _db().get("teams", stored["team_id"], workspace_id=stored["workspace_id"])
    if not team:
        raise FileNotFoundError("团队不存在")
    stored = _db().patch(
        "team_runs", stored["id"], {"status": "running", "started_at": utcnow()},
        workspace_id=stored["workspace_id"],
    ) or stored
    tasks_by_id = {item["id"]: item for item in stored["tasks"]}
    members = {str(item.get("name") or item.get("role")): item for item in team["members"]}
    source_ids = list(stored.get("source_ids") or [])
    responses: list[dict] = list(stored.get("responses") or [])
    completed = {item["id"] for item in stored["tasks"] if item.get("status") == "completed"}
    parent = RunStore(_db()).get_run(str(stored.get("agent_run_id") or ""))
    parent_budget = dict((parent or {}).get("budget") or RunStore.default_budget())
    task_count = max(1, len(tasks_by_id))
    child_budget = {
        key: None if value is None else max(1, float(value) / task_count)
        for key, value in parent_budget.items()
    }
    while len(completed) < len(tasks_by_id):
        if cancel.is_set():
            stored["status"] = "cancelled"
            break
        ready = [item for item in tasks_by_id.values() if item["status"] == "pending" and set(item["depends_on"]) <= completed]
        if not ready:
            stored["status"] = "partial_failed" if any(item["status"] == "failed" for item in tasks_by_id.values()) else "failed"
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
                    int((stored.get("limits") or {}).get("result_max_tokens") or 1200),
                    int((stored.get("limits") or {}).get("timeout_seconds") or 300),
                    parent_run_id=stored.get("agent_run_id"),
                    child_budget=child_budget,
                    actor_id=str(stored.get("actor_id") or (parent or {}).get("actor_id") or "local-default"),
                )
                return item, result

        with ThreadPoolExecutor(
            max_workers=min(int((stored.get("limits") or {}).get("max_concurrency") or 6), len(ready)),
            thread_name_prefix="meridian-team",
        ) as pool:
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
                    / len(tasks_by_id) * 85, f"团队任务 {item['id']}：{item['status']}",
                )
                stored.update({"tasks": list(tasks_by_id.values()), "responses": responses})
                _db().put("team_runs", stored, workspace_id=team["workspace_id"])
    parent_store = RunStore(_db())
    if stored.get("status") == "cancelled":
        final = _db().put("team_runs", stored, workspace_id=team["workspace_id"])
        if final.get("agent_run_id"):
            parent_store.update_status(final["agent_run_id"], "cancelled", outcome="cancelled", stop_reason="team_cancelled")
        if final.get("plan_id"):
            _db().patch("team_plans", final["plan_id"], {"status": "cancelled", "run_id": final["id"]}, workspace_id=team["workspace_id"])
        return {"status": "cancelled", "run_id": final["id"]}
    review = _quality_review(team, responses, source_ids)
    summary = _synthesize(team, stored["task"], responses)
    failed = any(item["status"] == "failed" for item in tasks_by_id.values())
    parent_final = {"published": False, "quality_status": "blocked"}
    if parent:
        parent_evidence = [{
            "tool": "team_member_publication",
            "status": "SUCCEEDED" if response.get("publication_id") else "FAILED",
            "refs": [str(response["publication_id"])] if response.get("publication_id") else [],
            "completeness": "complete" if response.get("publication_id") else "unknown",
            "validation_status": "PASS" if response.get("publication_id") else "not_evaluated",
        } for response in responses]
        parent_final = ResultService(
            _db(), authorize=lambda current: _source_authorized(_db(), current),
        ).finalize(parent["id"], summary, parent_evidence)
    status = (
        "partial_failed" if failed else "completed"
        if review["status"] == "passed" and parent_final.get("published")
        else "needs_review"
    )
    stored.update({
        "status": status, "tasks": list(tasks_by_id.values()), "responses": responses,
        "review": review, "summary": summary, "finished_at": utcnow(),
        "result_manifest_id": parent_final.get("manifest_id"),
        "publication_id": parent_final.get("publication_id"),
        "budget": {key: sum(int(item.get("usage", {}).get(key, 0)) for item in responses) for key in ("model_tokens", "tool_calls")},
    })
    _db().put("team_runs", stored, workspace_id=team["workspace_id"])
    if stored.get("agent_run_id"):
        parent_store.update_status(
            stored["agent_run_id"], "finished" if status == "completed" else "failed",
            outcome="complete" if status == "completed" else "partial",
            quality_status="passed" if status == "completed" else str(parent_final.get("quality_status") or "failed"),
            stop_reason=f"team_{status}",
        )
    if stored.get("plan_id"):
        _db().patch("team_plans", stored["plan_id"], {"status": status, "run_id": stored["id"]}, workspace_id=team["workspace_id"])
    return {"run_id": stored["id"], "status": status, "responses": responses, "review": review, "summary": summary}


register_job_handler("team_run", _team_job_handler)


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
