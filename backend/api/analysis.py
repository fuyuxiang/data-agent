from __future__ import annotations

import pandas as pd
from flask import Blueprint, current_app

from ..services.analytics import ANALYSIS_METHODS, run_analysis
from ..services.charts import catalog as chart_catalog, make_spec
from ..services.datasets import load_result_frame, source_table
from .common import api_errors, body, db, ok, require_workspace_record, workspace_id


bp = Blueprint("analysis", __name__)


def _resolve_frame(payload: dict) -> pd.DataFrame:
    if payload.get("result_id"):
        require_workspace_record("query_results", str(payload["result_id"]))
        return load_result_frame(str(payload["result_id"]))
    if payload.get("source_id"):
        source = require_workspace_record("sources", str(payload["source_id"]))
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
    return ok(items=db().list("analysis_runs", workspace_id=workspace_id()))


@bp.get("/api/charts/catalog")
def charts():
    return ok(items=chart_catalog())


@bp.post("/api/charts/spec")
@api_errors
def chart_spec():
    payload = body()
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
            "name": spec["title"], "spec": spec,
            "source_id": payload.get("source_id"), "result_id": payload.get("result_id"),
        },
        workspace_id=workspace_id(),
    )
    return ok(item=item)


@bp.get("/api/charts")
def saved_charts():
    return ok(items=db().list("charts", workspace_id=workspace_id()))


@bp.delete("/api/charts/<chart_id>")
@api_errors
def remove_chart(chart_id: str):
    require_workspace_record("charts", chart_id)
    if not db().archive("charts", chart_id):
        raise FileNotFoundError("图表不存在")
    return ok(archived=True)
