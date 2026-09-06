from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd

from ...core.database import Database


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _query_result(database: Database, workspace_id: str, refs: list[str]) -> tuple[dict | None, pd.DataFrame]:
    candidates: list[str] = []
    for ref_id in refs:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM dataset_refs WHERE id=? AND workspace_id=?", (ref_id, workspace_id),
            ).fetchone()
        if row:
            import json

            payload = json.loads(row["payload"])
            result_id = (payload.get("location") or {}).get("query_result_id")
            if result_id:
                candidates.append(str(result_id))
        candidates.append(str(ref_id))
    for result_id in candidates:
        result = database.get("query_results", result_id, workspace_id=workspace_id)
        if not result or result.get("completeness") != "complete":
            continue
        path = Path(str(result.get("path") or ""))
        if path.is_file() and path.stat().st_size <= 50 * 1024 * 1024:
            return result, pd.read_csv(path)
        return result, pd.DataFrame(result.get("data") or [])
    return None, pd.DataFrame()


def _kpis(frame: pd.DataFrame) -> list[dict[str, Any]]:
    numeric = list(frame.select_dtypes(include="number").columns)
    items: list[dict[str, Any]] = []
    if len(frame) == 1 and numeric:
        for column in numeric[:4]:
            items.append({"id": f"kpi_{len(items) + 1}", "label": str(column), "value": _json_value(frame.iloc[0][column]), "aggregation": "query_value"})
    elif numeric:
        column = numeric[0]
        stats = [
            (f"{column} 总和", frame[column].sum(), "sum"),
            (f"{column} 平均", frame[column].mean(), "mean"),
            (f"{column} 最大", frame[column].max(), "max"),
            (f"{column} 最小", frame[column].min(), "min"),
        ]
        items = [{"id": f"kpi_{index}", "label": str(label), "value": _json_value(value), "aggregation": aggregation} for index, (label, value, aggregation) in enumerate(stats, 1)]
    while len(items) < 4:
        items.append({
            "id": f"kpi_{len(items) + 1}", "label": "暂无可用指标", "value": None,
            "aggregation": "not_applicable", "unavailable_reason": "已验证结果没有可映射的数值列",
        })
    return items[:4]


def _series(frame: pd.DataFrame) -> tuple[str | None, list[str], list[Any]]:
    numeric = [str(value) for value in frame.select_dtypes(include="number").columns]
    categorical = [str(value) for value in frame.columns if str(value) not in numeric]
    category = categorical[0] if categorical else (str(frame.columns[0]) if len(frame.columns) else None)
    labels = [_json_value(value) for value in frame[category].head(20).tolist()] if category else []
    return category, numeric, labels


def _charts(frame: pd.DataFrame) -> list[dict[str, Any]]:
    category, numeric, labels = _series(frame)
    shown = frame.head(20)

    def values(column: str | None) -> list[Any]:
        return [_json_value(value) for value in shown[column].tolist()] if column and column in shown else []

    first = numeric[0] if numeric else None
    second = numeric[1] if len(numeric) > 1 else first
    charts = [
        {
            "id": "chart_combo", "title": "数值与趋势", "type": "bar_line",
            "option": {"xAxis": {"type": "category", "data": labels}, "yAxis": [{"type": "value"}, {"type": "value"}], "series": [
                {"name": first or "N/A", "type": "bar", "data": values(first)},
                {"name": second or "N/A", "type": "line", "yAxisIndex": 1, "data": values(second)},
            ]},
        },
        {
            "id": "chart_pie", "title": "构成", "type": "pie",
            "option": {"series": [{"type": "pie", "data": [
                {"name": str(label), "value": value}
                for label, value in zip(labels, values(first)) if value is not None
            ]}]},
        },
        {
            "id": "chart_stacked", "title": "分类堆叠对比", "type": "stacked_bar",
            "option": {"xAxis": {"type": "category", "data": labels}, "yAxis": {"type": "value"}, "series": [
                {"name": first or "N/A", "type": "bar", "stack": "total", "data": values(first)},
                {"name": second or "N/A", "type": "bar", "stack": "total", "data": values(second)},
            ]},
        },
        {
            "id": "chart_bar", "title": "分类对比", "type": "bar",
            "option": {"xAxis": {"type": "category", "data": labels}, "yAxis": {"type": "value"}, "series": [
                {"name": first or "N/A", "type": "bar", "data": values(first)},
            ]},
        },
    ]
    for chart in charts:
        chart["available"] = bool(category and first and labels)
        if not chart["available"]:
            chart["unavailable_reason"] = "已验证结果缺少可绘图的分类列或数值列"
    return charts


def build_manifest_payload(
    database: Database,
    *,
    workspace_id: str,
    contract: dict[str, Any],
    answer: str,
    evidence_refs: list[str],
    validation: dict[str, Any],
    dependency_fingerprint: dict[str, Any],
) -> dict[str, Any]:
    result, frame = _query_result(database, workspace_id, evidence_refs)
    limitations = [item["reason"] for item in validation["issues"]]
    if result is None:
        limitations.append("未找到可本地渲染的已验证有界结果；需先在仓内生成小型精确聚合")
    return {
        "contract": contract, "summary": answer, "evidence_refs": evidence_refs,
        "claims": [], "kpis": _kpis(frame), "charts": _charts(frame),
        "tables": [{
            "id": "detail", "title": "分析明细", "result_id": result.get("id") if result else None,
            "columns": [str(value) for value in frame.columns], "total_rows": result.get("rows") if result else None,
            "completeness": result.get("completeness") if result else "unknown", "server_paginated": True,
        }],
        "report": {
            "problem_and_definitions": contract,
            "data_results": answer,
            "attribution": [{"type": "fact", "text": answer, "evidence_refs": evidence_refs}],
            "recommendations": {"short_term": [], "medium_term": [], "long_term": []},
            "limitations": limitations,
        },
        "code": [], "environment": {},
        "validation": {
            "status": validation["status"], "quality_score": validation["quality_score"],
            "coverage": validation["coverage"], "scoring_note": validation["scoring_note"],
        },
        "limitations": limitations, "dependency_fingerprint": dependency_fingerprint,
    }
