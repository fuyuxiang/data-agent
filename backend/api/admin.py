"""管理后台的运营视图：运行记录、评测与指标试算。

用户端不展示 Run / Plan / Action / Trace 这些内部对象；它们在这里被翻译成
"谁在什么时候问了什么、用了哪些技能、结果是否可信"。
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, request

from ..agent.store import RunStore
from ..core.database import utcnow
from ..services.authorization import actor_role
from .common import (
    api_errors, body, current_user_id, db, ok, require_workspace_access, workspace_id,
)

bp = Blueprint("admin", __name__)

FEEDBACK_REASONS = (
    ("数据不正确", "数据不正确"),
    ("理解错误", "理解错误"),
    ("结论不合理", "结论不合理"),
    ("图表问题", "图表问题"),
    ("不够深入", "不够深入"),
    ("速度太慢", "速度太慢"),
    ("其他", "其他"),
)


def _require_admin(*, owner: bool = False) -> str:
    wid = workspace_id()
    require_workspace_access(wid, owner=owner)
    if actor_role(db(), wid, current_user_id()) not in {"owner", "editor"}:
        raise PermissionError("该操作仅限工作空间管理员")
    return wid


def _elapsed(run: dict[str, Any]) -> float | None:
    started, finished = run.get("started_at"), run.get("finished_at")
    if not started or not finished:
        return None
    try:
        from datetime import datetime

        return round((datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds(), 2)
    except (TypeError, ValueError):
        return None


@bp.get("/api/admin/runs")
@api_errors
def list_runs():
    wid = _require_admin()
    runs = RunStore(db()).list_runs(wid, limit=min(1000, int(request.args.get("limit", "200"))), include_archived=True)
    session_names = {
        item["id"]: item.get("name") for item in db().list("sessions", workspace_id=wid, limit=5000)
    }
    agent_names = {
        item["id"]: item.get("name")
        for item in db().list("agent_definitions", workspace_id=wid, limit=5000)
    }
    resolutions = {
        str(item.get("run_id")): item
        for item in db().list("skill_resolutions", workspace_id=wid, limit=5000)
    }
    store = RunStore(db())
    rows = []
    for run in runs:
        contract = store.latest_contract(run["id"])
        resolution = resolutions.get(str(run["id"])) or {}
        rows.append({
            "id": run["id"],
            "created_at": run.get("created_at") or "",
            "updated_at": run.get("updated_at") or "",
            "archived_at": run.get("archived_at"),
            "actor_id": run.get("actor_id") or "",
            "session_id": run.get("session_id") or "",
            "session_name": session_names.get(str(run.get("session_id") or "")) or "",
            "question": str((contract or {}).get("payload", {}).get("objective") or ""),
            "agent_name": agent_names.get(str(run.get("agent_id") or "")) or "",
            "skill_ids": [
                item for item in (resolution.get("skill_ids") or ([run["skill_id"]] if run.get("skill_id") else []))
            ],
            "provider_id": run.get("provider_id") or "",
            "execution_status": run.get("execution_status") or "",
            "quality_status": run.get("quality_status") or "",
            "outcome": run.get("outcome") or "",
            "stop_reason": run.get("stop_reason") or "",
            "duration_seconds": _elapsed(run),
            "usage": run.get("usage") or {},
        })
    return ok(items=rows, total=len(rows))


@bp.get("/api/admin/runs/<run_id>")
@api_errors
def run_detail(run_id: str):
    wid = _require_admin()
    store = RunStore(db())
    run = store.get_run(run_id, workspace_id=wid, include_archived=True)
    if not run:
        raise FileNotFoundError("运行记录不存在")
    contract = store.latest_contract(run_id)
    plan = store.latest_plan(run_id)
    actions = store.actions(run_id)
    decisions = store.decisions(run_id)
    context = db().get("analysis_context", run_id, workspace_id=wid) or {}
    resolution = db().get("skill_resolutions", f"skr_{run_id}", workspace_id=wid) or {}
    validation = db().list("validation_results", workspace_id=wid, limit=5000)
    artifacts = [
        item for item in db().list("artifacts", workspace_id=wid, limit=5000)
        if item.get("run_id") == run_id
    ]
    feedback = db().list("analysis_feedback", workspace_id=wid, limit=5000)
    return ok(item={
        "run": {
            "id": run["id"],
            "execution_status": run.get("execution_status"),
            "quality_status": run.get("quality_status"),
            "outcome": run.get("outcome"),
            "stop_reason": run.get("stop_reason"),
            "created_at": run.get("created_at"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "archived_at": run.get("archived_at"),
            "duration_seconds": _elapsed(run),
            "usage": run.get("usage") or {},
            "budget": run.get("budget") or {},
            "source_scope": list(run.get("source_scope") or []),
        },
        "contract": contract,
        "plan": plan,
        "skills": {
            "requested": list(resolution.get("requested_skill_ids") or []),
            "used": list(resolution.get("skill_ids") or []),
            "allowed_tools": list(resolution.get("allowed_tools") or []),
            "warnings": list(resolution.get("warnings") or []),
        },
        "actions": [
            {
                "tool_id": item.get("tool_id"),
                "status": item.get("status"),
                "error_code": item.get("error_code"),
                "created_at": item.get("created_at"),
                "arguments": item.get("arguments") or {},
            }
            for item in actions
        ],
        "decisions": [
            {
                "sequence": item.get("sequence"),
                "model": item.get("model_name"),
                "protocol": item.get("model_protocol"),
                "finish_reason": item.get("finish_reason"),
                "tool_call_count": len(item.get("tool_calls") or []),
                "usage": item.get("usage") or {},
                "created_at": item.get("created_at"),
            }
            for item in decisions
        ],
        "metrics": [item for item in validation if str(item.get("run_id") or "") == run_id],
        "artifacts": [
            {
                "id": item["id"], "kind": item.get("kind"), "title": item.get("title"),
                "filename": item.get("filename"), "size": item.get("size"),
                "download_url": f"/api/artifacts/{item['id']}/download",
            }
            for item in artifacts
        ],
        "feedback": [item for item in feedback if str(item.get("run_id") or "") == run_id],
        "agent_snapshot": context.get("agent_snapshot"),
    })


@bp.get("/api/admin/evaluations")
@api_errors
def evaluations():
    wid = _require_admin()
    feedback = db().list("analysis_feedback", workspace_id=wid, limit=2000)
    reasons = [
        {"key": key, "label": label,
         "count": sum(1 for item in feedback if key in (item.get("reasons") or []))}
        for key, label in FEEDBACK_REASONS
    ]
    positive = sum(1 for item in feedback if item.get("rating") in {"up", "positive", 1, True})
    negative = sum(1 for item in feedback if item.get("rating") in {"down", "negative", -1, False})

    runs = RunStore(db()).list_runs(wid, limit=1000, include_archived=True)
    failures: dict[str, int] = {}
    for run in runs:
        if run.get("execution_status") in {"failed", "cancelled"}:
            reason = str(run.get("stop_reason") or "unknown")
            failures[reason] = failures.get(reason, 0) + 1

    usage_events = db().list("usage_events", workspace_id=wid, limit=5000)
    by_model: dict[str, dict[str, int]] = {}
    for event in usage_events:
        bucket = by_model.setdefault(
            str(event.get("model") or "unknown"),
            {"requests": 0, "total_tokens": 0},
        )
        bucket["requests"] += 1
        bucket["total_tokens"] += int(event.get("total_tokens") or 0)

    return ok(
        totals={
            "runs": len(runs),
            "finished": sum(1 for item in runs if item.get("execution_status") == "finished"),
            "failed": sum(1 for item in runs if item.get("execution_status") == "failed"),
            "published": sum(1 for item in runs if item.get("quality_status") == "passed"),
            "feedback": len(feedback),
            "positive": positive,
            "negative": negative,
            "satisfaction": round(positive / len(feedback), 4) if feedback else None,
        },
        reasons=reasons,
        failures=sorted(
            ({"reason": key, "count": value} for key, value in failures.items()),
            key=lambda item: -item["count"],
        ),
        usage=sorted(
            ({"model": key, **value} for key, value in by_model.items()),
            key=lambda item: -item["total_tokens"],
        ),
        feedback=[
            {
                "id": item["id"], "run_id": item.get("run_id"),
                "rating": item.get("rating"), "reasons": item.get("reasons") or [],
                "comment": item.get("comment") or "", "created_at": item.get("created_at"),
                "actor_id": item.get("actor_id"),
            }
            for item in sorted(feedback, key=lambda entry: str(entry.get("created_at") or ""), reverse=True)[:100]
        ],
        feedback_reasons=[{"key": key, "label": label} for key, label in FEEDBACK_REASONS],
    )


# --------------------------------------------------------------------------- #
# Metric trial — the "试算" capability of the metric centre
# --------------------------------------------------------------------------- #

@bp.post("/api/admin/metric-trial")
@api_errors
def metric_trial():
    """Run a metric with a chosen time range, dimensions and filters.

    This is a *governed* preview, not a free SQL box: the metric compiler still
    decides the SQL, so what an administrator previews here is exactly what the
    Agent will produce later.
    """
    from ..services.semantic import execute_metric_query, visible_metrics

    wid = workspace_id()
    require_workspace_access(wid)
    payload = body()
    metric_ref = str(payload.get("metric") or "")
    if not metric_ref:
        raise ValueError("请选择要试算的指标")
    visible = {str(item.get("name")): item for item in visible_metrics(db(), wid, current_user_id())}
    visible.update({str(item["id"]): item for item in visible.values()})
    if metric_ref not in visible:
        raise ValueError("指标不存在或未在当前业务数据空间发布")
    selected = visible[metric_ref]
    can_preview_draft = actor_role(db(), wid, current_user_id()) in {"owner", "editor"}
    if selected.get("status") != "approved" and not can_preview_draft:
        raise PermissionError("草稿指标仅管理员可试算")

    group_by = [str(value) for value in (payload.get("group_by") or [])][:8]
    filters = list(payload.get("filters") or [])[:20]
    time_range = dict(payload.get("time_range") or {})
    limit = max(1, min(5000, int(payload.get("limit") or 200)))
    output = execute_metric_query(db(), {
        "metric": visible[metric_ref]["id"],
        "group_by": group_by,
        "filters": filters,
        "time_range": time_range,
        "limit": limit,
    }, wid, current_user_id(), allow_draft=can_preview_draft)
    plan, result = output["plan"], output["result"]
    trial = db().put("metric_trials", {
        "id": db().new_id("trial"), "workspace_id": wid,
        "metric_id": visible[metric_ref]["id"], "metric": metric_ref,
        "group_by": group_by, "filters": filters, "time_range": time_range,
        "limit": limit, "rows": int(result.get("rows") or len(result.get("data") or [])),
        "actor_id": current_user_id(),
    }, workspace_id=wid)
    db().audit("metric.trial", workspace_id=wid, actor=current_user_id(),
               object_type="semantic_metric", object_id=str(visible[metric_ref]["id"]),
               detail={"trial_id": trial["id"], "group_by": group_by})
    return ok(plan=_public_plan(plan), result=result, trial_id=trial["id"])


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Strip the compiled definition fingerprints the UI never shows."""
    return {
        "sql": plan.get("sql"),
        "dialect": plan.get("dialect"),
        "group_by": plan.get("group_by") or [],
        "filters": plan.get("filters") or [],
        "time_range": plan.get("time_range") or {},
        "limit": plan.get("limit"),
        "metric": {
            "id": (plan.get("metric") or {}).get("id"),
            "name": (plan.get("metric") or {}).get("name"),
            "label": (plan.get("metric") or {}).get("label"),
            "version": (plan.get("metric") or {}).get("version"),
            "unit": (plan.get("metric") or {}).get("unit"),
        },
        "model": {
            "id": (plan.get("model") or {}).get("id"),
            "name": (plan.get("model") or {}).get("name"),
            "version": (plan.get("model") or {}).get("version"),
        },
    }


@bp.post("/api/admin/evaluations/feedback/<feedback_id>")
@api_errors
def handle_feedback(feedback_id: str):
    wid = _require_admin()
    from .common import require_workspace_record

    require_workspace_record("analysis_feedback", feedback_id, wid)
    status = str(body().get("status") or "").strip()
    if status not in {"pending", "accepted", "rejected"}:
        raise ValueError("处理状态必须是 pending / accepted / rejected")
    updated = db().patch("analysis_feedback", feedback_id, {
        "status": status, "handled_by": current_user_id(), "handled_at": utcnow(),
    }, workspace_id=wid)
    return ok(item=updated)
