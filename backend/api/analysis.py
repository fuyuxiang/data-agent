from __future__ import annotations

import pandas as pd
from flask import Blueprint, current_app

from ..agent.store import RunStore
from ..services.analytics import ANALYSIS_METHODS, run_analysis
from ..services.authorization import require_sources_access
from ..services.charts import catalog as chart_catalog, make_spec
from ..services.datasets import load_result_frame, source_table
from .common import (
    api_errors, body, current_user_id, db, ok, require_query_result_access,
    require_source_access, require_workspace_record, workspace_id, workspace_membership,
)


bp = Blueprint("analysis", __name__)


def _payload_source_ids(payload: dict) -> list[str]:
    if payload.get("result_id"):
        result = require_query_result_access(str(payload["result_id"]), action="analyze")
        return [str(value) for value in result.get("source_ids") or []]
    if payload.get("source_id"):
        source = require_source_access(str(payload["source_id"]), action="analyze")
        return [str(source["id"])]
    return []


def _owned_analysis(record: dict) -> bool:
    actor_id = str(record.get("actor_id") or "")
    if actor_id and actor_id != current_user_id():
        return False
    if not actor_id and record.get("agent_run_id"):
        run = RunStore(db()).get_run(str(record["agent_run_id"]), workspace_id=record["workspace_id"])
        if not run or run.get("actor_id") != current_user_id():
            return False
    if not actor_id and not record.get("agent_run_id"):
        membership = workspace_membership(record["workspace_id"])
        if not membership or membership.get("role") != "owner":
            return False
    try:
        require_sources_access(
            db(), record.get("source_ids") or [], workspace_id=record["workspace_id"],
            actor_id=current_user_id(),
        )
    except (FileNotFoundError, PermissionError):
        return False
    return True


def _chart_access(item: dict, *, action: str = "read") -> dict:
    source_ids = [str(value) for value in item.get("source_ids") or []]
    if not source_ids and item.get("result_id"):
        result = db().get("query_results", str(item["result_id"]), workspace_id=item["workspace_id"]) or {}
        source_ids.extend(str(value) for value in result.get("source_ids") or [])
    if not source_ids and item.get("source_id"):
        source_ids.append(str(item["source_id"]))
    if not source_ids:
        chart_actor = str(item.get("actor_id") or "")
        membership = workspace_membership(item["workspace_id"])
        if (chart_actor and chart_actor != current_user_id()) or (
            not chart_actor and (not membership or membership.get("role") != "owner")
        ):
            raise FileNotFoundError("图表不存在")
    require_sources_access(
        db(), source_ids, workspace_id=item["workspace_id"],
        actor_id=current_user_id(), action=action,
    )
    return item


def _resolve_frame(payload: dict) -> pd.DataFrame:
    if payload.get("result_id"):
        require_query_result_access(str(payload["result_id"]), action="analyze")
        return load_result_frame(str(payload["result_id"]))
    if payload.get("source_id"):
        source = require_source_access(str(payload["source_id"]), action="analyze")
        return source_table(source, payload.get("table"))[1]
    if isinstance(payload.get("rows"), list):
        return pd.DataFrame(payload["rows"])
    raise ValueError("请选择数据源或查询结果")


@bp.get("/api/analysis/methods")
def methods():
    return ok(items=ANALYSIS_METHODS)


@bp.post("/api/analysis/run")
@api_errors
def analyze():
    payload = body()
    method = str(payload.get("method") or "profile")
    source_ids = _payload_source_ids(payload)
    frame = _resolve_frame(payload)
    limits = current_app.config["SETTINGS"]
    if len(frame) > limits.max_analysis_rows:
        raise ValueError(
            f"分析数据超过 {limits.max_analysis_rows} 行上限；请先在数据库侧聚合或筛选",
        )
    cells = int(frame.shape[0] * frame.shape[1])
    if cells > limits.max_analysis_cells:
        raise ValueError(
            f"分析数据超过 {limits.max_analysis_cells} 个单元格上限；请减少行数或字段",
        )
    result = run_analysis(frame, method, payload.get("params") or {})
    record = db().put(
        "analysis_runs",
        {
            "id": db().new_id("ana"),
            "workspace_id": workspace_id(),
            "actor_id": current_user_id(),
            "source_ids": source_ids,
            "method": method,
            "inputs": {key: value for key, value in payload.items() if key != "rows"},
            "result": result["result"],
            "status": "completed",
        },
        workspace_id=workspace_id(),
    )
    db().audit("analysis.method_completed", workspace_id=workspace_id(), object_type="analysis_run", object_id=record["id"], detail={"method": method})
    return ok(run=record)


@bp.get("/api/analysis/runs")
def analysis_runs():
    return ok(items=[
        item for item in db().list("analysis_runs", workspace_id=workspace_id())
        if _owned_analysis(item)
    ])


@bp.get("/api/charts/catalog")
def charts():
    return ok(items=chart_catalog())


@bp.post("/api/charts/spec")
@api_errors
def chart_spec():
    payload = body()
    source_ids = _payload_source_ids(payload)
    frame = _resolve_frame(payload)
    spec = make_spec(
        frame,
        chart_type=payload.get("type"),
        title=str(payload.get("title") or "分析结果"),
        x=payload.get("x"),
        y=payload.get("y"),
        group=payload.get("group"),
        options=payload.get("options"),
    )
    item = db().put(
        "charts",
        {
            "id": db().new_id("chart"), "workspace_id": workspace_id(),
            "actor_id": current_user_id(), "source_ids": source_ids,
            "name": spec["title"], "spec": spec,
            "source_id": payload.get("source_id"), "result_id": payload.get("result_id"),
        },
        workspace_id=workspace_id(),
    )
    return ok(item=item)


@bp.get("/api/charts")
def saved_charts():
    items = []
    for item in db().list("charts", workspace_id=workspace_id()):
        try:
            items.append(_chart_access(item))
        except (FileNotFoundError, PermissionError):
            continue
    return ok(items=items)


@bp.delete("/api/charts/<chart_id>")
@api_errors
def remove_chart(chart_id: str):
    _chart_access(require_workspace_record("charts", chart_id), action="delete")
    if not db().archive("charts", chart_id):
        raise FileNotFoundError("图表不存在")
    return ok(archived=True)
