"""
结构化证据检索模块（非向量）

用途：
1. 从执行过的 SQL 和结果中提取可解释证据
2. 输出来源表、参数、样例结果，便于审计与复盘
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


_TABLE_PATTERN = re.compile(
    r"(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
    flags=re.IGNORECASE,
)


def _truncate_value(value: Any, max_len: int = 160) -> Any:
    if isinstance(value, str) and len(value) > max_len:
        return value[: max_len - 3] + "..."
    return value


def _truncate_row(row: Dict[str, Any], max_fields: int = 20) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for idx, (key, value) in enumerate(row.items()):
        if idx >= max_fields:
            result["..."] = "truncated"
            break
        result[key] = _truncate_value(value)
    return result


def extract_tables_from_sql(sql: str) -> List[str]:
    """从 SQL 中提取 FROM/JOIN 的表名"""
    if not sql:
        return []
    tables = [match.group(1) for match in _TABLE_PATTERN.finditer(sql)]
    dedup: List[str] = []
    for table in tables:
        if table not in dedup:
            dedup.append(table)
    return dedup


def build_structured_evidence(
    *,
    question: str,
    intent: str,
    sql: str,
    sql_params: Optional[List[Any]],
    rows: Optional[List[Dict[str, Any]]],
    selected_tables: Optional[List[str]] = None,
    max_samples: int = 3,
) -> Dict[str, Any]:
    """
    构建结构化证据（无需额外向量检索）

    返回字段：
    - source_tables: 证据来源表
    - sql/sql_params: 执行依据
    - row_count/sample_rows: 结果证据样本
    """
    safe_rows = rows if isinstance(rows, list) else []
    sample_rows: List[Dict[str, Any]] = []
    for row in safe_rows[: max(1, max_samples)]:
        if isinstance(row, dict):
            sample_rows.append(_truncate_row(row))

    source_tables = extract_tables_from_sql(sql)
    if not source_tables and selected_tables:
        source_tables = [t for t in selected_tables if t]

    evidence: Dict[str, Any] = {
        "question": question,
        "intent": intent,
        "source_tables": source_tables,
        "sql": sql,
        "sql_params": sql_params or [],
        "row_count": len(safe_rows),
        "sample_rows": sample_rows,
    }

    if intent == "count":
        evidence["evidence_type"] = "aggregate"
        evidence["summary"] = (
            "统计结果基于上述 SQL 执行结果；sample_rows 展示聚合结果样本。"
        )
    else:
        evidence["evidence_type"] = "record"
        evidence["summary"] = (
            "明细结果基于上述 SQL 执行结果；sample_rows 展示命中记录样本。"
        )

    return evidence
