from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import pandas as pd

from ...core.database import Database
from ...agent.store import RunStore
from ..query_results import read_result_frame


def _json_value(value: Any) -> Any:
    from ..datasets import _json_safe

    return _json_safe(value)


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
    # A scalar belongs in an indicator card. Numeric measures alone do not
    # establish a category axis; explicit chart tools can choose numeric axes.
    if len(frame) < 2 or len(frame.select_dtypes(include="number").columns) == len(frame.columns):
        return []
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
    run_id: str | None = None,
    report_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .manifests import _result_for_ref
    from .content import field_definition

    content = report_content or {}
    result_frames = []
    seen = set()
    titles = {item["result_id"]: item["title"] for item in content.get("tables") or []}
    ordered_refs = [*titles, *evidence_refs]
    for ref in ordered_refs:
        result = _result_for_ref(database, workspace_id, ref)
        if not result or result["id"] in seen or result.get("completeness") != "complete":
            continue
        seen.add(result["id"])
        preview_rows = max(1, min(200, 5000 // max(1, len(result.get("columns") or []))))
        frame = read_result_frame(result, limit=preview_rows)
        result_frames.append((result, frame))
    tables, kpis, charts, glossary = [], list(content.get("metrics") or []), [], list(content.get("glossary") or [])
    for index, (result, frame) in enumerate(result_frames):
        tables.append({
            "id": "detail" if index == 0 else f"detail_{index + 1}",
            "title": titles.get(result["id"]) or ("分析明细" if index == 0 else f"补充数据 {index + 1}"),
            "result_id": result["id"], "columns": [str(value) for value in frame.columns],
            "rows": [{str(key): _json_value(value) for key, value in row.items()}
                     for row in frame.to_dict("records")],
            "total_rows": result.get("rows"), "completeness": result.get("completeness"),
            "server_paginated": True,
        })
        if not content.get("metrics") and int(result.get("rows") or 0) == 1:
            for item in _kpis(frame):
                item.update(id=f"{result['id']}_{item['id']}", result_id=result["id"],
                            evidence_cell={"result_id": result["id"], "row": 0, "column": item["label"]})
                kpis.append(item)
        for column in frame.columns:
            if any(item["result_id"] == result["id"] and item["column"] == str(column) for item in glossary):
                continue
            glossary.append(field_definition(database, workspace_id, result, str(column)))
    actions = RunStore(database).actions(run_id) if run_id else []
    for action in actions:
        if action.get("tool_id") != "generate_chart" or action.get("status") != "succeeded":
            continue
        value = (action.get("result") or {}).get("value") or {}
        if value.get("result_id") in seen and value.get("spec"):
            charts.append({**value["spec"], "id": value["id"], "result_id": value["result_id"], "available": True})
    charted = {item.get("result_id") for item in charts}
    for result, frame in result_frames:
        if result["id"] in charted:
            continue
        charts.extend({**item, "id": f"{result['id']}_{item['id']}", "result_id": result["id"],
                       "preview_rows": min(20, len(frame)), "total_rows": result.get("rows")}
                      for item in _charts(frame))
    for item in kpis:
        entry = next((entry for entry in glossary if entry["result_id"] == item.get("result_id")
                      and entry["column"] == (item.get("evidence_cell") or {}).get("column")), None)
        if entry:
            for key in ("definition", "formula", "unit", "notes", "definition_status"):
                item.setdefault(key, entry.get(key))
            if entry.get("definition_status") == "governed":
                item["label"] = entry["name"]
    limitations = [item["reason"] for item in validation["issues"]]
    limitations.append("自动核验覆盖数据结果和显式数值；业务解释、因果判断与建议需人工复核")
    if any(item.get("total_rows") is not None and item.get("preview_rows") is not None
           and item["total_rows"] > item["preview_rows"] for item in charts):
        limitations.append("部分图表仅展示前 20 条记录；完整结果请查看明细或导出 Excel")
    limitations.extend(content.get("limitations") or [])
    if not result_frames:
        limitations.append("未找到可本地渲染的已验证有界结果；需先在仓内生成小型精确聚合")
    methods = []
    quality_checks = []
    reuse_notes = []
    for action in actions:
        if action.get("status") != "succeeded":
            continue
        if action.get("tool_id") == "query_metric":
            methods.append("按已发布指标定义查询")
        elif action.get("tool_id") == "query_data":
            methods.append("在所选数据源执行只读查询")
        elif action.get("tool_id") == "run_analysis":
            method = (action.get("arguments") or {}).get("method")
            # The action stores arguments separately from the result; names stay user-readable.
            names = {"describe": "描述统计", "correlation": "相关性分析", "grouped_summary": "分组统计",
                     "decile": "分位分析", "ab_test": "组间比较", "linear_regression": "线性回归",
                     "kmeans": "聚类分析", "anomaly": "异常检测", "trend_forecast": "趋势预测"}
            methods.append(names.get(method, "受控统计分析"))
        elif action.get("tool_id") == "profile_data":
            profile = ((action.get("result") or {}).get("value") or {}).get("profile") or {}
            methods.append("数据质量探查")
            quality_checks.append({
                "scope": "样本数据" if profile.get("sampled") or profile.get("completeness") == "partial"
                else "所选探查范围",
                "rows": profile.get("rows"), "missing_cells": profile.get("missing_cells"),
                "duplicate_rows": profile.get("duplicate_rows"), "note": profile.get("note") or "",
            })
        elif action.get("tool_id") == "reuse_analysis_result":
            methods.append("复用已核验分析数据，未刷新来源")
            note = ((action.get("result") or {}).get("value") or {}).get("reuse_note")
            if note:
                reuse_notes.append(str(note))
    assumptions = list(dict.fromkeys([*(contract.get("confirmed_assumptions") or []),
                                     *(content.get("assumptions") or []), *reuse_notes]))
    limitations.extend(reuse_notes)
    findings = content.get("findings") or []
    return {
        "title": content.get("title") or contract.get("objective") or "数据分析报告",
        "contract": contract, "summary": answer, "evidence_refs": evidence_refs,
        "claims": [], "kpis": kpis, "charts": charts, "tables": tables, "glossary": glossary,
        "data_quality": {
            "completeness": "完整" if result_frames else "未评估", "accuracy": "未评估",
            "summary": "查询完整性已核验；业务准确性尚未独立评估" if result_frames else "数据质量尚未评估",
            "checks": quality_checks,
        },
        "report": {
            "problem_and_definitions": contract,
            "data_results": answer,
            "findings": findings, "attribution": findings,
            "actions": content.get("recommendations") or [],
            "recommendations": _report_recommendations(answer),
            "methods": list(dict.fromkeys(methods)), "assumptions": assumptions,
            "limitations": limitations,
        },
        "code": [], "environment": {},
        "validation": {
            "status": validation["status"], "quality_score": validation["quality_score"],
            "coverage": validation["coverage"], "scoring_note": validation["scoring_note"],
        },
        "limitations": limitations, "dependency_fingerprint": dependency_fingerprint,
    }
