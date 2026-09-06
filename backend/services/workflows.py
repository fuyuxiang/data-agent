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

from ..agent.contracts import ModelResponse, ModelToolCall, TaskContract, ToolSpec, ToolStatus
from ..agent.store import RunStore
from ..agent.tools import ToolExecutor, ToolRegistry
from ..core.database import Database, utcnow
from .analytics import run_analysis
from .authorization import require_result_access, require_session_access, require_sources_access
from .datasets import execute_query, load_result_frame, source_table
from .exports import export_data, export_report
from .hooks import dispatch_hooks
from .jobs import get_job_manager, register_job_handler


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
    """Accept both the simple step format and the graph-based workflow contract."""
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


def _workflow_source_ids(definition: dict, payload: dict) -> list[str]:
    values = payload.get("source_ids") or []
    if isinstance(values, str):
        values = [values]
    found = [str(value) for value in values]
    for step in definition.get("steps") or []:
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        config = _resolve(config, {"inputs": payload, **payload})
        configured = config.get("source_ids") or ([config["source_id"]] if config.get("source_id") else [])
        if isinstance(configured, str):
            configured = [configured]
        found.extend(str(value) for value in configured)
    return list(dict.fromkeys(value for value in found if value))


def start_workflow(
    workflow: dict, payload: dict, *, idempotency_key: str | None = None,
    actor_id: str = "local-default",
) -> dict:
    validation = validate_definition(workflow.get("definition", {}))
    if not validation["valid"]:
        raise ValueError("；".join(validation["errors"]))
    workspace_id = workflow.get("workspace_id", "default")
    requested_session_id = str(payload.get("session_id") or "")
    if requested_session_id:
        require_session_access(
            _db(), requested_session_id, workspace_id=workspace_id, actor_id=str(actor_id),
        )
    normalized = validation["definition"]
    run_id = _db().new_id("run")
    if idempotency_key:
        digest = hashlib.sha256(
            f"{workspace_id}\0{workflow.get('id')}\0{idempotency_key}".encode("utf-8"),
        ).hexdigest()[:24]
        run_id = f"run_{digest}"
        existing = _db().get("workflow_runs", run_id, workspace_id=workspace_id)
        if existing:
            if existing.get("workflow_id") != workflow.get("id") or existing.get("inputs") != payload:
                raise ValueError("幂等键已被不同的工作流请求占用")
            return existing
    source_ids = _workflow_source_ids(normalized, payload)
    require_sources_access(
        _db(), source_ids, workspace_id=workspace_id, actor_id=str(actor_id), action="analyze",
    )
    store = RunStore(_db())
    agent_run, _ = store.create_run(
        workspace_id=workspace_id, session_id=str(payload.get("session_id") or f"workflow:{workflow['id']}"),
        actor_id=str(actor_id), source_scope=source_ids,
        allowed_tool_ids=["workflow_step", "validate_result"], run_kind="workflow",
        budget=dict(workflow.get("budget") or RunStore.default_budget()),
        idempotency_key=f"workflow:{workflow['id']}:{idempotency_key}" if idempotency_key else None,
    )
    if agent_run["contract_version"] == 0:
        store.add_contract(agent_run["id"], TaskContract.from_payload({
            "objective": str(workflow.get("name") or "执行已发布分析工作流"),
            "coverage": str(workflow.get("description") or "已发布工作流定义与本次输入"),
            "dimensions": [str(step.get("name") or step["id"]) for step in normalized["steps"]],
            "deliverables": ["workflow_result_manifest"], "source_scope": source_ids,
            "budget": dict(workflow.get("budget") or RunStore.default_budget()),
        }), expected_version=0, confirmed_by=str(actor_id))
        store.add_plan(agent_run["id"], {"tasks": [{
            "id": str(step["id"]), "title": str(step.get("name") or step["id"]),
            "status": "open", "depends_on": [str(value) for value in step.get("depends_on") or []],
        } for step in normalized["steps"]]}, reason="published_workflow_definition", expected_version=0)
    run_payload = {
            "id": run_id, "workspace_id": workspace_id,
            "actor_id": str(actor_id), "agent_run_id": agent_run["id"],
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
    job = get_job_manager(app).submit_spec(
        workspace_id=workspace_id, session_id=payload.get("session_id"), job_type="workflow_run",
        title=f"运行：{workflow.get('name', workflow['id'])}",
        spec={"run_id": run["id"]}, run_id=agent_run["id"],
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
        actor_id=str(context.get("_actor_id") or "local-default"),
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
        return execute_query(
            [str(item) for item in source_ids or []], str(config["sql"]), workspace_id,
            actor_id=str(context.get("_actor_id") or "local-default"),
        )
    if step_type == "analysis":
        result_id = config.get("result_id") or context.get("result_id")
        if result_id:
            require_result_access(
                _db(), _db().get("query_results", str(result_id)), workspace_id=workspace_id,
                actor_id=str(context.get("_actor_id") or "local-default"), action="analyze",
            )
            frame = load_result_frame(str(result_id))
        else:
            source_id = config.get("source_id") or context["inputs"].get("source_id")
            source = require_sources_access(
                _db(), [str(source_id or "")], workspace_id=workspace_id,
                actor_id=str(context.get("_actor_id") or "local-default"), action="analyze",
            )[0]
            _, frame = source_table(source, config.get("table"))
        return run_analysis(frame, str(config["method"]), config.get("params", {}))
    if step_type == "export_data":
        return export_data(
            {**config, "result_id": config.get("result_id") or context.get("result_id")},
            workspace_id, str(context.get("_actor_id") or "local-default"),
        )
    if step_type == "export_report":
        return export_report(
            {**config, "result_id": config.get("result_id") or context.get("result_id")},
            workspace_id, str(context.get("_actor_id") or "local-default"),
        )
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


def _workflow_has_published_evidence(context: dict) -> bool:
    return any(
        isinstance(value, dict) and (
            value.get("validation_status") == "PASS" or value.get("publication_id")
            or (value.get("validation") or {}).get("validation_status") == "PASS"
        )
        for value in (context.get("steps") or {}).values()
    )


def _execute_step_through_boundary(
    step: dict, config: dict, context: dict, workspace_id: str,
    *, agent_run_id: str, call_suffix: str, lease,
) -> dict:
    """Execute one deterministic workflow node through the canonical Action boundary."""
    from .advanced_agent import _dataset_ref, _source_authorized, _validate_result
    from .data_plane.contracts import DatasetRefStore

    database = _db()
    store = RunStore(database)
    formal_run = store.get_run(agent_run_id, workspace_id=workspace_id)
    if not formal_run or not _source_authorized(database, formal_run):
        raise PermissionError("工作流执行期间数据授权已失效")
    registry = ToolRegistry()

    def execute(arguments: dict[str, Any]) -> dict:
        if step["type"] in {"export_data", "export_report", "notification"} and not _workflow_has_published_evidence(context):
            raise PermissionError("导出或通知必须位于已通过验证/发布的结果之后")
        value = _execute_step(step, arguments["config"], context, workspace_id)
        value = value if isinstance(value, dict) else {"value": value}
        if step["type"] == "query" and str(value.get("id") or "").startswith("qry_"):
            ref = _dataset_ref(database, formal_run, value)
            DatasetRefStore(database).put(ref, workspace_id=workspace_id, run_id=agent_run_id)
            value = {
                **value, "result_id": value["id"], "dataset_ref_id": ref.ref_id,
                "output_refs": [ref.ref_id], "completeness": ref.result_completeness,
                "provenance_ref": ref.provenance_ref,
            }
        elif str(value.get("id") or "").startswith(("art_", "export_", "report_")):
            value = {
                **value, "artifact_id": value["id"], "output_refs": [value["id"]],
                "completeness": "complete",
            }
        elif value.get("publication_id"):
            value = {
                **value, "output_refs": [str(value["publication_id"])],
                "completeness": "complete", "validation_status": "PASS",
            }
        return value

    registry.register(ToolSpec(
        id="workflow_step", description="执行已发布工作流中的一个确定性节点。",
        input_schema={
            "type": "object", "properties": {
                "node_id": {"type": "string"}, "node_type": {"type": "string"},
                "config": {"type": "object"},
            }, "required": ["node_id", "node_type", "config"],
        },
        mutability="external" if step["type"] in {"export_data", "export_report", "notification"} else "read",
        cancellable=step["type"] in {"query", "analysis", "agent", "verifier"},
    ), execute)
    registry.register(ToolSpec(
        id="validate_result", description="验证工作流节点的数据完整性、来源范围与结构。",
        input_schema={"type": "object", "properties": {
            "dataset_ref_id": {"type": "string"}, "result_id": {"type": "string"},
        }},
    ), lambda arguments: _validate_result(database, formal_run, arguments))
    executor = ToolExecutor(store, registry)
    arguments = {"node_id": step["id"], "node_type": step["type"], "config": config}
    call_id = f"{step['id']}:{call_suffix}"
    decision = store.record_decision(agent_run_id, ModelResponse(
        protocol="local_deterministic", model="published_workflow", content="",
        tool_calls=(ModelToolCall(call_id, "workflow_step", arguments),),
        finish_reason="tool_calls", refusal=None,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    ))
    executed = executor.execute(
        context=lease, decision_id=decision["id"], call_id=call_id,
        tool_id="workflow_step", arguments=arguments,
    )
    if executed.result.status is not ToolStatus.SUCCEEDED:
        raise RuntimeError(str(executed.value.get("error") or "工作流动作执行失败"))
    output = dict(executed.value)
    if output.get("dataset_ref_id"):
        validation_args = {
            "dataset_ref_id": str(output["dataset_ref_id"]),
            "result_id": str(output.get("result_id") or ""),
        }
        validation_call = f"{call_id}:validate"
        validation_decision = store.record_decision(agent_run_id, ModelResponse(
            protocol="local_deterministic", model="workflow_validator", content="",
            tool_calls=(ModelToolCall(validation_call, "validate_result", validation_args),),
            finish_reason="tool_calls", refusal=None,
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        ))
        checked = executor.execute(
            context=lease, decision_id=validation_decision["id"], call_id=validation_call,
            tool_id="validate_result", arguments=validation_args,
        )
        output["validation"] = checked.value
        output["validation_status"] = checked.value.get("validation_status")
        if checked.result.status is not ToolStatus.SUCCEEDED or output["validation_status"] != "PASS":
            raise RuntimeError("工作流查询结果未通过正式验证")
    return output


def _execute_step_with_retry(
    app, step: dict, config: dict, context: dict, workspace_id: str,
    agent_run_id: str, node_run_id: str, lease,
) -> tuple[dict | None, Exception | None, int]:
    retry = step.get("retry") if isinstance(step.get("retry"), dict) else {}
    config = deepcopy(config)
    max_attempts = max(1, min(int(retry.get("max_attempts", config.pop("max_attempts", 1))), 10))
    last_error: Exception | None = None
    with app.app_context():
        for attempt in range(1, max_attempts + 1):
            try:
                return _execute_step_through_boundary(
                    step, config, context, workspace_id,
                    agent_run_id=agent_run_id, call_suffix=f"{node_run_id}:{attempt}", lease=lease,
                ), None, attempt
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
    context: dict[str, Any] = {
        "inputs": deepcopy(run.get("inputs", {})), "steps": {},
        "_actor_id": str(run.get("actor_id") or "local-default"),
    }
    context.update(run.get("inputs", {}))
    for step_id, output in run.get("outputs", {}).items():
        if isinstance(output, dict):
            _update_context(context, step_id, output)
    run.update({"status": "running", "pause_requested": False})
    run.setdefault("started_at", utcnow())
    _db().put("workflow_runs", run, workspace_id=run["workspace_id"])
    _record_event(run, "workflow_running")
    parent_store = RunStore(_db())
    if not run.get("agent_run_id"):
        raise RuntimeError("工作流缺少统一执行边界对应的 AgentRun")
    parent_store.update_status(run["agent_run_id"], "running")
    lease_owner = f"workflow:{run_id}"
    parent_lease = parent_store.acquire_lease(run["agent_run_id"], lease_owner, ttl_seconds=3600)
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
                    run["agent_run_id"], node_run_id, parent_lease,
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
    if run["status"] == "completed":
        from .advanced_agent import _source_authorized
        from .results.manifests import ResultService

        actions = parent_store.actions(run["agent_run_id"])
        evidence = []
        for action in actions:
            arguments = action.get("arguments") or {}
            node_id = str(arguments.get("node_id") or "")
            if action["status"] != "succeeded" and run.get("step_states", {}).get(node_id, {}).get("status") == "completed":
                continue
            result = action.get("result") or {}
            tool_result = result.get("tool_result") or {}
            has_data_ref = bool(
                tool_result.get("dataset_ref_id") or tool_result.get("result_id")
                or tool_result.get("publication_id")
            )
            refs = (
                list(tool_result.get("output_refs") or [])
                if has_data_ref or action["tool_id"] == "validate_result" else []
            )
            evidence.append({
                "tool": action["tool_id"],
                "status": "SUCCEEDED" if action["status"] == "succeeded" else "FAILED",
                "refs": refs,
                "completeness": tool_result.get("completeness", "unknown"),
                "validation_status": tool_result.get("validation_status", "not_evaluated"),
            })
        # A checkpoint branch reuses immutable upstream artifacts rather than copying
        # old Action rows. Feed only the persisted PASS evidence into the new gate.
        for output in run["outputs"].values():
            if not isinstance(output, dict) or output.get("validation_status") != "PASS":
                continue
            ref = str(output.get("dataset_ref_id") or output.get("result_id") or "")
            evidence.append({
                "tool": "checkpoint_reuse", "status": "SUCCEEDED",
                "refs": [ref] if ref else [],
                "completeness": str(output.get("completeness") or "unknown"),
                "validation_status": "PASS",
            })
        final = ResultService(
            _db(), authorize=lambda current: _source_authorized(_db(), current),
        ).finalize(
            run["agent_run_id"],
            "工作流已完成，数据结果已通过独立验证。",
            evidence,
        )
        run["result_manifest_id"] = final.get("manifest_id")
        run["publication_id"] = final.get("publication_id")
        if final.get("published"):
            parent_store.update_status(
                run["agent_run_id"], "finished", outcome="complete",
                quality_status="passed", stop_reason="workflow_published",
            )
        else:
            run.update({"status": "failed", "error": "工作流结果未通过正式发布门禁"})
            parent_store.update_status(
                run["agent_run_id"], "failed", outcome="partial",
                quality_status=str(final.get("quality_status") or "failed"),
                stop_reason="workflow_publication_blocked",
            )
    elif run["status"] == "cancelled":
        parent_store.update_status(run["agent_run_id"], "cancelled", outcome="cancelled", stop_reason="workflow_cancelled")
    elif run["status"] == "failed":
        parent_store.update_status(run["agent_run_id"], "failed", outcome="failed", stop_reason="workflow_failed")
    elif run["status"] == "paused":
        parent_store.update_status(run["agent_run_id"], "paused", stop_reason="workflow_paused")
    elif run["status"] == "waiting_approval":
        parent_store.update_status(run["agent_run_id"], "waiting_approval", stop_reason="workflow_waiting_approval")
    parent_store.release_lease(run["agent_run_id"], lease_owner, parent_lease.lease_epoch)
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
    source_parent = RunStore(database).get_run(str(source_run.get("agent_run_id") or ""))
    if not source_parent:
        raise ValueError("源工作流缺少统一执行边界，不能安全分叉")
    branch_store = RunStore(database)
    branch_parent, _ = branch_store.create_run(
        workspace_id=source_parent["workspace_id"], session_id=source_parent["session_id"],
        actor_id=source_parent["actor_id"], source_scope=source_parent["source_scope"],
        allowed_tool_ids=source_parent["allowed_tool_ids"], parent_run_id=source_parent["id"],
        run_kind="workflow_branch", budget=source_parent["budget"],
    )
    source_contract = branch_store.latest_contract(source_parent["id"])
    source_plan = branch_store.latest_plan(source_parent["id"])
    if not source_contract or not source_plan:
        raise ValueError("源工作流契约或计划缺失，不能安全分叉")
    branch_store.add_contract(
        branch_parent["id"], TaskContract.from_payload(source_contract["payload"]),
        expected_version=0, confirmed_by=source_parent["actor_id"],
    )
    branch_store.add_plan(
        branch_parent["id"], source_plan["payload"], reason="workflow_checkpoint_branch",
        expected_version=0,
    )
    branch.update({
        "id": database.new_id("run"), "status": "queued", "outputs": {},
        "agent_run_id": branch_parent["id"],
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


def _workflow_job_handler(app, spec, progress, cancel):
    run_id = str(spec.get("run_id") or "")
    run = _db().get("workflow_runs", run_id)
    if not run:
        raise FileNotFoundError("工作流运行不存在")
    try:
        return execute_run(run_id, progress, cancel)
    except Exception as exc:
        failed = _db().patch(
            "workflow_runs", run_id,
            {"status": "failed", "error": str(exc), "finished_at": utcnow()},
            workspace_id=run["workspace_id"],
        )
        if failed:
            _record_event(failed, "workflow_failed", error=str(exc))
        if run.get("agent_run_id"):
            parent_store = RunStore(_db())
            parent = parent_store.get_run(run["agent_run_id"])
            if parent:
                parent_store.update_status(
                    parent["id"], "failed", outcome="failed",
                    quality_status="failed", stop_reason="workflow_failed",
                )
                parent_store.release_lease(
                    parent["id"], f"workflow:{run_id}", int(parent.get("lease_epoch") or 0),
                )
        dispatch_hooks(
            "workflow.failed", failed or {"error": str(exc)},
            run["workspace_id"], database=_db(),
        )
        raise


register_job_handler("workflow_run", _workflow_job_handler)
