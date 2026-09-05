from __future__ import annotations

import json
import hashlib
import re
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Any

from flask import current_app

from ..core.database import Database, utcnow
from .analytics import run_analysis
from .datasets import execute_query, load_result_frame, source_table
from .exports import export_data, export_report
from .hooks import dispatch_hooks
from .jobs import get_job_manager


STEP_TYPES = {
    "query": {"name": "数据查询", "required": ["source_ids", "sql"]},
    "analysis": {"name": "统计分析", "required": ["method"]},
    "export_data": {"name": "数据导出", "required": ["format"]},
    "export_report": {"name": "报告生成", "required": ["format"]},
    "approval": {"name": "人工审批", "required": []},
    "notification": {"name": "通知", "required": ["message"]},
    "validation": {"name": "规则校验", "required": []},
    "router": {"name": "条件路由", "required": []},
    "agent": {"name": "Agent 节点", "required": ["prompt"]},
    "verifier": {"name": "复核节点", "required": ["prompt"]},
}
EDGE_TYPES = {"auto", "conditional", "approval", "retry_loop"}


class WorkflowConcurrencyLimiter:
    """Process-local dispatch quota; durable node state remains in the database."""

    def __init__(self, global_limit: int = 6, workspace_limit: int = 3, run_limit: int = 2, profile_limit: int = 1):
        self.limits = {
            "global": global_limit, "workspace": workspace_limit,
            "run": run_limit, "profile": profile_limit,
        }
        self._active: set[tuple[str, str, str, str]] = set()
        self._lock = threading.RLock()

    def acquire(self, workspace_id: str, run_id: str, profile_id: str, node_id: str) -> bool:
        key = (workspace_id, run_id, profile_id, node_id)
        with self._lock:
            if key in self._active:
                return False
            if len(self._active) >= self.limits["global"]:
                return False
            if sum(1 for item in self._active if item[0] == workspace_id) >= self.limits["workspace"]:
                return False
            if sum(1 for item in self._active if item[1] == run_id) >= self.limits["run"]:
                return False
            if sum(1 for item in self._active if item[2] == profile_id) >= self.limits["profile"]:
                return False
            self._active.add(key)
            return True

    def release(self, workspace_id: str, run_id: str, profile_id: str, node_id: str) -> None:
        with self._lock:
            self._active.discard((workspace_id, run_id, profile_id, node_id))


WORKFLOW_LIMITER = WorkflowConcurrencyLimiter()


def _db() -> Database:
    return current_app.extensions["meridian_db"]


def normalize_definition(definition: dict) -> dict:
    """Accept the simple step format and the reference project's graph contract."""
    if isinstance(definition.get("steps"), list):
        return deepcopy(definition)
    nodes = definition.get("nodes")
    edges = definition.get("edges")
    if not isinstance(nodes, list):
        return deepcopy(definition)
    incoming: dict[str, list[dict]] = defaultdict(list)
    for edge in edges if isinstance(edges, list) else []:
        incoming[str(edge.get("to_node") or "")].append(deepcopy(edge))
    steps = []
    for node in nodes:
        node_id = str(node.get("node_id") or node.get("id") or "")
        node_type = str(node.get("type") or "agent")
        config = deepcopy(node.get("config") or {})
        for key in (
            "prompt", "source_ids", "source_id", "sql", "method", "params", "format",
            "message", "connector_id", "provider_id", "rules", "expression",
            "agent_profile_id",
        ):
            if key in node and key not in config:
                config[key] = deepcopy(node[key])
        if node_type == "sql":
            node_type = "query"
        elif node_type == "export":
            node_type = "export_report" if config.get("kind") == "report" else "export_data"
        steps.append({
            "id": node_id,
            "name": node.get("name") or node_id,
            "type": node_type,
            "config": config,
            "depends_on": [str(edge.get("from_node")) for edge in incoming[node_id] if edge.get("type") != "retry_loop"],
            "incoming_edges": incoming[node_id],
            "join_policy": node.get("join_policy", "all_success"),
            "input_contract": deepcopy(node.get("input_contract") or []),
            "output_contract": deepcopy(node.get("output_contract") or []),
            "limits": deepcopy(node.get("limits") or {}),
            "retry": deepcopy(node.get("retry") or {}),
            "on_error": node.get("on_error", "fail_run"),
            "when": deepcopy(node.get("when")),
        })
    return {**deepcopy(definition), "steps": steps, "graph_source": True}


def validate_definition(definition: dict) -> dict:
    normalized = normalize_definition(definition)
    steps = normalized.get("steps")
    if not isinstance(steps, list) or not steps:
        return {"valid": False, "errors": ["工作流至少需要一个步骤"], "order": [], "definition": normalized}
    ids = [str(step.get("id") or "") for step in steps]
    errors: list[str] = []
    if any(not step_id for step_id in ids):
        errors.append("每个步骤必须有 id")
    if len(ids) != len(set(ids)):
        errors.append("步骤 id 不能重复")
    by_id = {str(step.get("id")): step for step in steps}
    indegree = {step_id: 0 for step_id in ids}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for step in steps:
        step_id = str(step.get("id") or "")
        step_type = str(step.get("type") or "")
        if step_type not in STEP_TYPES:
            errors.append(f"步骤 {step_id} 的类型无效：{step_type}")
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        for key in STEP_TYPES.get(step_type, {}).get("required", []):
            if key not in config:
                errors.append(f"步骤 {step_id} 缺少配置：{key}")
        join_policy = str(step.get("join_policy") or "all_success")
        if join_policy not in {"all_success", "all_terminal"}:
            errors.append(f"步骤 {step_id} 的 join_policy 无效：{join_policy}")
        for dependency in step.get("depends_on", []):
            if dependency not in by_id:
                errors.append(f"步骤 {step_id} 依赖不存在的步骤：{dependency}")
                continue
            outgoing[dependency].append(step_id)
            indegree[step_id] += 1
        for edge in step.get("incoming_edges", []):
            edge_type = str(edge.get("type") or "auto")
            if edge_type not in EDGE_TYPES:
                errors.append(f"进入步骤 {step_id} 的边类型无效：{edge_type}")
            if edge_type == "retry_loop" and int(edge.get("max_iterations") or 0) < 1:
                errors.append(f"步骤 {step_id} 的 retry_loop 必须配置正整数 max_iterations")
    queue = deque(sorted(step_id for step_id, degree in indegree.items() if degree == 0))
    order = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for target in outgoing[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if len(order) != len(ids):
        errors.append("步骤的非重试依赖存在环路")
    entries = normalized.get("entry_node_ids")
    if entries is not None:
        if not isinstance(entries, list) or not entries:
            errors.append("工作流图必须声明至少一个入口节点")
        elif any(str(item) not in by_id for item in entries):
            errors.append("工作流入口节点不存在")
    return {
        "valid": not errors, "errors": errors, "order": order,
        "definition": normalized, "node_count": len(steps),
        "edge_count": sum(len(step.get("depends_on", [])) for step in steps),
    }


def create_published_workflow(
    *, workspace_id: str, name: str, description: str, definition: dict,
    mode: str = "full_auto", input_schema: dict | None = None, output_schema: dict | None = None,
) -> dict:
    validation = validate_definition(definition)
    if not validation["valid"]:
        raise ValueError("；".join(validation["errors"]))
    if mode not in {"full_auto", "key_approval", "exception_review"}:
        raise ValueError("工作流模式无效")
    normalized = validation["definition"]
    workflow = _db().put(
        "workflows",
        {
            "id": _db().new_id("flow"), "workspace_id": workspace_id,
            "name": str(name or "分析工作流")[:120], "description": str(description or "")[:1000],
            "definition": deepcopy(normalized), "published_definition": deepcopy(normalized),
            "input_schema": input_schema or {}, "output_schema": output_schema or {},
            "mode": mode, "status": "published", "version": 1, "draft_revision": 1,
        },
        workspace_id=workspace_id,
    )
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    version = _db().put(
        "workflow_versions",
        {
            "id": _db().new_id("wfver"), "workspace_id": workspace_id,
            "workflow_id": workflow["id"], "version": 1,
            "graph_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "definition": deepcopy(normalized), "input_schema": input_schema or {},
            "output_schema": output_schema or {}, "published_at": utcnow(),
        },
        workspace_id=workspace_id,
    )
    return _db().patch(
        "workflows", workflow["id"],
        {"current_version_id": version["id"], "published_at": utcnow()},
    ) or workflow


def template_definition(template: str, source_key: str = "source_ids") -> dict:
    key = str(source_key or "source_ids")
    templates: dict[str, list[dict]] = {
        "analysis": [
            {"id": "data", "name": "数据核对", "type": "agent", "depends_on": [], "config": {"prompt": f"检查输入 {key} 的表结构、质量和可用口径。"}},
            {"id": "quant", "name": "量化分析", "type": "agent", "depends_on": ["data"], "config": {"prompt": "基于上游数据证据完成定量分析，标注不确定性。"}},
            {"id": "review", "name": "证据复核", "type": "verifier", "depends_on": ["quant"], "config": {"prompt": "复核字段、SQL、样本量、口径和结论的证据链。"}},
        ],
        "insight": [
            {"id": "insight", "name": "洞察分析", "type": "agent", "depends_on": [], "config": {"prompt": f"对 {key} 先查结构和数据，再给出可复核洞察。"}},
            {"id": "review", "name": "洞察复核", "type": "verifier", "depends_on": ["insight"], "config": {"prompt": "检查所有数字是否有工具证据，指出夸大或无法支持的结论。"}},
        ],
        "report": [
            {"id": "analysis", "name": "报告分析", "type": "agent", "depends_on": [], "config": {"prompt": f"基于 {key} 形成执行摘要、关键证据、风险和行动建议。"}},
            {"id": "review", "name": "报告复核", "type": "verifier", "depends_on": ["analysis"], "config": {"prompt": "检查报告数字与数据证据是否一致。"}},
        ],
        "cleaning_approval": [
            {"id": "profile", "name": "清洗评估", "type": "agent", "depends_on": [], "config": {"prompt": f"评估 {key} 的缺失、重复、异常和类型问题，只提出非破坏性方案。"}},
            {"id": "approval", "name": "清洗审批", "type": "approval", "depends_on": ["profile"], "config": {}},
            {"id": "verify", "name": "清洗后复核", "type": "verifier", "depends_on": ["approval"], "config": {"prompt": "复核获批的清洗方案和数据血缘，不覆盖原始数据。"}},
        ],
    }
    if template not in templates:
        raise ValueError("工作流模板必须是 analysis、insight、report 或 cleaning_approval")
    return {"steps": deepcopy(templates[template]), "source_key": key}


def _path_get(context: dict, path: str) -> Any:
    value: Any = context
    for part in filter(None, str(path or "").split(".")):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


_VARIABLE = re.compile(r"(?:\$\{|\{\{\s*)([A-Za-z_][A-Za-z0-9_.]*)(?:\}|\s*\}\})")


def _resolve(value: Any, context: dict) -> Any:
    if isinstance(value, dict):
        return {key: _resolve(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, context) for item in value]
    if not isinstance(value, str):
        return value
    match = _VARIABLE.fullmatch(value)
    if match:
        return deepcopy(_path_get(context, match.group(1)))

    def replace(item: re.Match[str]) -> str:
        resolved = _path_get(context, item.group(1))
        if isinstance(resolved, (dict, list)):
            return json.dumps(resolved, ensure_ascii=False)
        return "" if resolved is None else str(resolved)

    return _VARIABLE.sub(replace, value)


def _condition_matches(condition: Any, context: dict) -> bool:
    if condition in (None, "", {}):
        return True
    if isinstance(condition, bool):
        return condition
    if isinstance(condition, str):
        value = _path_get(context, condition)
        return bool(value)
    if not isinstance(condition, dict):
        return False
    left = _path_get(context, str(condition.get("field") or condition.get("path") or ""))
    operator = str(condition.get("operator") or "equals")
    right = _resolve(condition.get("value"), context)
    if operator in {"equals", "=="}:
        return left == right
    if operator in {"not_equals", "!="}:
        return left != right
    if operator == "exists":
        return left is not None
    if operator == "contains":
        return str(right) in str(left)
    if operator == "in":
        return left in right if isinstance(right, (list, tuple, set, dict)) else False
    try:
        a, b = float(left), float(right)
    except (TypeError, ValueError):
        return False
    return {
        "gt": a > b, ">": a > b,
        "gte": a >= b, ">=": a >= b,
        "lt": a < b, "<": a < b,
        "lte": a <= b, "<=": a <= b,
    }.get(operator, False)


def _record_event(run: dict, event: str, **detail: Any) -> None:
    _db().put(
        "workflow_events",
        {
            "id": _db().new_id("wfevt"), "workspace_id": run["workspace_id"],
            "run_id": run["id"], "workflow_id": run["workflow_id"], "event": event,
            "detail": detail,
        },
        workspace_id=run["workspace_id"],
    )


def start_workflow(workflow: dict, payload: dict, *, idempotency_key: str | None = None) -> dict:
    validation = validate_definition(workflow.get("definition", {}))
    if not validation["valid"]:
        raise ValueError("；".join(validation["errors"]))
    workspace_id = workflow.get("workspace_id", "default")
    normalized = validation["definition"]
    run_id = _db().new_id("run")
    if idempotency_key:
        digest = hashlib.sha256(
            f"{workspace_id}\0{workflow.get('id')}\0{idempotency_key}".encode("utf-8"),
        ).hexdigest()[:24]
        run_id = f"run_{digest}"
    run_payload = {
            "id": run_id, "workspace_id": workspace_id,
            "workflow_id": workflow["id"], "workflow_version": workflow.get("version", 1),
            "workflow_version_id": workflow.get("current_version_id"),
            "definition_snapshot": normalized, "status": "queued", "inputs": payload,
            "input_schema_snapshot": deepcopy(workflow.get("input_schema", {})),
            "output_schema_snapshot": deepcopy(workflow.get("output_schema", {})),
            "outputs": {}, "step_states": {
                step_id: {"status": "pending", "attempts": 0} for step_id in validation["order"]
            },
            "order": validation["order"], "idempotency_key": idempotency_key,
            "pause_requested": False, "cancel_requested": False,
        }
    if idempotency_key:
        run, created = _db().put_if_absent("workflow_runs", run_payload, workspace_id=workspace_id)
        if not created:
            if run.get("workflow_id") != workflow.get("id") or run.get("inputs") != payload:
                raise ValueError("幂等键已被不同的工作流请求占用")
            return run
    else:
        run = _db().put("workflow_runs", run_payload, workspace_id=workspace_id)
    _record_event(run, "workflow_created")
    dispatch_hooks(
        "workflow.started", {**run, "hook_depth": int(payload.get("hook_depth", 0))},
        workspace_id, database=_db(),
    )
    app = current_app._get_current_object()

    def work(progress, cancel):
        with app.app_context():
            try:
                return execute_run(run["id"], progress, cancel)
            except Exception as exc:
                failed = _db().patch(
                    "workflow_runs", run["id"],
                    {"status": "failed", "error": str(exc), "finished_at": utcnow()},
                )
                if failed:
                    _record_event(failed, "workflow_failed", error=str(exc))
                dispatch_hooks("workflow.failed", failed or {"error": str(exc)}, workspace_id, database=_db())
                raise

    job = get_job_manager(app).submit(
        workspace_id=workspace_id, session_id=payload.get("session_id"), kind="workflow",
        title=f"运行：{workflow.get('name', workflow['id'])}", work=work,
    )
    _db().patch("workflow_runs", run["id"], {"job_id": job["id"]})
    return _db().get("workflow_runs", run["id"])


def _should_execute(step: dict, context: dict) -> bool:
    if not _condition_matches(step.get("when"), context):
        return False
    edges = [edge for edge in step.get("incoming_edges", []) if edge.get("type") != "retry_loop"]
    if not edges:
        return True
    unconditional = [edge for edge in edges if edge.get("type", "auto") in {"auto", "approval"}]
    conditional = [edge for edge in edges if edge.get("type") == "conditional"]
    return bool(unconditional) or any(_condition_matches(edge.get("condition"), context) for edge in conditional)


def _execute_agent_step(step: dict, config: dict, context: dict, workspace_id: str) -> dict:
    profile = _db().get("agent_profiles", str(config.get("agent_profile_id") or step.get("agent_profile_id") or ""))
    prompt = str(config.get("prompt") or "")
    shared = json.dumps({"inputs": context.get("inputs"), "steps": context.get("steps")}, ensure_ascii=False, default=str)
    from .teams import delegate_once

    member = {
        **(profile or {}),
        "name": (profile or {}).get("name") or step.get("name") or step["id"],
        "role": (profile or {}).get("role") or ("证据复核" if step["type"] == "verifier" else "数据分析"),
        "instructions": (profile or {}).get("instructions") or "只依据输入和工具证据回答。",
        "provider_id": config.get("provider_id") or (profile or {}).get("provider_id"),
        "tools": config.get("allowed_tools") or (profile or {}).get("tools") or ["query", "analysis", "knowledge"],
    }
    source_ids = config.get("source_ids") or context.get("inputs", {}).get("source_ids") or []
    if isinstance(source_ids, str):
        source_ids = [source_ids]
    delegated = delegate_once(
        team=None, member=member, prompt=prompt, description=shared[:24000],
        workspace_id=workspace_id, source_ids=[str(value) for value in source_ids],
        session_id=str(context.get("inputs", {}).get("session_id") or f"workflow-{step['id']}"),
    )
    result = delegated["result"]
    return {
        "content": result.get("content", ""), "mode": result.get("mode", "model"),
        "verified": step["type"] == "verifier" and delegated["review"].get("status") == "passed",
        "tool_evidence": result.get("tool_evidence", []), "usage": result.get("usage", {}),
        "review": delegated["review"],
    }


def _execute_step(step: dict, config: dict, context: dict, workspace_id: str) -> dict:
    step_type = step["type"]
    if step_type == "query":
        source_ids = config.get("source_ids") or context["inputs"].get("source_ids")
        return execute_query([str(item) for item in source_ids or []], str(config["sql"]), workspace_id)
    if step_type == "analysis":
        result_id = config.get("result_id") or context.get("result_id")
        if result_id:
            result = _db().get("query_results", str(result_id))
            if not result or result.get("workspace_id", "default") != workspace_id:
                raise ValueError("分析步骤的查询结果不存在或不属于当前工作空间")
            frame = load_result_frame(str(result_id))
        else:
            source_id = config.get("source_id") or context["inputs"].get("source_id")
            source = _db().get("sources", str(source_id or ""))
            if not source or source.get("workspace_id", "default") != workspace_id:
                raise ValueError("分析步骤的数据源不存在或不属于当前工作空间")
            _, frame = source_table(source, config.get("table"))
        return run_analysis(frame, str(config["method"]), config.get("params", {}))
    if step_type == "export_data":
        return export_data({**config, "result_id": config.get("result_id") or context.get("result_id")}, workspace_id)
    if step_type == "export_report":
        return export_report({**config, "result_id": config.get("result_id") or context.get("result_id")}, workspace_id)
    if step_type == "notification":
        connector_id = str(config.get("connector_id") or "")
        if not connector_id:
            return {"delivered": False, "message": config["message"], "reason": "未绑定通知连接器"}
        from ..api.integration import _send_connector

        connector = _db().get("connectors", connector_id)
        if not connector or connector.get("workspace_id", "default") != workspace_id:
            raise ValueError("通知连接器不存在或不属于当前工作空间")
        return _send_connector(connector, str(config["message"]), context)
    if step_type == "validation":
        checks = config.get("rules") or ([config["expression"]] if config.get("expression") else [])
        results = [{"rule": rule, "valid": _condition_matches(rule, context)} for rule in checks]
        valid = all(item["valid"] for item in results)
        if not valid and config.get("fail_on_invalid", True):
            raise ValueError("工作流校验未通过")
        return {"valid": valid, "checks": results}
    if step_type == "router":
        selected = []
        for route in config.get("routes", []):
            if _condition_matches(route.get("condition"), context):
                selected.append(route.get("target"))
                if not config.get("multiple"):
                    break
        return {"selected": selected, "matched": bool(selected)}
    if step_type in {"agent", "verifier"}:
        return _execute_agent_step(step, config, context, workspace_id)
    return {"approved": True}


def _update_context(context: dict, step_id: str, output: dict) -> None:
    context["steps"][step_id] = output
    identifier = str(output.get("id") or "")
    if identifier.startswith("qry_"):
        context["result_id"] = identifier
    elif identifier.startswith("art_"):
        context["artifact_id"] = identifier
    if "method" in output and "result" in output:
        context["analysis"] = output


def _execute_run_sequential(run_id: str, progress, cancel) -> dict:
    run = _db().get("workflow_runs", run_id)
    if not run:
        raise FileNotFoundError("工作流运行不存在")
    definition = normalize_definition(run.get("definition_snapshot") or {})
    by_id = {step["id"]: step for step in definition["steps"]}
    context: dict[str, Any] = {"inputs": deepcopy(run.get("inputs", {})), "steps": {}}
    context.update(run.get("inputs", {}))
    for step_id, output in run.get("outputs", {}).items():
        if isinstance(output, dict):
            _update_context(context, step_id, output)
    run.update({"status": "running", "pause_requested": False})
    run.setdefault("started_at", utcnow())
    _db().put("workflow_runs", run, workspace_id=run["workspace_id"])
    _record_event(run, "workflow_running")
    for index, step_id in enumerate(run["order"]):
        latest = _db().get("workflow_runs", run_id) or run
        if cancel.is_set() or latest.get("cancel_requested"):
            run["status"] = "cancelled"
            break
        if latest.get("pause_requested"):
            run.update({"status": "paused", "paused_at": utcnow(), "current_step_id": step_id})
            break
        step = by_id[step_id]
        state = run["step_states"][step_id]
        if state.get("status") in {"completed", "skipped"}:
            continue
        dependency_states = [run["step_states"][item].get("status") for item in step.get("depends_on", [])]
        if step.get("join_policy", "all_success") == "all_success" and any(
            status in {"failed", "rejected", "cancelled"} for status in dependency_states
        ):
            state.update({"status": "skipped", "reason": "上游步骤未成功", "finished_at": utcnow()})
            continue
        if not _should_execute(step, context):
            state.update({"status": "skipped", "reason": "条件未满足", "finished_at": utcnow()})
            _record_event(run, "workflow_step_skipped", step_id=step_id, reason=state["reason"])
            _db().put("workflow_runs", run, workspace_id=run["workspace_id"])
            continue
        if step["type"] == "approval" and not state.get("approved"):
            state.update({"status": "waiting_approval", "requested_at": utcnow()})
            run.update({"status": "waiting_approval", "current_step_id": step_id})
            _db().put("workflow_runs", run, workspace_id=run["workspace_id"])
            _record_event(run, "workflow_approval_requested", step_id=step_id)
            dispatch_hooks(
                "workflow.waiting_approval", {**run, "step_id": step_id, "step": step},
                run["workspace_id"], database=_db(),
            )
            return {"run_id": run_id, "status": "waiting_approval", "step_id": step_id}
        state.update({"status": "running", "started_at": utcnow()})
        run["current_step_id"] = step_id
        _db().put("workflow_runs", run, workspace_id=run["workspace_id"])
        _record_event(run, "workflow_step_started", step_id=step_id)
        dispatch_hooks(
            "workflow.step_started", {**run, "step_id": step_id, "step": step},
            run["workspace_id"], database=_db(),
        )
        progress(index / max(1, len(run["order"])) * 100, f"执行步骤：{step.get('name', step_id)}")
        config = _resolve(
            {**step.get("config", {}), **context.get("overrides", {}).get(step_id, {})}, context,
        )
        retry = step.get("retry") if isinstance(step.get("retry"), dict) else {}
        max_attempts = max(1, min(int(retry.get("max_attempts", config.pop("max_attempts", 1))), 10))
        output = None
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            state["attempts"] = int(state.get("attempts", 0)) + 1
            try:
                output = _execute_step(step, config, context, run["workspace_id"])
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                state.update({"last_error": str(exc), "last_failed_at": utcnow()})
                _db().put("workflow_runs", run, workspace_id=run["workspace_id"])
                _record_event(run, "workflow_step_retry", step_id=step_id, attempt=attempt + 1, error=str(exc))
                if attempt + 1 < max_attempts:
                    time.sleep(min(float(retry.get("delay_seconds", 0)), 2.0))
        if last_error is not None or output is None:
            state.update({"status": "failed", "error": str(last_error), "finished_at": utcnow()})
            _db().put("workflow_runs", run, workspace_id=run["workspace_id"])
            if step.get("on_error") in {"continue", "close_branch"}:
                continue
            raise last_error or RuntimeError("工作流步骤没有产生输出")
        state.update({"status": "completed", "finished_at": utcnow(), "output": output})
        run["outputs"][step_id] = output
        _update_context(context, step_id, output)
        _db().put("workflow_runs", run, workspace_id=run["workspace_id"])
        _record_event(run, "workflow_step_completed", step_id=step_id, output=output)
        dispatch_hooks(
            "workflow.step_completed", {**run, "step_id": step_id, "step": step, "step_output": output},
            run["workspace_id"], database=_db(),
        )
    if run["status"] == "running":
        run["status"] = "completed"
    if run["status"] in {"completed", "cancelled"}:
        run["finished_at"] = utcnow()
    _db().put("workflow_runs", run, workspace_id=run["workspace_id"])
    _record_event(run, f"workflow_{run['status']}")
    if run["status"] in {"completed", "cancelled"}:
        dispatch_hooks(f"workflow.{run['status']}", run, run["workspace_id"], database=_db())
    return {"run_id": run_id, "status": run["status"], "outputs": run["outputs"]}


def _profile_key(step: dict) -> str:
    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    return str(config.get("agent_profile_id") or step.get("agent_profile_id") or step["id"])


def _record_consumptions(run: dict, step: dict, node_run_id: str) -> None:
    manifests = _db().list("workflow_manifests", workspace_id=run["workspace_id"], limit=5000)
    dependencies = set(step.get("depends_on") or [])
    for manifest in manifests:
        if manifest.get("run_id") != run["id"] or manifest.get("node_id") not in dependencies:
            continue
        for item in manifest.get("items") or []:
            _db().put(
                "workflow_consumptions",
                {
                    "id": _db().new_id("consume"), "workspace_id": run["workspace_id"],
                    "run_id": run["id"], "consumer_node_id": step["id"],
                    "consumer_node_run_id": node_run_id, "producer_node_id": manifest.get("node_id"),
                    "artifact_id": item.get("artifact_id"), "logical_name": item.get("logical_name"),
                },
                workspace_id=run["workspace_id"],
            )


def _record_manifest(run: dict, step: dict, node_run_id: str, output: dict) -> dict:
    declared = [str(value) for value in step.get("output_contract") or [] if str(value)]
    if declared:
        values = {name: output.get(name) for name in declared if name in output}
        if not values and len(declared) == 1:
            values = {declared[0]: output}
    else:
        values = {"result": output}
    items = []
    for logical_name, content in values.items():
        serialized = json.dumps(content, ensure_ascii=False, default=str, sort_keys=True)
        artifact = _db().put(
            "workflow_artifacts",
            {
                "id": _db().new_id("wfart"), "workspace_id": run["workspace_id"],
                "run_id": run["id"], "node_id": step["id"], "node_run_id": node_run_id,
                "logical_name": logical_name, "content": deepcopy(content),
                "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                "size_bytes": len(serialized.encode("utf-8")), "immutable": True,
            },
            workspace_id=run["workspace_id"],
        )
        items.append({
            "artifact_id": artifact["id"], "logical_name": logical_name,
            "uri": f"workflow://{run['id']}/{step['id']}/{logical_name}",
            "sha256": artifact["sha256"], "size_bytes": artifact["size_bytes"],
            "evidence": output.get("tool_evidence", []) if isinstance(output, dict) else [],
            "quality": output.get("review", {}) if isinstance(output, dict) else {},
        })
    return _db().put(
        "workflow_manifests",
        {
            "id": _db().new_id("manifest"), "workspace_id": run["workspace_id"],
            "run_id": run["id"], "node_id": step["id"], "node_run_id": node_run_id,
            "items": items,
        },
        workspace_id=run["workspace_id"],
    )


def _execute_step_with_retry(
    app, step: dict, config: dict, context: dict, workspace_id: str,
) -> tuple[dict | None, Exception | None, int]:
    retry = step.get("retry") if isinstance(step.get("retry"), dict) else {}
    config = deepcopy(config)
    max_attempts = max(1, min(int(retry.get("max_attempts", config.pop("max_attempts", 1))), 10))
    last_error: Exception | None = None
    with app.app_context():
        for attempt in range(1, max_attempts + 1):
            try:
                return _execute_step(step, config, context, workspace_id), None, attempt
            except Exception as exc:
                last_error = exc
                if attempt < max_attempts:
                    time.sleep(min(max(0.0, float(retry.get("delay_seconds", 0))), 2.0))
    return None, last_error, max_attempts


def execute_run(run_id: str, progress, cancel) -> dict:
    """Execute all currently-ready DAG nodes, with bounded parallel independent branches."""
    run = _db().get("workflow_runs", run_id)
    if not run:
        raise FileNotFoundError("工作流运行不存在")
    definition = normalize_definition(run.get("definition_snapshot") or {})
    by_id = {step["id"]: step for step in definition["steps"]}
    context: dict[str, Any] = {"inputs": deepcopy(run.get("inputs", {})), "steps": {}}
    context.update(run.get("inputs", {}))
    for step_id, output in run.get("outputs", {}).items():
        if isinstance(output, dict):
            _update_context(context, step_id, output)
    run.update({"status": "running", "pause_requested": False})
    run.setdefault("started_at", utcnow())
    _db().put("workflow_runs", run, workspace_id=run["workspace_id"])
    _record_event(run, "workflow_running")
    app = current_app._get_current_object()
    total = max(1, len(run["order"]))

    while True:
        latest = _db().get("workflow_runs", run_id) or run
        run["pause_requested"] = latest.get("pause_requested", False)
        run["cancel_requested"] = latest.get("cancel_requested", False)
        if cancel.is_set() or run.get("cancel_requested"):
            run["status"] = "cancelled"
            break
        pending_ids = [
            step_id for step_id in run["order"]
            if run["step_states"][step_id].get("status") not in {
                "completed", "skipped", "failed", "rejected", "cancelled", "waiting_approval",
            }
        ]
        if not pending_ids:
            if any(state.get("status") == "waiting_approval" for state in run["step_states"].values()):
                run["status"] = "waiting_approval"
            break
        if run.get("pause_requested"):
            run.update({"status": "paused", "paused_at": utcnow(), "current_step_id": pending_ids[0]})
            break

        ready: list[dict] = []
        made_progress = False
        for step_id in pending_ids:
            step = by_id[step_id]
            state = run["step_states"][step_id]
            dependency_states = [run["step_states"][item].get("status") for item in step.get("depends_on", [])]
            if any(status in {"pending", "running", "waiting_approval"} for status in dependency_states):
                continue
            if step.get("join_policy", "all_success") == "all_success" and any(
                status in {"failed", "rejected", "cancelled", "skipped"} for status in dependency_states
            ):
                state.update({"status": "skipped", "reason": "上游步骤未成功", "finished_at": utcnow()})
                _record_event(run, "workflow_step_skipped", step_id=step_id, reason=state["reason"])
                made_progress = True
                continue
            if not _should_execute(step, context):
                state.update({"status": "skipped", "reason": "条件未满足", "finished_at": utcnow()})
                _record_event(run, "workflow_step_skipped", step_id=step_id, reason=state["reason"])
                made_progress = True
                continue
            if step["type"] == "approval" and not state.get("approved"):
                state.update({"status": "waiting_approval", "requested_at": utcnow()})
                run["current_step_id"] = step_id
                approval = _db().put(
                    "workflow_approvals",
                    {
                        "id": _db().new_id("approval"), "workspace_id": run["workspace_id"],
                        "run_id": run_id, "node_id": step_id, "status": "pending",
                        "requested_at": utcnow(), "decision": None,
                    },
                    workspace_id=run["workspace_id"],
                )
                state["approval_id"] = approval["id"]
                _record_event(run, "workflow_approval_requested", step_id=step_id, approval_id=approval["id"])
                dispatch_hooks(
                    "workflow.waiting_approval", {**run, "step_id": step_id, "step": step},
                    run["workspace_id"], database=_db(),
                )
                made_progress = True
                continue
            ready.append(step)

        if made_progress:
            _db().put("workflow_runs", run, workspace_id=run["workspace_id"])
        if not ready:
            if made_progress:
                continue
            if any(state.get("status") == "waiting_approval" for state in run["step_states"].values()):
                run["status"] = "waiting_approval"
                break
            run.update({"status": "failed", "error": "工作流无可调度节点，依赖状态不一致"})
            break

        selected: list[tuple[dict, str, str, dict]] = []
        for step in ready:
            profile = _profile_key(step)
            if not WORKFLOW_LIMITER.acquire(run["workspace_id"], run_id, profile, step["id"]):
                continue
            state = run["step_states"][step["id"]]
            state.update({"status": "running", "started_at": utcnow()})
            run["current_step_id"] = step["id"]
            config = _resolve(
                {**step.get("config", {}), **context.get("overrides", {}).get(step["id"], {})}, context,
            )
            node_run = _db().put(
                "workflow_node_runs",
                {
                    "id": _db().new_id("noderun"), "workspace_id": run["workspace_id"],
                    "run_id": run_id, "workflow_id": run["workflow_id"], "node_id": step["id"],
                    "node_type": step["type"], "agent_profile_id": profile, "status": "running",
                    "attempt": int(state.get("attempts", 0)) + 1, "iteration": 1,
                    "started_at": state["started_at"],
                },
                workspace_id=run["workspace_id"],
            )
            _record_consumptions(run, step, node_run["id"])
            selected.append((step, profile, node_run["id"], config))
            _record_event(run, "workflow_step_started", step_id=step["id"], node_run_id=node_run["id"])
            dispatch_hooks(
                "workflow.step_started", {**run, "step_id": step["id"], "step": step},
                run["workspace_id"], database=_db(),
            )
            if len(selected) >= WORKFLOW_LIMITER.limits["run"]:
                break
        _db().put("workflow_runs", run, workspace_id=run["workspace_id"])
        if not selected:
            time.sleep(0.02)
            continue

        finished_count = sum(
            state.get("status") in {"completed", "skipped", "failed", "rejected", "cancelled"}
            for state in run["step_states"].values()
        )
        progress(finished_count / total * 100, "并行执行：" + "、".join(item[0].get("name", item[0]["id"]) for item in selected))
        fatal_error: Exception | None = None
        with ThreadPoolExecutor(max_workers=len(selected), thread_name_prefix="workflow-node") as pool:
            futures = {
                pool.submit(
                    _execute_step_with_retry, app, step, config, deepcopy(context), run["workspace_id"],
                ): (step, profile, node_run_id)
                for step, profile, node_run_id, config in selected
            }
            for future in as_completed(futures):
                step, profile, node_run_id = futures[future]
                try:
                    output, error, attempts = future.result()
                except Exception as exc:
                    output, error, attempts = None, exc, 1
                finally:
                    WORKFLOW_LIMITER.release(run["workspace_id"], run_id, profile, step["id"])
                state = run["step_states"][step["id"]]
                state["attempts"] = int(state.get("attempts", 0)) + attempts
                if error is not None or output is None:
                    state.update({"status": "failed", "error": str(error), "finished_at": utcnow()})
                    _db().patch("workflow_node_runs", node_run_id, {
                        "status": "failed", "error": str(error), "attempts": attempts, "finished_at": utcnow(),
                    })
                    _record_event(run, "workflow_step_failed", step_id=step["id"], error=str(error))
                    if step.get("on_error") not in {"continue", "close_branch"}:
                        fatal_error = error or RuntimeError("工作流步骤没有产生输出")
                    continue
                state.update({"status": "completed", "finished_at": utcnow(), "output": output})
                run["outputs"][step["id"]] = output
                _update_context(context, step["id"], output)
                manifest = _record_manifest(run, step, node_run_id, output)
                usage = output.get("usage", {}) if isinstance(output, dict) else {}
                _db().patch("workflow_node_runs", node_run_id, {
                    "status": "completed", "output": output, "output_manifest_id": manifest["id"],
                    "attempts": attempts, "finished_at": utcnow(),
                    "input_tokens": int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
                    "output_tokens": int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0),
                })
                _record_event(
                    run, "workflow_step_completed", step_id=step["id"],
                    node_run_id=node_run_id, manifest_id=manifest["id"],
                )
                dispatch_hooks(
                    "workflow.step_completed", {**run, "step_id": step["id"], "step": step, "step_output": output},
                    run["workspace_id"], database=_db(),
                )
        _db().put("workflow_runs", run, workspace_id=run["workspace_id"])
        if fatal_error is not None:
            raise fatal_error

    if run["status"] == "running":
        failed = any(state.get("status") in {"failed", "rejected"} for state in run["step_states"].values())
        run["status"] = "failed" if failed else "completed"
    if run["status"] in {"completed", "cancelled", "failed"}:
        run["finished_at"] = utcnow()
    _db().put("workflow_runs", run, workspace_id=run["workspace_id"])
    _record_event(run, f"workflow_{run['status']}")
    if run["status"] in {"completed", "cancelled", "failed"}:
        dispatch_hooks(f"workflow.{run['status']}", run, run["workspace_id"], database=_db())
    return {"run_id": run_id, "status": run["status"], "outputs": run["outputs"]}


def reset_run_steps(run: dict, step_ids: list[str] | None = None) -> dict:
    definition = normalize_definition(run.get("definition_snapshot") or {})
    dependents: dict[str, set[str]] = defaultdict(set)
    for step in definition.get("steps", []):
        for dependency in step.get("depends_on", []):
            dependents[str(dependency)].add(str(step["id"]))
    selected = set(step_ids or [
        step_id for step_id, state in run.get("step_states", {}).items() if state.get("status") == "failed"
    ])
    changed = True
    while changed:
        before = len(selected)
        selected.update(target for source in list(selected) for target in dependents.get(source, set()))
        changed = len(selected) != before
    for step_id in selected:
        if step_id in run.get("step_states", {}):
            run["step_states"][step_id] = {"status": "pending", "attempts": 0}
            run.get("outputs", {}).pop(step_id, None)
    run.update({
        "status": "queued", "pause_requested": False, "cancel_requested": False,
        "error": None, "finished_at": None, "retry_of": run.get("retry_of") or run["id"],
    })
    return run


def run_detail(database: Database, run: dict) -> dict[str, Any]:
    wid, run_id = run["workspace_id"], run["id"]

    def related(collection: str) -> list[dict]:
        return sorted(
            [item for item in database.list(collection, workspace_id=wid, limit=5000) if item.get("run_id") == run_id],
            key=lambda item: (item.get("created_at", ""), item.get("id", "")),
        )

    nodes = related("workflow_node_runs")
    manifests = related("workflow_manifests")
    consumptions = related("workflow_consumptions")
    approvals = related("workflow_approvals")
    templates = related("workflow_run_templates")
    candidates = related("workflow_knowledge_candidates")
    events = related("workflow_events")
    definition = normalize_definition(run.get("definition_snapshot") or {})
    flat_outputs: dict[str, Any] = {}
    lineage = []
    for step in definition.get("steps", []):
        output = run.get("outputs", {}).get(step["id"])
        if not isinstance(output, dict):
            continue
        names = [str(value) for value in step.get("output_contract") or [] if str(value)]
        for name in names:
            if name in output:
                flat_outputs[name] = output[name]
    for manifest in manifests:
        for item in manifest.get("items") or []:
            lineage.append({
                "output": item.get("logical_name"), "producer_node_id": manifest.get("node_id"),
                "producer_node_run_id": manifest.get("node_run_id"), "artifact_id": item.get("artifact_id"),
                "uri": item.get("uri"), "evidence": item.get("evidence", []),
                "quality": item.get("quality", {}),
            })
    return {
        "run": run, "graph": definition,
        "output_schema": run.get("output_schema_snapshot", {}),
        "outputs": flat_outputs or deepcopy(run.get("outputs", {})), "raw_outputs": run.get("outputs", {}),
        "lineage": lineage, "nodes": nodes, "manifests": manifests,
        "consumptions": consumptions, "approvals": approvals, "templates": templates,
        "knowledge_candidates": candidates, "events": events,
    }


def _checkpoint_ancestors(definition: dict, node_id: str) -> set[str]:
    steps = normalize_definition(definition).get("steps", [])
    incoming = {str(step["id"]): set(str(value) for value in step.get("depends_on", [])) for step in steps}
    required, pending = {node_id}, [node_id]
    while pending:
        current = pending.pop()
        for dependency in incoming.get(current, set()):
            if dependency not in required:
                required.add(dependency)
                pending.append(dependency)
    return required


def fork_run(database: Database, source_run: dict, checkpoint_node_run_id: str) -> dict:
    if source_run.get("status") not in {"completed", "failed", "cancelled"}:
        raise ValueError("只有终态工作流可以从检查点分叉")
    checkpoint = database.get("workflow_node_runs", checkpoint_node_run_id)
    if (
        not checkpoint or checkpoint.get("workspace_id") != source_run["workspace_id"]
        or checkpoint.get("run_id") != source_run["id"]
    ):
        raise FileNotFoundError("工作流节点运行不存在")
    if checkpoint.get("status") != "completed":
        raise ValueError("只有成功节点可作为分叉检查点")
    required = _checkpoint_ancestors(source_run.get("definition_snapshot") or {}, str(checkpoint["node_id"]))
    reusable_runs = {}
    for item in database.list("workflow_node_runs", workspace_id=source_run["workspace_id"], limit=5000):
        if item.get("run_id") == source_run["id"] and item.get("node_id") in required and item.get("status") == "completed":
            reusable_runs[item["node_id"]] = item
    if missing := sorted(required - set(reusable_runs)):
        raise ValueError("检查点缺少成功的上游依赖：" + "、".join(missing))
    branch = deepcopy(source_run)
    branch.update({
        "id": database.new_id("run"), "status": "queued", "outputs": {},
        "step_states": {
            step_id: ({"status": "completed", "attempts": 0, "reused": True} if step_id in required
                      else {"status": "pending", "attempts": 0})
            for step_id in source_run["order"]
        },
        "forked_from_run_id": source_run["id"], "checkpoint_node_run_id": checkpoint_node_run_id,
        "pause_requested": False, "cancel_requested": False, "error": None,
        "started_at": None, "finished_at": None, "job_id": None,
    })
    for step_id in required:
        branch["outputs"][step_id] = deepcopy(source_run.get("outputs", {}).get(step_id, {}))
    branch = database.put("workflow_runs", branch, workspace_id=source_run["workspace_id"])
    source_manifests = [
        item for item in database.list("workflow_manifests", workspace_id=source_run["workspace_id"], limit=5000)
        if item.get("run_id") == source_run["id"]
    ]
    for step_id, original_node in reusable_runs.items():
        node = database.put(
            "workflow_node_runs",
            {
                **deepcopy(original_node), "id": database.new_id("noderun"), "run_id": branch["id"],
                "status": "completed", "reused_from_node_run_id": original_node["id"],
            },
            workspace_id=source_run["workspace_id"],
        )
        manifest = next((item for item in reversed(source_manifests) if item.get("node_id") == step_id), None)
        if manifest:
            database.put(
                "workflow_manifests",
                {
                    **deepcopy(manifest), "id": database.new_id("manifest"), "run_id": branch["id"],
                    "node_run_id": node["id"], "reused_from_manifest_id": manifest["id"],
                },
                workspace_id=source_run["workspace_id"],
            )
    _record_event(branch, "workflow_run_checkpoint_forked", source_run_id=source_run["id"],
                  checkpoint_node_run_id=checkpoint_node_run_id, reused_node_ids=sorted(required))
    return branch


def create_run_template(database: Database, run: dict, *, name: str = "", description: str = "", created_by: str = "") -> dict:
    if run.get("status") != "completed":
        raise ValueError("只有成功工作流运行可以标记为模板")
    existing = next(
        (item for item in database.list("workflow_run_templates", workspace_id=run["workspace_id"], limit=5000)
         if item.get("run_id") == run["id"]),
        None,
    )
    if existing:
        return existing
    manifests = [
        item for item in database.list("workflow_manifests", workspace_id=run["workspace_id"], limit=5000)
        if item.get("run_id") == run["id"]
    ]
    return database.put(
        "workflow_run_templates",
        {
            "id": database.new_id("wft"), "workspace_id": run["workspace_id"], "run_id": run["id"],
            "workflow_id": run["workflow_id"], "workflow_version_id": run.get("workflow_version_id"),
            "name": str(name or f"工作流 {run['workflow_id']} 成功模板")[:240],
            "description": str(description or "")[:2000],
            "source_manifest_id": manifests[-1]["id"] if manifests else "", "created_by": str(created_by or "")[:200],
        },
        workspace_id=run["workspace_id"],
    )


def generate_knowledge_candidates(database: Database, run: dict) -> list[dict]:
    if run.get("status") != "completed":
        raise ValueError("只有成功工作流运行可生成知识候选")
    existing = [
        item for item in database.list("workflow_knowledge_candidates", workspace_id=run["workspace_id"], limit=5000)
        if item.get("run_id") == run["id"]
    ]
    if existing:
        return existing
    candidates = []
    report_tokens = ("report", "报告", "summary", "总结", "conclusion", "结论", "template", "模板")
    workflow = database.get("workflows", run["workflow_id"]) or {}
    workflow_name = workflow.get("name") or "Workflow"
    for node_id, output in run.get("outputs", {}).items():
        if not isinstance(output, dict):
            continue
        for key, value in output.items():
            if any(token in str(key).lower() for token in report_tokens):
                candidates.append({
                    "candidate_type": "report_template", "title": f"{workflow_name} · {key}",
                    "payload": {
                        "topic": f"{workflow_name} · {key}",
                        "content": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str),
                        "tags": ["workflow", "report-template"], "producer_node_id": node_id,
                    },
                })
        sql = str(output.get("sql") or "").strip()
        if sql:
            candidates.append({
                "candidate_type": "metric_sql", "title": f"{workflow_name} · {node_id}",
                "payload": {
                    "name": f"{workflow_name} · {node_id}", "definition": "来自成功工作流的可复用 SQL",
                    "sql_template": sql, "notes": f"来源 Workflow Run {run['id']}",
                },
            })
    for candidate in candidates:
        database.put(
            "workflow_knowledge_candidates",
            {
                "id": database.new_id("wkc"), "workspace_id": run["workspace_id"],
                "run_id": run["id"], "workflow_version_id": run.get("workflow_version_id"),
                "status": "pending", **candidate,
            },
            workspace_id=run["workspace_id"],
        )
    return [
        item for item in database.list("workflow_knowledge_candidates", workspace_id=run["workspace_id"], limit=5000)
        if item.get("run_id") == run["id"]
    ]
