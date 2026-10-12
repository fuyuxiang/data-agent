"""Evidence-bound report content shared by the UI and every export format.

The model supplies labels and interpretation. Metric values are always read
from current-run query cells; this service neither queries sources nor runs code.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ...agent.store import RunStore
from ...core.database import Database
from ..authorization import require_sources_access
from ..query_results import read_result_frame


CELL_SCHEMA = {
    "type": "object", "properties": {
        "result_id": {"type": "string"}, "row": {"type": "integer", "minimum": 0},
        "column": {"type": "string"},
    }, "required": ["result_id", "row", "column"],
}
REPORT_SCHEMA = {
    "type": "object", "properties": {
        "title": {"type": "string"},
        "metrics": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "properties": {
                "name": {"type": "string"}, "cell": CELL_SCHEMA,
                "definition": {"type": "string"}, "formula": {"type": "string"},
                "unit": {"type": "string"}, "notes": {"type": "string"},
            }, "required": ["name", "cell"],
        }},
        "findings": {"type": "array", "maxItems": 7, "items": {
            "type": "object", "properties": {
                "text": {"type": "string"},
                "type": {"type": "string", "enum": ["fact", "inference", "risk"]},
                "confidence": {"type": "string", "enum": ["high", "medium", "low", "not_evaluated"]},
                "confidence_reason": {"type": "string"},
                "why": {"type": "string"}, "impact": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            }, "required": ["text", "type", "evidence_refs"],
        }},
        "recommendations": {"type": "array", "maxItems": 7, "items": {
            "type": "object", "properties": {
                "text": {"type": "string"}, "owner": {"type": "string"},
                "scope": {"type": "string"},
                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            }, "required": ["text", "evidence_refs"],
        }},
        "glossary": {"type": "array", "maxItems": 100, "items": {
            "type": "object", "properties": {
                "name": {"type": "string"}, "result_id": {"type": "string"},
                "column": {"type": "string"}, "definition": {"type": "string"},
                "formula": {"type": "string"}, "unit": {"type": "string"}, "notes": {"type": "string"},
            }, "required": ["name", "result_id", "column"],
        }},
        "tables": {"type": "array", "maxItems": 20, "items": {
            "type": "object", "properties": {
                "result_id": {"type": "string"}, "title": {"type": "string"},
            }, "required": ["result_id", "title"],
        }},
        "assumptions": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
        "limitations": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
    },
}


def _text(value: Any, *, maximum: int = 2000, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("报告字段必须是长度受限的纯文本")
    value = value.strip()
    if required and not value:
        raise ValueError("报告必填字段不能为空")
    # Keep formatting in the renderer, not in model-provided content.
    if re.search(r"<\s*/?\s*(?:script|style|iframe|img|svg|div|span|p|a|strong|table|h[1-6]|ul|li)\b[^>]*>", value, re.I):
        raise ValueError("报告内容不得包含 HTML 标签")
    return value


def _items(payload: dict, key: str, maximum: int) -> list:
    items = payload.get(key, [])
    if not isinstance(items, list) or len(items) > maximum:
        raise ValueError(f"报告 {key} 必须是最多 {maximum} 项的数组")
    return items


def _object(value: Any, fields: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) - fields:
        raise ValueError("报告结构包含不支持的字段")
    return value


def _canonical(database: Database, workspace_id: str, ref: str) -> str:
    from .manifests import _result_for_ref

    result = _result_for_ref(database, workspace_id, ref)
    if not result:
        raise ValueError("报告引用的查询结果不存在")
    return str(result["id"])


def current_result_ids(database: Database, run: dict) -> list[str]:
    """Only successful actions in this run grant report evidence access."""
    from .manifests import _expand_result_refs

    refs = []
    for action in RunStore(database).actions(run["id"]):
        if action.get("status") != "succeeded":
            continue
        stored = action.get("result") or {}
        value = stored.get("value") or stored
        raw = list((stored.get("tool_result") or {}).get("output_refs") or [])
        raw += list(value.get("output_refs") or [])
        raw += [value.get("result_id"), value.get("dataset_ref_id")]
        for ref in raw:
            if ref:
                refs.extend(_expand_result_refs(database, run["workspace_id"], str(ref)))
    return list(dict.fromkeys(refs))


def field_definition(database: Database, workspace_id: str, result: dict, column: str) -> dict:
    semantic = result.get("semantic_query") or {}
    metric = semantic.get("metric_definition") or {}
    if column == semantic.get("metric_name") and not metric:
        metric = database.get("semantic_metric_versions",
                              f"{semantic.get('metric_id')}:{semantic.get('metric_version')}",
                              workspace_id=workspace_id) or {}
    governed = bool(metric and column == semantic.get("metric_name"))
    return {
        "name": str(metric.get("label") or column) if governed else column,
        "definition": str(metric.get("definition") or metric.get("description") or metric.get("label") or column)
        if governed else "本次查询返回的字段，业务定义待核对",
        "formula": str(metric.get("formula") or metric.get("expression") or "") if governed else "",
        "unit": str(metric.get("unit") or "") if governed else "", "notes": "",
        "definition_status": "governed" if governed else "exploratory",
        "result_id": result["id"], "column": column,
        "metric_version": semantic.get("metric_version") if governed else None,
    }


def normalize_report(database: Database, run: dict, payload: dict, allowed_ids: list[str]) -> dict:
    from .manifests import _build_claims
    from .rendering import _json_value
    from ..advanced_agent import _source_authorized

    if not _source_authorized(database, run):
        raise PermissionError("数据权限已变更，请重新发起分析")

    _object(payload, set(REPORT_SCHEMA["properties"]))
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 100_000:
        raise ValueError("报告内容超过大小上限")
    results: dict[str, dict] = {}

    def result_for(ref: Any) -> dict:
        ref = _text(ref, maximum=200, required=True)
        result_id = _canonical(database, run["workspace_id"], ref)
        if result_id not in allowed_ids:
            raise PermissionError("报告只能引用当前分析已执行的查询结果")
        if result_id not in results:
            result = database.get("query_results", result_id, workspace_id=run["workspace_id"])
            if result.get("completeness") != "complete":
                raise ValueError("报告不得把截断或抽样结果当作完整证据")
            if not set(result.get("source_ids") or []).issubset(set(run["source_scope"])):
                raise PermissionError("报告证据超出已确认的数据范围")
            require_sources_access(database, result.get("source_ids") or [],
                                   workspace_id=run["workspace_id"], actor_id=run["actor_id"])
            results[result_id] = result
        return results[result_id]

    def refs_for(item: dict) -> list[str]:
        refs = _items(item, "evidence_refs", 20)
        if not refs:
            raise ValueError("每条发现和建议都需要当前分析的证据引用")
        return list(dict.fromkeys(result_for(ref)["id"] for ref in refs))

    def definition(item: dict, result: dict, column: str) -> dict:
        entry = field_definition(database, run["workspace_id"], result, column)
        entry["name"] = _text(item.get("name"), maximum=120, required=True)
        entry["notes"] = _text(item.get("notes"))
        if entry["definition_status"] != "governed":
            entry.update(definition=_text(item.get("definition")) or entry["definition"],
                         formula=_text(item.get("formula")), unit=_text(item.get("unit"), maximum=40))
        return entry

    metrics, glossary, findings, recommendations, tables = [], [], [], [], []
    for index, raw in enumerate(_items(payload, "metrics", 8)):
        item = _object(raw, {"name", "cell", "definition", "formula", "unit", "notes"})
        cell = _object(item.get("cell"), {"result_id", "row", "column"})
        result = result_for(cell.get("result_id"))
        row = cell.get("row")
        if not isinstance(row, int) or isinstance(row, bool) or row < 0 or row >= int(result.get("rows") or 0):
            raise ValueError("指标证据行号无效")
        column = _text(cell.get("column"), maximum=200, required=True)
        frame = read_result_frame(result, offset=row, limit=1)
        if column not in frame.columns or frame.empty:
            raise ValueError("指标证据字段不存在")
        value = _json_value(frame.iloc[0][column])
        if isinstance(value, (dict, list, bool)):
            raise ValueError("指标证据必须是数值或标量")
        entry = definition(item, result, column)
        glossary.append(entry)
        metrics.append({
            **entry, "id": f"kpi_{index + 1}", "label": entry["name"], "value": value,
            "aggregation": "query_value", "evidence_cell": {
                "result_id": result["id"], "row": row, "column": column,
            },
        })
    for raw in _items(payload, "glossary", 100):
        item = _object(raw, {"name", "result_id", "column", "definition", "formula", "unit", "notes"})
        result = result_for(item.get("result_id"))
        column = _text(item.get("column"), maximum=200, required=True)
        if column not in result.get("columns", []):
            raise ValueError("指标词典字段不存在")
        glossary.append(definition(item, result, column))
    for raw in _items(payload, "findings", 7):
        item = _object(raw, {"text", "type", "confidence", "confidence_reason", "why", "impact", "evidence_refs"})
        refs = refs_for(item)
        kind = item.get("type")
        confidence = item.get("confidence") or "not_evaluated"
        if kind not in {"fact", "inference", "risk"} or confidence not in {"high", "medium", "low", "not_evaluated"}:
            raise ValueError("发现类型或置信度无效")
        reason = _text(item.get("confidence_reason"))
        if confidence != "not_evaluated" and not reason:
            raise ValueError("声明置信度时必须说明依据")
        if kind == "inference" and confidence == "high":
            raise ValueError("未经独立验证的推测不能标为高置信度")
        text = _text(item.get("text"), required=True)
        why, impact = _text(item.get("why")), _text(item.get("impact"))
        claims = _build_claims(database, run["workspace_id"], "\n".join([text, why, impact]), refs)
        if any(claim["numeric_replay"] == "FAIL" for claim in claims):
            raise ValueError("发现中的数字无法从所引用的查询结果核对，请先查询出推导值")
        findings.append({"text": text, "type": kind, "confidence": confidence,
                         "confidence_reason": reason, "why": why, "impact": impact, "evidence_refs": refs})
    for raw in _items(payload, "recommendations", 7):
        item = _object(raw, {"text", "owner", "scope", "priority", "evidence_refs"})
        priority = item.get("priority") or "medium"
        if priority not in {"high", "medium", "low"}:
            raise ValueError("建议优先级无效")
        recommendations.append({"text": _text(item.get("text"), required=True),
                                "owner": _text(item.get("owner"), maximum=120),
                                "scope": _text(item.get("scope"), maximum=300),
                                "priority": priority, "evidence_refs": refs_for(item)})
    for raw in _items(payload, "tables", 20):
        item = _object(raw, {"result_id", "title"})
        tables.append({"result_id": result_for(item.get("result_id"))["id"],
                       "title": _text(item.get("title"), maximum=160, required=True)})
    if not (metrics or findings or glossary or tables):
        raise ValueError("报告至少需要指标、发现、指标解释或数据表")
    return {
        "version": 1, "title": _text(payload.get("title"), maximum=160),
        "metrics": metrics, "findings": findings, "recommendations": recommendations,
        "glossary": list({(item["name"], item["result_id"], item["column"]): item for item in glossary}.values()),
        "tables": tables,
        "assumptions": [_text(item, maximum=1000, required=True) for item in _items(payload, "assumptions", 10)],
        "limitations": [_text(item, maximum=1000, required=True) for item in _items(payload, "limitations", 10)],
        "evidence_refs": list(results),
    }


def _digest(content: dict) -> str:
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def submit_report(database: Database, run: dict, payload: dict) -> dict:
    current = RunStore(database).get_run(run["id"], workspace_id=run["workspace_id"])
    if not current or current["execution_status"] in {"finished", "failed", "cancelled", "cancelling"}:
        raise ValueError("已结束或终止的分析不能修改报告内容")
    content = normalize_report(database, run, payload, current_result_ids(database, run))
    database.put("analysis_report_content", {
        "id": run["id"], "workspace_id": run["workspace_id"], "actor_id": run["actor_id"],
        "input": payload, "content": content, "fingerprint": _digest(content),
    }, workspace_id=run["workspace_id"])
    return {"report_id": run["id"], "content": content, "completeness": "complete",
            "output_refs": content["evidence_refs"]}


def checked_report(database: Database, run: dict, refs: list[str]) -> dict | None:
    record = database.get("analysis_report_content", run["id"], workspace_id=run["workspace_id"])
    attempts = [action for action in RunStore(database).actions(run["id"])
                if action.get("tool_id") == "submit_analysis_report"]
    if attempts and attempts[-1].get("status") != "succeeded":
        raise ValueError("最新报告内容尚未通过校验，请修正报告字段与证据引用后重新提交")
    if not record:
        if attempts:
            raise ValueError("报告内容尚未通过校验，请修正报告字段与证据引用后重新提交")
        return None
    if record.get("actor_id") != run["actor_id"]:
        raise PermissionError("报告内容不属于当前分析用户")
    content = normalize_report(database, run, record["input"], refs)
    if _digest(content) != record.get("fingerprint"):
        raise ValueError("报告证据已变化，请根据当前结果重新提交报告")
    return content
