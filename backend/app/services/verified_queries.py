"""
已验证查询（Verified Queries）服务

目标：
1. 支持企业沉淀“可信问法 -> SQL 模板”
2. 在 NL2SQL 前置命中，降低幻觉与误判
3. 保持可配置、可审计、可逐步扩展
"""

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _normalize_scope(tables: Optional[List[str]]) -> List[str]:
    if not tables:
        return []
    return sorted({str(t).strip().lower() for t in tables if str(t).strip()})


def _default_verified_query_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "data" / "verified_queries.json"


@lru_cache(maxsize=1)
def _load_verified_queries() -> List[Dict[str, Any]]:
    path_str = os.getenv("VERIFIED_QUERIES_PATH", "").strip()
    path = Path(path_str).expanduser().resolve() if path_str else _default_verified_query_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
    except Exception:
        return []
    return []


def _extract_top_k(question: str) -> int:
    m = re.search(r"(?:前|top)\s*(\d+)", question, flags=re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*条", question)
    if m:
        return int(m.group(1))
    return 20


def _extract_year_month(question: str) -> Dict[str, str]:
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", question)
    if not m:
        return {}
    year = int(m.group(1))
    month = int(m.group(2))
    return {
        "year": f"{year}",
        "month": f"{month:02d}",
        "year_month": f"{year}{month:02d}",
    }


def _render_template(value: Any, variables: Dict[str, Any]) -> Any:
    if isinstance(value, str):
        rendered = value
        for key, var in variables.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", str(var))
        return rendered
    if isinstance(value, list):
        return [_render_template(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _render_template(v, variables) for k, v in value.items()}
    return value


def _scope_compatible(record_scope: List[str], selected_scope: List[str]) -> bool:
    if not record_scope:
        return True
    if not selected_scope:
        return False
    return set(record_scope).issubset(set(selected_scope))


def match_verified_query(question: str, selected_tables: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """
    命中已验证查询模板

    返回结构：
    {
      "intent": "...",
      "sql": "...",
      "params": [...],
      "filters": {...}
    }
    """
    question_norm = _normalize_text(question)
    selected_scope = _normalize_scope(selected_tables)
    variables: Dict[str, Any] = {"top_k": _extract_top_k(question)}
    variables.update(_extract_year_month(question))

    for record in _load_verified_queries():
        patterns = record.get("question_patterns") or []
        if not isinstance(patterns, list) or not patterns:
            continue

        record_scope = _normalize_scope(record.get("table_scope") or [])
        if not _scope_compatible(record_scope, selected_scope):
            continue

        matched = False
        for pattern in patterns:
            if not pattern:
                continue
            try:
                if re.search(pattern, question, flags=re.IGNORECASE):
                    matched = True
                    break
            except re.error:
                if _normalize_text(str(pattern)) == question_norm:
                    matched = True
                    break
        if not matched:
            continue

        sql = _render_template(record.get("sql", ""), variables)
        params = _render_template(record.get("params", []), variables)
        if not isinstance(params, list):
            params = []

        filters = {
            "verified_query": True,
            "verified_query_id": record.get("id"),
            "verified_query_name": record.get("name"),
            "verified_query_version": record.get("version"),
        }
        return {
            "intent": record.get("intent") or "list",
            "sql": sql,
            "params": params,
            "filters": filters,
        }

    return None

