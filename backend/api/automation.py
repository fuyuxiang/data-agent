from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, current_app, request

from ..core.database import utcnow
from ..services.jobs import get_job_manager
from ..services.hooks import SUPPORTED_EVENTS, dispatch_hooks as run_hooks, normalize_event_name
from ..services.security import validate_outbound_url
from ..services.scheduler import validate_cron
from ..services.teams import retry_team_run, start_team_run, team_run_to_workflow
from ..services.workflows import (
    STEP_TYPES,
    create_run_template,
    execute_run,
    fork_run,
    generate_knowledge_candidates,
    reset_run_steps,
    run_detail,
    start_workflow,
    validate_definition,
)
from .common import (
    api_errors, body, current_user_id, db, ok, require_workspace_access,
    require_workspace_record, workspace_id,
)


bp = Blueprint("automation", __name__)


def _valid_hook_event(event: str) -> bool:
    return bool(event) and len(event) <= 100 and all(character.isalnum() or character in "._" for character in event)


def _validate_hook_action(action: dict, wid: str) -> None:
    action_type = str(action.get("type") or "")
    if action_type in {"http", "webhook"} and action.get("url") and "{{" not in str(action["url"]):
        validate_outbound_url(str(action["url"]))
    if action_type == "workflow":
        require_workspace_record("workflows", str(action.get("workflow_id") or ""), wid)
    if action_type == "connector":
        require_workspace_record("connectors", str(action.get("connector_id") or ""), wid)


def _validate_workflow_references(definition: dict, wid: str) -> None:
    nodes = definition.get("nodes") or definition.get("steps") or []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        for source_id in config.get("source_ids") or []:
            require_workspace_record("sources", str(source_id), wid)
        references = {
            "result_id": "query_results", "connector_id": "connectors",
            "agent_profile_id": "agent_profiles",
        }
        for key, collection in references.items():
            if config.get(key):
                require_workspace_record(collection, str(config[key]), wid)
        provider_id = config.get("provider_id")
        if provider_id and provider_id != "environment-default":
            require_workspace_record("providers", str(provider_id), wid)


@bp.get("/api/jobs")
def list_jobs():
    items = db().list("jobs", workspace_id=workspace_id(), limit=int(request.args.get("limit", "200")))
    if request.args.get("active") == "true":
        items = [item for item in items if item.get("status") in {"queued", "running", "waiting_approval"}]
    return ok(items=items)


@bp.get("/api/jobs/events")
def job_events():
    allowed = {item["id"] for item in db().list("jobs", workspace_id=workspace_id(), limit=5000)}
    items = db().job_events(int(request.args.get("after", "0")), int(request.args.get("limit", "500")))
    return ok(items=[item for item in items if item.get("job_id") in allowed])


@bp.get("/api/jobs/<job_id>")
@api_errors
def get_job(job_id: str):
    return ok(item=require_workspace_record("jobs", job_id))


@bp.post("/api/jobs/<job_id>/cancel")
@api_errors
def cancel_job(job_id: str):
    require_workspace_record("jobs", job_id)
    accepted = get_job_manager(current_app._get_current_object()).cancel(job_id)
    return ok(accepted=accepted)


@bp.delete("/api/jobs/completed")
def clear_jobs():
    count = 0
    for item in db().list("jobs", workspace_id=workspace_id()):
        if item.get("status") in {"completed", "failed", "cancelled"} and db().archive("jobs", item["id"]):
            count += 1
    return ok(archived=count)


@bp.get("/api/automation/step-types")
def step_types():
    return ok(items=[{"id": key, **value} for key, value in STEP_TYPES.items()])


@bp.get("/api/agent-profiles")
def agent_profiles():
    defaults = [
        {"id": "data-specialist", "name": "数据工程顾问", "role": "负责结构识别、查询与质量校验", "built_in": True},
        {"id": "quant-specialist", "name": "量化分析顾问", "role": "负责统计检验、建模与不确定性", "built_in": True},
        {"id": "business-specialist", "name": "经营策略顾问", "role": "负责业务解释、风险和行动建议", "built_in": True},
        {"id": "review-specialist", "name": "证据复核顾问", "role": "负责口径、证据链和交付审查", "built_in": True},
    ]
    return ok(items=defaults + db().list("agent_profiles", workspace_id=workspace_id()))


@bp.post("/api/agent-profiles")
@api_errors
def create_agent_profile():
    payload = body()
    if not payload.get("name") or not payload.get("role"):
        raise ValueError("顾问名称与职责不能为空")
    wid = workspace_id()
    if payload.get("provider_id") and payload["provider_id"] != "environment-default":
        require_workspace_record("providers", str(payload["provider_id"]), wid)
    item = db().put(
        "agent_profiles",
        {
            "id": db().new_id("profile"), "workspace_id": wid,
            "name": str(payload["name"])[:100], "role": str(payload["role"])[:1000],
            "instructions": str(payload.get("instructions") or "")[:8000], "provider_id": payload.get("provider_id"),
            "tools": payload.get("tools", ["query", "analysis", "knowledge"]), "enabled": True,
        },
        workspace_id=wid,
    )
    return ok(item=item), 201


@bp.get("/api/workflows")
def list_workflows():
    return ok(items=db().list("workflows", workspace_id=workspace_id()))


@bp.post("/api/workflows")
@api_errors
def create_workflow():
    payload = body()
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("工作流名称不能为空")
    definition = payload.get("definition") or {"steps": []}
    wid = workspace_id()
    _validate_workflow_references(definition, wid)
    item = db().put(
        "workflows",
        {
            "id": db().new_id("flow"), "workspace_id": wid, "name": name[:120],
            "description": str(payload.get("description") or "")[:1000], "definition": definition,
            "input_schema": payload.get("input_schema", {}), "output_schema": payload.get("output_schema", {}),
            "status": "draft", "version": 0, "draft_revision": 1,
            "published_definition": None, "current_version_id": None,
        },
        workspace_id=wid,
    )
    return ok(item=item), 201


@bp.get("/api/workflows/<workflow_id>")
@api_errors
def get_workflow(workflow_id: str):
    return ok(item=require_workspace_record("workflows", workflow_id))


@bp.put("/api/workflows/<workflow_id>/draft")
@api_errors
def update_workflow(workflow_id: str):
    workflow = require_workspace_record("workflows", workflow_id)
    payload = body()
    expected = payload.get("expected_revision")
    if expected is not None and int(expected) != int(workflow.get("draft_revision", 1)):
        raise ValueError("工作流草稿版本冲突，请刷新后重试")
    changes = {
        key: payload[key]
        for key in ("name", "description", "definition", "input_schema", "output_schema")
        if key in payload
    }
    if "definition" in changes:
        _validate_workflow_references(changes["definition"], workflow["workspace_id"])
    changes["status"] = "draft"
    changes["draft_revision"] = int(workflow.get("draft_revision", 1)) + 1
    return ok(item=db().patch("workflows", workflow_id, changes))


@bp.post("/api/workflows/<workflow_id>/validate")
@api_errors
def validate_workflow(workflow_id: str):
    workflow = require_workspace_record("workflows", workflow_id)
    return ok(validation=validate_definition(workflow.get("definition", {})))


@bp.post("/api/workflows/<workflow_id>/publish")
@api_errors
def publish_workflow(workflow_id: str):
    workflow = require_workspace_record("workflows", workflow_id)
    validation = validate_definition(workflow.get("definition", {}))
    if not validation["valid"]:
        raise ValueError("；".join(validation["errors"]))
    normalized = validation["definition"]
    _validate_workflow_references(normalized, workflow["workspace_id"])
    canonical = json.dumps(
        {
            "definition": normalized,
            "input_schema": workflow.get("input_schema", {}),
            "output_schema": workflow.get("output_schema", {}),
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    graph_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    versions = [
        item for item in db().list("workflow_versions", workspace_id=workflow["workspace_id"], limit=5000)
        if item.get("workflow_id") == workflow_id
    ]
    existing = next((item for item in versions if item.get("graph_hash") == graph_hash), None)
    reused = existing is not None
    if existing:
        version_record = existing
    else:
        number = max([int(item.get("version", 0)) for item in versions] or [0]) + 1
        version_record = db().put(
            "workflow_versions",
            {
                "id": db().new_id("wfver"), "workspace_id": workflow["workspace_id"],
                "workflow_id": workflow_id, "version": number, "graph_hash": graph_hash,
                "definition": deepcopy(normalized), "input_schema": deepcopy(workflow.get("input_schema", {})),
                "output_schema": deepcopy(workflow.get("output_schema", {})), "published_at": utcnow(),
            },
            workspace_id=workflow["workspace_id"],
        )
    item = db().patch(
        "workflows", workflow_id,
        {
            "status": "published", "version": version_record["version"],
            "current_version_id": version_record["id"],
            "published_definition": deepcopy(version_record["definition"]), "published_at": utcnow(),
        },
    )
    return ok(item=item, version=version_record, reused=reused, validation=validation)


@bp.get("/api/workflows/<workflow_id>/versions")
@api_errors
def workflow_versions(workflow_id: str):
    workflow = require_workspace_record("workflows", workflow_id)
    items = [
        item for item in db().list("workflow_versions", workspace_id=workflow["workspace_id"], limit=5000)
        if item.get("workflow_id") == workflow_id
    ]
    return ok(items=sorted(items, key=lambda item: int(item.get("version", 0)), reverse=True))


@bp.delete("/api/workflows/<workflow_id>")
@api_errors
def archive_workflow(workflow_id: str):
    require_workspace_record("workflows", workflow_id)
    if not db().archive("workflows", workflow_id):
        raise FileNotFoundError("工作流不存在")
    return ok(archived=True)


@bp.post("/api/workflows/<workflow_id>/runs")
@api_errors
def run_workflow(workflow_id: str):
    workflow = require_workspace_record("workflows", workflow_id)
    payload = body()
    if workflow.get("status") != "published" and not payload.get("allow_draft"):
        raise ValueError("请先发布工作流")
    executable = {**workflow, "definition": workflow.get("published_definition") or workflow["definition"]}
    return ok(run=start_workflow(
        executable, payload.get("inputs") or {}, idempotency_key=payload.get("idempotency_key"),
    )), 202


@bp.get("/api/workflow-runs")
def workflow_runs():
    return ok(items=db().list("workflow_runs", workspace_id=workspace_id()))


@bp.get("/api/workflow-runs/<run_id>")
@api_errors
def workflow_run(run_id: str):
    run = require_workspace_record("workflow_runs", run_id)
    detail = run_detail(db(), run)
    return ok(item=run, **detail)


def _resume_run(run: dict):
    app = current_app._get_current_object()

    def work(progress, cancel):
        with app.app_context():
            try:
                return execute_run(run["id"], progress, cancel)
            except Exception as exc:
                db().patch("workflow_runs", run["id"], {"status": "failed", "error": str(exc), "finished_at": utcnow()})
                raise

    job = get_job_manager(app).submit(
        workspace_id=run["workspace_id"], session_id=run.get("inputs", {}).get("session_id"),
        kind="workflow_resume", title=f"继续工作流：{run['id']}", work=work,
    )
    db().patch("workflow_runs", run["id"], {"job_id": job["id"]})
    return job


@bp.post("/api/workflow-runs/<run_id>/pause")
@api_errors
def pause_run(run_id: str):
    run = require_workspace_record("workflow_runs", run_id)
    if run.get("status") not in {"queued", "running", "waiting_approval"}:
        raise ValueError("当前状态不可暂停")
    changes = {"pause_requested": True}
    if run.get("status") == "waiting_approval":
        changes.update({"status": "paused", "paused_at": utcnow()})
    return ok(item=db().patch("workflow_runs", run_id, changes))


@bp.post("/api/workflow-runs/<run_id>/resume")
@api_errors
def resume_run(run_id: str):
    run = require_workspace_record("workflow_runs", run_id)
    if run.get("status") != "paused":
        raise ValueError("只有已暂停的工作流可以继续")
    if any(state.get("status") == "waiting_approval" for state in run.get("step_states", {}).values()):
        raise ValueError("工作流仍在等待审批，请先处理审批")
    run.update({"status": "queued", "pause_requested": False})
    db().put("workflow_runs", run, workspace_id=run["workspace_id"])
    return ok(item=run, job=_resume_run(run)), 202


@bp.post("/api/workflow-runs/<run_id>/retry")
@api_errors
def retry_run(run_id: str):
    run = require_workspace_record("workflow_runs", run_id)
    if run.get("status") != "failed":
        raise ValueError("只有失败的工作流可以重试")
    step_ids = body().get("step_ids")
    if step_ids is not None and not isinstance(step_ids, list):
        raise ValueError("step_ids 必须是数组")
    run = reset_run_steps(run, [str(item) for item in step_ids] if step_ids else None)
    db().put("workflow_runs", run, workspace_id=run["workspace_id"])
    return ok(item=run, job=_resume_run(run)), 202


@bp.post("/api/workflow-runs/<run_id>/approve")
@api_errors
def approve_run(run_id: str):
    run = require_workspace_record("workflow_runs", run_id)
    require_workspace_access(run["workspace_id"], owner=True)
    step_id = str(body().get("step_id") or run.get("current_step_id") or "")
    state = run.get("step_states", {}).get(step_id)
    if not state or state.get("status") != "waiting_approval":
        step_id, state = next(
            ((key, value) for key, value in run.get("step_states", {}).items() if value.get("status") == "waiting_approval"),
            ("", None),
        )
    if not state or state.get("status") != "waiting_approval":
        raise ValueError("当前没有可审批步骤")
    state.update({"approved": True, "status": "pending", "approved_at": utcnow(), "comment": str(body().get("comment") or "")[:1000]})
    if state.get("approval_id"):
        db().patch("workflow_approvals", state["approval_id"], {
            "status": "approved", "decision": "approve", "decided_at": utcnow(),
            "decided_by": current_user_id(), "comment": state["comment"],
        })
    run["status"] = "queued"
    db().put("workflow_runs", run, workspace_id=run["workspace_id"])
    return ok(item=run, job=_resume_run(run)), 202


@bp.post("/api/workflow-runs/<run_id>/reject")
@api_errors
def reject_run(run_id: str):
    run = require_workspace_record("workflow_runs", run_id)
    require_workspace_access(run["workspace_id"], owner=True)
    payload = body()
    step_id = str(payload.get("step_id") or run.get("current_step_id") or "")
    state = run.get("step_states", {}).get(step_id)
    if not state or state.get("status") != "waiting_approval":
        step_id, state = next(
            ((key, value) for key, value in run.get("step_states", {}).items() if value.get("status") == "waiting_approval"),
            ("", None),
        )
    if not state or state.get("status") != "waiting_approval":
        raise ValueError("当前没有可拒绝步骤")
    decision = str(payload.get("decision") or "fail_run")
    approval_id = state.get("approval_id")
    if approval_id:
        db().patch("workflow_approvals", approval_id, {
            "status": "rejected", "decision": decision, "decided_at": utcnow(),
            "decided_by": current_user_id(), "comment": str(payload.get("comment") or "")[:1000],
        })
    if decision in {"retry", "rework", "reject_and_retry"}:
        state.update({
            "status": "pending", "approved": False, "rejected_at": utcnow(),
            "comment": str(payload.get("comment") or "")[:1000],
        })
        run.update({"status": "queued", "pause_requested": False})
        db().put("workflow_runs", run, workspace_id=run["workspace_id"])
        return ok(item=run, job=_resume_run(run)), 202
    if decision == "skip":
        state.update({
            "status": "skipped", "rejected_at": utcnow(),
            "comment": str(payload.get("comment") or "")[:1000],
        })
        run["status"] = "queued"
        db().put("workflow_runs", run, workspace_id=run["workspace_id"])
        return ok(item=run, job=_resume_run(run)), 202
    state.update({
        "status": "rejected", "rejected_at": utcnow(),
        "comment": str(payload.get("comment") or "")[:1000],
    })
    run.update({"status": "failed", "error": f"审批步骤 {step_id} 被拒绝", "finished_at": utcnow()})
    return ok(item=db().put("workflow_runs", run, workspace_id=run["workspace_id"]))


@bp.post("/api/workflow-runs/<run_id>/cancel")
@api_errors
def cancel_run(run_id: str):
    run = require_workspace_record("workflow_runs", run_id)
    accepted = get_job_manager(current_app._get_current_object()).cancel(run.get("job_id", ""))
    item = db().patch(
        "workflow_runs", run_id,
        {"cancel_requested": True, "status": "cancelled", "finished_at": utcnow()},
    )
    return ok(item=item, accepted=accepted)


def _require_session_run(session_id: str, run_id: str) -> tuple[dict, dict]:
    session_record = require_workspace_record("sessions", session_id)
    run = require_workspace_record("workflow_runs", run_id, session_record["workspace_id"])
    run_session = str(run.get("inputs", {}).get("session_id") or "")
    if run_session and run_session != session_id:
        raise FileNotFoundError("工作流运行不存在")
    return session_record, run


@bp.post("/api/session/<session_id>/workflow-runs")
@api_errors
def start_session_workflow_run(session_id: str):
    session_record = require_workspace_record("sessions", session_id)
    payload = body()
    version_id = str(payload.get("workflow_version_id") or "")
    version = require_workspace_record("workflow_versions", version_id, session_record["workspace_id"])
    workflow = require_workspace_record("workflows", str(version.get("workflow_id") or ""), session_record["workspace_id"])
    executable = {
        **workflow, "definition": deepcopy(version["definition"]),
        "version": version["version"], "current_version_id": version["id"],
        "input_schema": version.get("input_schema", {}), "output_schema": version.get("output_schema", {}),
    }
    inputs = deepcopy(payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {})
    inputs["session_id"] = session_id
    run = start_workflow(executable, inputs, idempotency_key=payload.get("idempotency_key"))
    return ok(run=run), 202


@bp.get("/api/session/<session_id>/workflow-runs")
@api_errors
def list_session_workflow_runs(session_id: str):
    session_record = require_workspace_record("sessions", session_id)
    runs = [
        item for item in db().list("workflow_runs", workspace_id=session_record["workspace_id"], limit=5000)
        if str(item.get("inputs", {}).get("session_id") or "") == session_id
    ]
    return ok(runs=runs)


@bp.get("/api/session/<session_id>/workflow-runs/<run_id>")
@api_errors
def get_session_workflow_run(session_id: str, run_id: str):
    _, run = _require_session_run(session_id, run_id)
    return ok(**run_detail(db(), run))


@bp.delete("/api/session/<session_id>/workflow-runs/<run_id>")
@api_errors
def delete_session_workflow_run(session_id: str, run_id: str):
    _, run = _require_session_run(session_id, run_id)
    if run.get("status") not in {"completed", "failed", "cancelled"}:
        raise ValueError("只能删除终态工作流运行")
    collections = (
        "workflow_node_runs", "workflow_manifests", "workflow_consumptions", "workflow_approvals",
        "workflow_artifacts", "workflow_events", "workflow_run_templates", "workflow_knowledge_candidates",
    )
    counts = {}
    for collection in collections:
        count = 0
        for item in db().list(collection, workspace_id=run["workspace_id"], limit=5000):
            if item.get("run_id") == run_id and db().archive(collection, item["id"]):
                count += 1
        counts[collection] = count
    db().archive("workflow_runs", run_id)
    return ok(deleted=True, cascaded=counts)


@bp.get("/api/session/<session_id>/workflow-runs/<run_id>/events")
@api_errors
def session_workflow_events(session_id: str, run_id: str):
    _, run = _require_session_run(session_id, run_id)
    events = run_detail(db(), run)["events"]
    after = max(0, int(request.args.get("after_sequence", "0")))
    return ok(events=[{**item, "sequence": index} for index, item in enumerate(events, 1) if index > after])


@bp.get("/api/session/<session_id>/workflow-runs/<run_id>/artifacts/<artifact_id>")
@api_errors
def workflow_artifact_content(session_id: str, run_id: str, artifact_id: str):
    _, run = _require_session_run(session_id, run_id)
    artifact = require_workspace_record("workflow_artifacts", artifact_id, run["workspace_id"])
    if artifact.get("run_id") != run_id:
        raise FileNotFoundError("工作流 Artifact 不存在")
    return ok(artifact_id=artifact_id, content=artifact.get("content"), sha256=artifact.get("sha256"))


@bp.get("/api/session/<session_id>/workflow-runs/<run_id>/approvals")
@api_errors
def workflow_approvals(session_id: str, run_id: str):
    _, run = _require_session_run(session_id, run_id)
    return ok(approvals=run_detail(db(), run)["approvals"])


@bp.post("/api/session/<session_id>/workflow-runs/<run_id>/approvals/<approval_id>/decide")
@api_errors
def decide_workflow_approval(session_id: str, run_id: str, approval_id: str):
    _, run = _require_session_run(session_id, run_id)
    require_workspace_access(run["workspace_id"], owner=True)
    approval = require_workspace_record("workflow_approvals", approval_id, run["workspace_id"])
    if approval.get("run_id") != run_id:
        raise FileNotFoundError("工作流审批不存在")
    if approval.get("status") != "pending":
        raise ValueError("审批已被处理")
    payload = body()
    decision = str(payload.get("decision") or "")
    if decision not in {"approve", "reject_and_retry", "reject_and_stop"}:
        raise ValueError("decision 必须是 approve、reject_and_retry 或 reject_and_stop")
    step_id = str(approval["node_id"])
    state = run.get("step_states", {}).get(step_id)
    if not state or state.get("status") != "waiting_approval":
        raise ValueError("审批对应节点不再等待审批")
    decided = db().patch("workflow_approvals", approval_id, {
        "status": "approved" if decision == "approve" else "rejected", "decision": decision,
        "decided_by": current_user_id(),
        "comment": str(payload.get("comment") or "")[:2000], "comments": payload.get("comments") or {},
        "revised_outputs": payload.get("revised_outputs") or {}, "revised_summary": str(payload.get("revised_summary") or "")[:8000],
        "decided_at": utcnow(),
    })
    if decision == "reject_and_stop":
        state.update({"status": "rejected", "rejected_at": utcnow()})
        run.update({"status": "failed", "error": f"审批节点 {step_id} 被拒绝", "finished_at": utcnow()})
        db().put("workflow_runs", run, workspace_id=run["workspace_id"])
        return ok(**run_detail(db(), run), approval=decided)
    state.update({
        "status": "pending", "approved": decision == "approve", "approved_at": utcnow(),
        "comment": str(payload.get("comment") or "")[:2000],
    })
    if decision == "reject_and_retry":
        state["approved"] = False
    run.update({"status": "queued", "pause_requested": False})
    db().put("workflow_runs", run, workspace_id=run["workspace_id"])
    job = _resume_run(run)
    return ok(**run_detail(db(), run), approval=decided, job=job), 202


@bp.post("/api/session/<session_id>/workflow-runs/<run_id>/cancel")
@api_errors
def cancel_session_workflow_run(session_id: str, run_id: str):
    _, run = _require_session_run(session_id, run_id)
    accepted = get_job_manager(current_app._get_current_object()).cancel(run.get("job_id", ""))
    item = db().patch("workflow_runs", run_id, {
        "cancel_requested": True, "status": "cancelled", "finished_at": utcnow(),
    })
    return ok(**run_detail(db(), item), accepted=accepted)


@bp.post("/api/session/<session_id>/workflow-runs/<run_id>/resume")
@api_errors
def resume_session_workflow_run(session_id: str, run_id: str):
    _, run = _require_session_run(session_id, run_id)
    if run.get("status") not in {"paused", "waiting_approval"}:
        raise ValueError("当前工作流不可继续")
    if any(state.get("status") == "waiting_approval" for state in run.get("step_states", {}).values()):
        raise ValueError("工作流仍在等待审批")
    run.update({"status": "queued", "pause_requested": False})
    db().put("workflow_runs", run, workspace_id=run["workspace_id"])
    return ok(**run_detail(db(), run), job=_resume_run(run)), 202


@bp.post("/api/session/<session_id>/workflow-runs/<run_id>/nodes/<node_run_id>/retry")
@api_errors
def retry_workflow_node(session_id: str, run_id: str, node_run_id: str):
    _, run = _require_session_run(session_id, run_id)
    node_run = require_workspace_record("workflow_node_runs", node_run_id, run["workspace_id"])
    if node_run.get("run_id") != run_id or node_run.get("status") != "failed":
        raise ValueError("只能重试当前运行中失败的节点")
    run = reset_run_steps(run, [str(node_run["node_id"])])
    db().put("workflow_runs", run, workspace_id=run["workspace_id"])
    return ok(**run_detail(db(), run), job=_resume_run(run)), 202


@bp.post("/api/session/<session_id>/workflow-runs/<run_id>/fork")
@api_errors
def fork_session_workflow_run(session_id: str, run_id: str):
    _, run = _require_session_run(session_id, run_id)
    branch = fork_run(db(), run, str(body().get("checkpoint_node_run_id") or ""))
    return ok(**run_detail(db(), branch), job=_resume_run(branch)), 202


@bp.get("/api/session/<session_id>/workflow-templates")
@api_errors
def workflow_templates(session_id: str):
    session_record = require_workspace_record("sessions", session_id)
    return ok(templates=db().list("workflow_run_templates", workspace_id=session_record["workspace_id"], limit=5000))


@bp.post("/api/session/<session_id>/workflow-runs/<run_id>/template")
@api_errors
def mark_workflow_template(session_id: str, run_id: str):
    _, run = _require_session_run(session_id, run_id)
    payload = body()
    item = create_run_template(
        db(), run, name=str(payload.get("name") or ""), description=str(payload.get("description") or ""),
        created_by=current_user_id(),
    )
    return ok(template=item), 201


@bp.get("/api/session/<session_id>/workflow-knowledge-candidates")
@api_errors
def workflow_knowledge_candidates(session_id: str):
    session_record = require_workspace_record("sessions", session_id)
    run_id, status = str(request.args.get("run_id") or ""), str(request.args.get("status") or "")
    items = db().list("workflow_knowledge_candidates", workspace_id=session_record["workspace_id"], limit=5000)
    if run_id:
        items = [item for item in items if item.get("run_id") == run_id]
    if status:
        items = [item for item in items if item.get("status") == status]
    return ok(candidates=items)


@bp.post("/api/session/<session_id>/workflow-runs/<run_id>/knowledge-candidates")
@api_errors
def create_workflow_knowledge_candidates(session_id: str, run_id: str):
    _, run = _require_session_run(session_id, run_id)
    return ok(candidates=generate_knowledge_candidates(db(), run)), 201


@bp.post("/api/session/<session_id>/workflow-knowledge-candidates/<candidate_id>/decide")
@api_errors
def decide_workflow_knowledge_candidate(session_id: str, candidate_id: str):
    session_record = require_workspace_record("sessions", session_id)
    candidate = require_workspace_record("workflow_knowledge_candidates", candidate_id, session_record["workspace_id"])
    if candidate.get("status") in {"accepted", "rejected"}:
        return ok(candidate=candidate)
    payload, published_ref = body(), {}
    decision = str(body().get("decision") or "")
    if decision not in {"accept", "reject"}:
        raise ValueError("decision 必须是 accept 或 reject")
    if decision == "accept":
        from ..services.knowledge import save_entry

        source = candidate.get("payload") or {}
        if candidate.get("candidate_type") == "metric_sql":
            entry_payload = {
                "type": "metric", "name": source.get("name") or candidate["title"],
                "definition": source.get("definition", ""), "sql_template": source.get("sql_template", ""),
                "notes": source.get("notes", ""), "category_id": payload.get("category_id"),
            }
        elif candidate.get("candidate_type") == "report_template":
            entry_payload = {
                "type": "context_note", "name": source.get("topic") or candidate["title"],
                "topic": source.get("topic") or candidate["title"], "content": source.get("content", ""),
                "tags": source.get("tags", ["workflow"]), "category_id": payload.get("category_id"),
            }
        else:
            raise ValueError("不支持的知识候选类型")
        entry = save_entry(entry_payload, session_record["workspace_id"])
        published_ref = {"kind": entry["type"], "id": entry["id"]}
    candidate = db().patch("workflow_knowledge_candidates", candidate_id, {
        "status": "accepted" if decision == "accept" else "rejected",
        "decision_comment": str(payload.get("comment") or "")[:2000],
        "decided_by": current_user_id(), "decided_at": utcnow(),
        "published_ref": published_ref,
    })
    return ok(candidate=candidate)


def _duration_seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except (TypeError, ValueError):
        return None


def _workflow_metrics(wid: str, version_filter: str = "") -> dict:
    runs = db().list("workflow_runs", workspace_id=wid, limit=5000)
    if version_filter:
        runs = [item for item in runs if item.get("workflow_version_id") == version_filter]
    versions = []
    for version_id in sorted({str(item.get("workflow_version_id") or "") for item in runs}):
        version_runs = [item for item in runs if str(item.get("workflow_version_id") or "") == version_id]
        terminal = [item for item in version_runs if item.get("status") in {"completed", "failed", "cancelled"}]
        successes = [item for item in terminal if item.get("status") == "completed"]
        node_runs = [
            item for item in db().list("workflow_node_runs", workspace_id=wid, limit=5000)
            if item.get("run_id") in {run["id"] for run in version_runs}
        ]
        approvals = [
            item for item in db().list("workflow_approvals", workspace_id=wid, limit=5000)
            if item.get("run_id") in {run["id"] for run in version_runs}
        ]
        candidates = [
            item for item in db().list("workflow_knowledge_candidates", workspace_id=wid, limit=5000)
            if item.get("run_id") in {run["id"] for run in version_runs}
        ]
        node_metrics = []
        for node_id in sorted({str(item.get("node_id") or "") for item in node_runs}):
            rows = [item for item in node_runs if str(item.get("node_id") or "") == node_id]
            failures = sum(item.get("status") == "failed" for item in rows)
            retries = sum(int(item.get("attempts", item.get("attempt", 1)) or 1) > 1 for item in rows)
            durations = [value for value in (_duration_seconds(item.get("started_at"), item.get("finished_at")) for item in rows) if value is not None]
            node_metrics.append({
                "node_id": node_id, "runs": len(rows), "failures": failures,
                "failure_rate": round(failures / len(rows), 4) if rows else 0,
                "retries": retries, "retry_rate": round(retries / len(rows), 4) if rows else 0,
                "avg_duration_seconds": round(sum(durations) / len(durations), 2) if durations else 0,
                "input_tokens": sum(int(item.get("input_tokens") or 0) for item in rows),
                "output_tokens": sum(int(item.get("output_tokens") or 0) for item in rows),
            })
        durations = [value for value in (_duration_seconds(item.get("started_at"), item.get("finished_at")) for item in terminal) if value is not None]
        waits = [value for value in (_duration_seconds(item.get("requested_at"), item.get("decided_at")) for item in approvals) if value is not None]
        workflow = db().get("workflows", version_runs[0]["workflow_id"]) if version_runs else {}
        versions.append({
            "workflow_id": version_runs[0]["workflow_id"] if version_runs else "",
            "workflow_name": (workflow or {}).get("name", version_id), "workflow_version_id": version_id,
            "version_number": version_runs[0].get("workflow_version") if version_runs else None,
            "run_count": len(version_runs), "terminal_run_count": len(terminal), "success_count": len(successes),
            "success_rate": round(len(successes) / len(terminal), 4) if terminal else 0,
            "avg_duration_seconds": round(sum(durations) / len(durations), 2) if durations else 0,
            "approval_count": len(approvals), "pending_approval_count": sum(item.get("status") == "pending" for item in approvals),
            "avg_approval_wait_seconds": round(sum(waits) / len(waits), 2) if waits else 0,
            "approval_rejection_rate": round(sum(item.get("status") == "rejected" for item in approvals) / len(approvals), 4) if approvals else 0,
            "node_run_count": len(node_runs),
            "node_failure_rate": round(sum(item.get("status") == "failed" for item in node_runs) / len(node_runs), 4) if node_runs else 0,
            "node_retry_rate": round(sum(int(item.get("attempts", 1) or 1) > 1 for item in node_runs) / len(node_runs), 4) if node_runs else 0,
            "input_tokens": sum(int(item.get("input_tokens") or 0) for item in node_runs),
            "output_tokens": sum(int(item.get("output_tokens") or 0) for item in node_runs),
            "artifact_count": sum(
                len(item.get("items") or []) for item in db().list("workflow_manifests", workspace_id=wid, limit=5000)
                if item.get("run_id") in {run["id"] for run in version_runs}
            ),
            "knowledge_candidate_count": len(candidates),
            "knowledge_candidate_adoption_rate": round(sum(item.get("status") == "accepted" for item in candidates) / len(candidates), 4) if candidates else 0,
            "nodes": node_metrics, "models": [], "estimated_cost": None,
        })
    total_terminal = sum(item["terminal_run_count"] for item in versions)
    total_success = sum(item["success_count"] for item in versions)
    return {
        "summary": {
            "workflow_version_count": len(versions), "run_count": len(runs), "terminal_run_count": total_terminal,
            "success_rate": round(total_success / total_terminal, 4) if total_terminal else 0,
            "input_tokens": sum(item["input_tokens"] for item in versions),
            "output_tokens": sum(item["output_tokens"] for item in versions), "estimated_cost": None,
        },
        "versions": versions,
    }


def _optimization_suggestions(metrics: dict) -> list[dict]:
    suggestions = []
    for version in metrics["versions"]:
        version_id = version["workflow_version_id"]
        if version["terminal_run_count"] >= 3 and version["success_rate"] < 0.9:
            suggestions.append({
                "id": "wos_" + hashlib.sha256(f"{version_id}:reliability".encode()).hexdigest()[:16],
                "workflow_version_id": version_id, "kind": "reliability", "severity": "high",
                "title": "检查失败节点并调整恢复策略",
                "rationale": f"成功率 {version['success_rate']:.0%}，低于建议门槛 90%。",
                "proposed_change": "复制当前发布版本为草稿，人工检查失败节点、超时和重试上限。",
            })
        for node in version["nodes"]:
            if node["runs"] >= 3 and node["failure_rate"] >= 0.15:
                suggestions.append({
                    "id": "wos_" + hashlib.sha256(f"{version_id}:node:{node['node_id']}".encode()).hexdigest()[:16],
                    "workflow_version_id": version_id, "node_id": node["node_id"], "kind": "node_failure",
                    "severity": "high", "title": f"优化高失败节点：{node['node_id']}",
                    "rationale": f"节点失败率 {node['failure_rate']:.0%}。",
                    "proposed_change": "复制为草稿后检查输入契约、Agent Profile 和重试上限。",
                })
    return suggestions


@bp.get("/api/session/<session_id>/workflow-metrics")
@api_errors
def workflow_metrics(session_id: str):
    session_record = require_workspace_record("sessions", session_id)
    metrics = _workflow_metrics(session_record["workspace_id"], str(request.args.get("workflow_version_id") or ""))
    suggestions = _optimization_suggestions(metrics)
    for item in suggestions:
        db().put("workflow_optimization_suggestions", {**item, "workspace_id": session_record["workspace_id"]}, workspace_id=session_record["workspace_id"])
    return ok(metrics=metrics, suggestions=suggestions)


@bp.post("/api/session/<session_id>/workflow-optimization-suggestions/<suggestion_id>/draft")
@api_errors
def workflow_suggestion_draft(session_id: str, suggestion_id: str):
    session_record = require_workspace_record("sessions", session_id)
    suggestion = require_workspace_record("workflow_optimization_suggestions", suggestion_id, session_record["workspace_id"])
    version = require_workspace_record("workflow_versions", suggestion["workflow_version_id"], session_record["workspace_id"])
    source = require_workspace_record("workflows", version["workflow_id"], session_record["workspace_id"])
    workflow = db().put(
        "workflows",
        {
            "id": db().new_id("flow"), "workspace_id": session_record["workspace_id"],
            "name": f"{source['name']} · 优化草稿", "description": f"{suggestion['rationale']} {suggestion['proposed_change']}",
            "definition": deepcopy(version["definition"]), "input_schema": deepcopy(version.get("input_schema", {})),
            "output_schema": deepcopy(version.get("output_schema", {})), "status": "draft", "version": 0,
            "draft_revision": 1, "published_definition": None, "current_version_id": None,
            "optimization_source": suggestion_id, "created_by": str(body().get("created_by") or "workflow_metrics")[:200],
        },
        workspace_id=session_record["workspace_id"],
    )
    return ok(workflow=workflow), 201


@bp.get("/api/schedules")
def list_schedules():
    return ok(items=db().list("schedules", workspace_id=workspace_id()))


@bp.post("/api/schedules")
@api_errors
def create_schedule():
    payload = body()
    wid = workspace_id()
    workflow = require_workspace_record("workflows", str(payload.get("workflow_id") or ""), wid)
    if workflow.get("status") != "published" or not workflow.get("published_definition"):
        raise ValueError("只能为已发布的工作流创建调度")
    cron = validate_cron(str(payload.get("cron") or ""))
    timezone_name = str(payload.get("timezone") or "Asia/Shanghai")[:60]
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("时区名称无效") from exc
    item = db().put(
        "schedules",
        {
            "id": db().new_id("sched"), "workspace_id": wid, "name": str(payload.get("name") or "定时分析")[:100],
            "workflow_id": payload["workflow_id"], "cron": cron,
            "timezone": timezone_name, "inputs": payload.get("inputs", {}),
            "enabled": bool(payload.get("enabled", True)), "last_run_at": None, "next_run_at": payload.get("next_run_at"),
        },
        workspace_id=wid,
    )
    return ok(item=item), 201


@bp.patch("/api/schedules/<schedule_id>")
@api_errors
def update_schedule(schedule_id: str):
    schedule = require_workspace_record("schedules", schedule_id)
    payload = body()
    allowed = {"name", "workflow_id", "cron", "timezone", "inputs", "enabled"}
    changes = {key: payload[key] for key in allowed if key in payload}
    workflow_id = str(changes.get("workflow_id") or schedule.get("workflow_id") or "")
    workflow = require_workspace_record("workflows", workflow_id, schedule["workspace_id"])
    if workflow.get("status") != "published" or not workflow.get("published_definition"):
        raise ValueError("调度只能关联已发布的工作流")
    if "cron" in changes:
        changes["cron"] = validate_cron(str(changes["cron"]))
    if "timezone" in changes:
        changes["timezone"] = str(changes["timezone"])[:60]
        try:
            ZoneInfo(changes["timezone"])
        except ZoneInfoNotFoundError as exc:
            raise ValueError("时区名称无效") from exc
    if "name" in changes:
        changes["name"] = str(changes["name"])[:100]
    if "enabled" in changes:
        changes["enabled"] = bool(changes["enabled"])
    return ok(item=db().patch("schedules", schedule_id, changes))


@bp.delete("/api/schedules/<schedule_id>")
@api_errors
def delete_schedule(schedule_id: str):
    require_workspace_record("schedules", schedule_id)
    if not db().archive("schedules", schedule_id):
        raise FileNotFoundError("计划不存在")
    return ok(archived=True)


@bp.post("/api/schedules/<schedule_id>/run")
@api_errors
def run_schedule_now(schedule_id: str):
    schedule = require_workspace_record("schedules", schedule_id)
    workflow = require_workspace_record("workflows", schedule["workflow_id"], schedule["workspace_id"])
    if workflow.get("status") != "published" or not workflow.get("published_definition"):
        raise ValueError("调度关联的工作流尚未发布")
    executable = {**workflow, "definition": workflow["published_definition"]}
    run = start_workflow(executable, schedule.get("inputs", {}))
    db().patch("schedules", schedule_id, {"last_run_at": utcnow(), "last_run_id": run["id"]})
    return ok(run=run), 202


@bp.get("/api/hooks")
def list_hooks():
    wid = workspace_id()
    items = db().list("hooks", workspace_id=wid)
    configured = [
        {
            **item, "action_type": (item.get("action") or {}).get("type", ""),
            "event_dispatched": item.get("event") in SUPPORTED_EVENTS,
        }
        for item in items
    ]
    enabled = [item for item in configured if item.get("enabled", True)]
    return ok(
        items=items, settings={"enabled": True, "hooks": configured},
        runtime={
            "enabled": True, "configured_count": len(configured),
            "enabled_count": len(enabled), "runnable_count": len(enabled), "pending_count": 0,
            "active_hooks": enabled, "configured_hooks": configured, "internal_endpoints": [],
            "supported_events": sorted(SUPPORTED_EVENTS),
        },
    )


@bp.post("/api/hooks")
@api_errors
def create_hook():
    payload = body()
    action = payload.get("action")
    if not payload.get("event") or not isinstance(action, dict) or not action.get("type"):
        raise ValueError("Hook 必须声明 event 和 action")
    event = normalize_event_name(str(payload["event"]))
    if not _valid_hook_event(event):
        raise ValueError(f"不支持的 Hook 事件：{event}")
    if str(payload.get("once_scope") or "session") not in {"turn", "session", "global"}:
        raise ValueError("once_scope 只能是 turn、session 或 global")
    wid = workspace_id()
    _validate_hook_action(action, wid)
    item = db().put(
        "hooks",
        {
            "id": db().new_id("hook"), "workspace_id": wid,
            "name": str(payload.get("name") or event)[:100], "event": event,
            "condition": payload.get("condition", payload.get("if", {})), "action": action,
            "enabled": bool(payload.get("enabled", True)), "once": bool(payload.get("once", False)),
            "once_scope": str(payload.get("once_scope") or "session"),
            "reject": bool(payload.get("reject", False)), "run_count": 0, "execution_keys": [],
        },
        workspace_id=wid,
    )
    return ok(item=item), 201


@bp.post("/api/hooks/validate")
@api_errors
def validate_hook():
    payload = body()
    event = normalize_event_name(str(payload.get("event") or ""))
    errors = []
    if not _valid_hook_event(event):
        errors.append(f"不支持的事件：{event}")
    if not isinstance(payload.get("action"), dict) or not payload.get("action", {}).get("type"):
        errors.append("action.type 不能为空")
    elif not errors:
        try:
            _validate_hook_action(payload["action"], workspace_id())
        except (ValueError, FileNotFoundError, PermissionError) as exc:
            errors.append(str(exc))
    return ok(valid=not errors, errors=errors, normalized={**payload, "event": event})


@bp.get("/api/hooks/history")
def hook_history():
    limit = int(request.args.get("limit", "100"))
    return ok(items=db().list("hook_runs", workspace_id=workspace_id(), limit=limit))


@bp.patch("/api/hooks/<hook_id>")
@api_errors
def update_hook(hook_id: str):
    require_workspace_record("hooks", hook_id)
    changes = body()
    if "event" in changes:
        changes["event"] = normalize_event_name(str(changes["event"]))
        if not _valid_hook_event(changes["event"]):
            raise ValueError(f"不支持的 Hook 事件：{changes['event']}")
    if isinstance(changes.get("action"), dict):
        _validate_hook_action(changes["action"], workspace_id())
    return ok(item=db().patch("hooks", hook_id, changes))


@bp.delete("/api/hooks/<hook_id>")
@api_errors
def delete_hook(hook_id: str):
    require_workspace_record("hooks", hook_id)
    if not db().archive("hooks", hook_id):
        raise FileNotFoundError("Hook 不存在")
    return ok(archived=True)


@bp.post("/api/hooks/dispatch")
@api_errors
def dispatch_hooks():
    payload = body()
    event = str(payload.get("event") or "")
    event_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    return ok(matched=run_hooks(event, event_payload, workspace_id()))


@bp.get("/api/teams")
def list_teams():
    return ok(items=db().list("teams", workspace_id=workspace_id()))


@bp.post("/api/teams")
@api_errors
def create_team():
    payload = body()
    members = payload.get("members")
    if not payload.get("name") or not isinstance(members, list) or not members:
        raise ValueError("协作组需要名称和至少一名成员")
    if len(members) > 8:
        raise ValueError("协作组最多包含 8 名成员")
    normalized_members = []
    names = set()
    for index, member in enumerate(members, 1):
        if not isinstance(member, dict):
            raise ValueError("成员配置必须是对象")
        profile_id = str(member.get("profile_id") or "")
        profile = db().get("agent_profiles", profile_id)
        if profile and profile.get("workspace_id", "default") != workspace_id():
            raise ValueError("团队成员配置不属于当前工作空间")
        name = str(member.get("name") or (profile or {}).get("name") or f"成员 {index}")[:100]
        if name in names:
            raise ValueError("团队成员名称不能重复")
        names.add(name)
        normalized_members.append({
            **member, "name": name,
            "role": str(member.get("role") or (profile or {}).get("role") or "分析顾问")[:1000],
            "instructions": str(member.get("instructions") or (profile or {}).get("instructions") or "")[:8000],
            "tools": member.get("tools") or (profile or {}).get("tools") or ["query", "analysis", "knowledge"],
        })
        provider_id = member.get("provider_id") or (profile or {}).get("provider_id")
        if provider_id and provider_id != "environment-default":
            require_workspace_record("providers", str(provider_id))
    wid = workspace_id()
    item = db().put(
        "teams",
        {
            "id": db().new_id("team"), "workspace_id": wid, "name": str(payload["name"])[:100],
            "objective": str(payload.get("objective") or "")[:2000], "members": normalized_members,
            "lead_profile_id": payload.get("lead_profile_id") or normalized_members[0].get("profile_id"),
            "quality_reviewer": {"name": "固定证据复核员", "role": "quality_reviewer"}, "status": "ready",
        },
        workspace_id=wid,
    )
    return ok(item=item), 201


@bp.post("/api/teams/<team_id>/runs")
@api_errors
def run_team(team_id: str):
    team = require_workspace_record("teams", team_id)
    run, job = start_team_run(team, body())
    return ok(run=run, job=job), 202


@bp.get("/api/team-runs")
def list_team_runs():
    return ok(items=db().list("team_runs", workspace_id=workspace_id()))


@bp.get("/api/team-runs/<run_id>")
@api_errors
def get_team_run(run_id: str):
    return ok(item=require_workspace_record("team_runs", run_id))


@bp.post("/api/team-runs/<run_id>/retry")
@api_errors
def retry_team_tasks(run_id: str):
    run = require_workspace_record("team_runs", run_id)
    if run.get("status") not in {"partial_failed", "failed", "needs_review"}:
        raise ValueError("当前团队运行没有可重试任务")
    team = require_workspace_record("teams", run["team_id"])
    task_ids = body().get("task_ids")
    if task_ids is not None and not isinstance(task_ids, list):
        raise ValueError("task_ids 必须是数组")
    reset = retry_team_run(run, [str(item) for item in task_ids] if task_ids else None)
    item, job = start_team_run(team, {}, existing_run=reset)
    return ok(item=item, job=job), 202


@bp.post("/api/team-runs/<run_id>/cancel")
@api_errors
def cancel_team_run(run_id: str):
    run = require_workspace_record("team_runs", run_id)
    accepted = get_job_manager(current_app._get_current_object()).cancel(str(run.get("job_id") or ""))
    item = db().patch("team_runs", run_id, {"status": "cancelled", "finished_at": utcnow()})
    return ok(item=item, accepted=accepted)


@bp.post("/api/team-runs/<run_id>/workflow")
@api_errors
def promote_team_run(run_id: str):
    run = require_workspace_record("team_runs", run_id)
    team = require_workspace_record("teams", run["team_id"])
    return ok(item=team_run_to_workflow(team, run)), 201


@bp.get("/api/teams/<team_id>/messages")
@api_errors
def team_messages(team_id: str):
    team = require_workspace_record("teams", team_id)
    items = [
        item for item in db().list("team_messages", workspace_id=team["workspace_id"], limit=1000)
        if item.get("team_id") == team_id
    ]
    return ok(items=sorted(items, key=lambda item: item.get("created_at", "")))


@bp.post("/api/teams/<team_id>/messages")
@api_errors
def send_team_message(team_id: str):
    team = require_workspace_record("teams", team_id)
    payload = body()
    content = str(payload.get("content") or "").strip()
    if not content:
        raise ValueError("团队消息不能为空")
    recipients = payload.get("recipients") or ["*"]
    if not isinstance(recipients, list):
        raise ValueError("recipients 必须是数组")
    item = db().put(
        "team_messages",
        {
            "id": db().new_id("teammsg"), "workspace_id": team["workspace_id"], "team_id": team_id,
            "sender": str(payload.get("sender") or "leader")[:100],
            "recipients": [str(value)[:100] for value in recipients], "content": content[:8000], "read_by": [],
        },
        workspace_id=team["workspace_id"],
    )
    return ok(item=item), 201


@bp.delete("/api/teams/<team_id>")
@api_errors
def delete_team(team_id: str):
    if body().get("confirm") is not True:
        raise ValueError("删除协作组需要 confirm=true")
    require_workspace_record("teams", team_id)
    if not db().archive("teams", team_id):
        raise FileNotFoundError("协作组不存在")
    return ok(archived=True)


MAP_TEMPLATES = [
    {"id": "operating-model", "name": "经营模型", "blocks": ["目标", "核心指标", "驱动因素", "约束", "行动", "验证"]},
    {"id": "metric-tree", "name": "指标树", "blocks": ["北极星指标", "一级指标", "二级指标", "口径", "责任人", "数据源"]},
    {"id": "analysis-playbook", "name": "分析方法卡", "blocks": ["问题", "假设", "数据", "方法", "证据", "结论", "后续动作"]},
]


@bp.get("/api/decision-maps/templates")
def map_templates():
    return ok(items=MAP_TEMPLATES)


@bp.get("/api/decision-maps")
def decision_maps():
    return ok(items=db().list("decision_maps", workspace_id=workspace_id()))


@bp.post("/api/decision-maps")
@api_errors
def create_map():
    payload = body()
    template = next((item for item in MAP_TEMPLATES if item["id"] == payload.get("template_id")), MAP_TEMPLATES[0])
    wid = workspace_id()
    item = db().put(
        "decision_maps",
        {
            "id": db().new_id("map"), "workspace_id": wid,
            "name": str(payload.get("name") or template["name"])[:100], "description": str(payload.get("description") or "")[:1000],
            "template_id": template["id"], "blocks": {key: "" for key in template["blocks"]},
            "nodes": [], "edges": [], "revision": 1,
        },
        workspace_id=wid,
    )
    return ok(item=item), 201


@bp.get("/api/decision-maps/<map_id>")
@api_errors
def decision_map(map_id: str):
    return ok(item=require_workspace_record("decision_maps", map_id), revisions=db().list(f"map_revisions_{map_id}", workspace_id=workspace_id()))


@bp.patch("/api/decision-maps/<map_id>")
@api_errors
def update_map(map_id: str):
    current = require_workspace_record("decision_maps", map_id)
    db().put(f"map_revisions_{map_id}", {"id": db().new_id("rev"), "map_id": map_id, "snapshot": current, "revision": current.get("revision", 1)}, workspace_id=current["workspace_id"])
    changes = {key: value for key, value in body().items() if key in {"name", "description", "blocks", "nodes", "edges"}}
    changes["revision"] = int(current.get("revision", 1)) + 1
    return ok(item=db().patch("decision_maps", map_id, changes))


@bp.delete("/api/decision-maps/<map_id>")
@api_errors
def delete_map(map_id: str):
    require_workspace_record("decision_maps", map_id)
    if not db().archive("decision_maps", map_id):
        raise FileNotFoundError("决策地图不存在")
    return ok(archived=True)
