from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import pandas as pd

from ...core.database import Database
from ..query_results import read_result_frame


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
            return result, read_result_frame(result)
        return result, pd.DataFrame(result.get("data") or [])
    return None, pd.DataFrame()


def _kpis(frame: pd.DataFrame) -> list[dict[str, Any]]:
    numeric = list(frame.select_dtypes(include="number").columns)
    if len(frame) != 1:
        return []
    return [
        {"id": f"kpi_{index}", "label": str(column),
         "value": _json_value(frame.iloc[0][column]), "aggregation": "query_value"}
        for index, column in enumerate(numeric[:4], 1)
    ]


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

    if not category or not numeric or not labels:
        return []
    first = numeric[0]
    time_dimension = bool(re.search(r"日期|时间|月份|年月|date|time|month|year", category, re.I))
    kind = "line" if time_dimension else "bar"
    charts = [{
        "id": "chart_primary", "title": f"{first}按{category}{'趋势' if time_dimension else '对比'}",
        "type": kind, "available": True,
        "option": {"xAxis": {"type": "category", "data": labels}, "yAxis": {"type": "value"},
                   "series": [{"name": first, "type": kind, "data": values(first)}]},
    }]
    second = numeric[1] if len(numeric) > 1 else None
    if second:
        charts.append({
            "id": "chart_secondary", "title": f"{second}按{category}对比",
            "type": "bar", "available": True,
            "option": {"xAxis": {"type": "category", "data": labels}, "yAxis": {"type": "value"},
                       "series": [{"name": second, "type": "bar", "data": values(second)}]},
        })
    first_values = values(first)
    additive = not re.search(r"率|占比|比例|平均|均值|rate|ratio|percent|avg|mean|%", first, re.I)
    if additive and not time_dimension and 2 <= len(labels) <= 10 and len(set(map(str, labels))) == len(labels) and all(
        isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 for value in first_values
    ) and sum(first_values) > 0:
        charts.append({
            "id": "chart_composition", "title": f"{first}构成", "type": "pie", "available": True,
            "option": {"series": [{"type": "pie", "data": [
                {"name": str(label), "value": value} for label, value in zip(labels, first_values)
            ]}]},
        })
    return charts


def _report_recommendations(answer: str) -> dict[str, list[str]]:
    """Preserve only recommendations explicitly present in the validated answer."""
    result: dict[str, list[str]] = {"short_term": [], "medium_term": [], "long_term": []}
    current = ""
    mapping = {"短期": "short_term", "中期": "medium_term", "长期": "long_term"}
    for raw in answer.splitlines():
        line = raw.strip().lstrip("#").strip()
        heading = re.match(r"^(短期|中期|长期)(?:建议|行动|措施)?\s*[:：]?\s*(.*)$", line)
        if heading:
            current = mapping[heading.group(1)]
            if heading.group(2):
                result[current].append(heading.group(2)[:1000])
            continue
        if current and line.startswith(("- ", "* ", "• ")):
            result[current].append(line[2:][:1000])
        elif line and not line.startswith(("- ", "* ", "• ")):
            current = ""
    return result


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
    limitations.append("自动核验覆盖数据结果和显式数值；业务解释、因果判断与建议需人工复核")
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
            "attribution": [],
            "recommendations": _report_recommendations(answer),
            "limitations": limitations,
        },
        "code": [], "environment": {},
        "validation": {
            "status": validation["status"], "quality_score": validation["quality_score"],
            "coverage": validation["coverage"], "scoring_note": validation["scoring_note"],
        },
        "limitations": limitations, "dependency_fingerprint": dependency_fingerprint,
    }
