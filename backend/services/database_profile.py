"""Explicit metadata, sample and database-side aggregate exploration."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import sqlglot

from .analytics import profile
from .authorization import actor_role, require_sources_access
from .data_policy import effective_policy, policy_fingerprint
from .database_catalog import ensure_columns, scoped_tables, upgrade_source
from .datasets import db, execute_query, load_result_frame, settings


def profile_database(source: dict, *, table_name: str | None = None, actor_id: str = "local-default",
                     mode: str = "sample", columns: list[str] | None = None, sample_rows: int = 500,
                     time_column: str = "", time_start: str = "", time_end: str = "",
                     time_format: str = "iso",
                     should_cancel=None) -> dict:
    if mode not in {"metadata", "sample", "aggregate"}:
        raise ValueError("探查方式必须为 metadata、sample 或 aggregate")
    source = require_sources_access(db(), [source["id"]], workspace_id=source.get("workspace_id", "default"),
                                   actor_id=actor_id, action="analyze")[0]
    source = upgrade_source(source)
    available = scoped_tables(source)
    name = table_name or (available[0]["name"] if available else "")
    source = ensure_columns(source, [name])
    table = next(table for table in source["tables"] if table["name"] == name)
    if table.get("schema_error"):
        raise ValueError(table["schema_error"])
    role = actor_role(db(), source.get("workspace_id", "default"), actor_id)
    row_filters, denied = effective_policy(source, name, actor_id, role)
    visible = [column for column in table["schema"] if column["name"] not in denied]
    chosen = list(dict.fromkeys(columns or [column["name"] for column in visible[:10 if mode == "aggregate" else 50]]))
    names = {column["name"] for column in visible}
    if not chosen or any(column not in names for column in chosen):
        raise PermissionError("探查字段不存在或不在当前授权范围")
    if len(chosen) > (10 if mode == "aggregate" else 50):
        raise ValueError("样本探查最多选择 50 个字段，汇总探查最多选择 10 个字段")
    dialect = source.get("engine_family") if source.get("engine_family") in {"doris", "starrocks"} else {
        "postgresql": "postgres", "mssql": "tsql"}.get(source.get("driver"), source.get("driver", "mysql"))
    def quote(value):
        return sqlglot.exp.Identifier(this=str(value), quoted=True).sql(dialect=dialect)
    qualified = quote(table["source_name"])
    if table.get("schema_name"):
        qualified = quote(table["schema_name"]) + "." + qualified
    predicates = []
    if time_format not in {"iso", "yyyyMMdd", "yyyyMMddHH", "unix_seconds", "unix_milliseconds"}:
        raise ValueError("时间字段格式不受支持，请选择字段实际使用的存储格式")
    if time_start or time_end:
        if time_column not in names:
            raise ValueError("请先选择可访问的时间字段")
        for value in (time_start, time_end):
            if value:
                try:
                    datetime.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError("时间范围请使用 YYYY-MM-DD 或 ISO 日期时间格式") from exc
        if time_start and time_end:
            try:
                ordered = datetime.fromisoformat(time_start) < datetime.fromisoformat(time_end)
            except TypeError as exc:
                raise ValueError("开始和结束时间须使用相同的时区格式") from exc
            if not ordered:
                raise ValueError("结束时间必须晚于开始时间")
        for value, operator in ((time_start, ">="), (time_end, "<")):
            if value:
                if time_format != "iso":
                    parsed = datetime.fromisoformat(value)
                    if time_format in {"yyyyMMdd", "yyyyMMddHH"}:
                        value = parsed.strftime("%Y%m%d%H" if time_format == "yyyyMMddHH" else "%Y%m%d")
                    else:
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                        value = str(int(parsed.timestamp() * (1000 if time_format == "unix_milliseconds" else 1)))
                predicates.append(f"{quote(time_column)} {operator} {sqlglot.exp.Literal.string(value).sql(dialect=dialect)}")
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    scope = {"table": name, "columns": chosen, "time_column": time_column,
             "time_start": time_start, "time_end": time_end, "row_policy_applied": bool(row_filters)}
    scope["time_format"] = time_format
    if mode == "metadata":
        return {"table": name, "mode": mode, "rows": None if row_filters else table.get("rows"),
                "rows_estimated": True, "column_count": len(visible), "columns": visible,
                "accuracy": "unknown", "completeness": "unknown", "scope": scope,
                "note": "数据库元数据估计，未扫描业务数据，不代表全量质量检查。"}
    key = hashlib.sha256(json.dumps({"source": source["id"], "version": source.get("last_refreshed_at"),
        "policy": policy_fingerprint([source], actor_id=actor_id, role=role), "actor": actor_id,
        "mode": mode, "scope": scope, "sample_rows": sample_rows}, sort_keys=True).encode()).hexdigest()
    cached = db().get("database_profiles", key, workspace_id=source.get("workspace_id", "default"))
    if cached and time.time() - cached["timestamp"] < 120:
        return {**cached["profile"], "cached": True}
    if mode == "sample":
        size = max(1, min(int(sample_rows), 5000, settings().max_query_rows))
        sql = f"SELECT {', '.join(quote(column) for column in chosen)} FROM {qualified}{where}"
        result = execute_query([source["id"]], sql, source.get("workspace_id", "default"), size,
                               actor_id=actor_id, should_cancel=should_cancel)
        value = profile(load_result_frame(result["id"]))
        truncated = result["completeness"] != "complete"
        value.update(table=name, mode=mode, sampled=True, sample_rows=value["rows"], sample_truncated=truncated,
                     accuracy="sample_based" if truncated else "exact", completeness="partial" if truncated else "complete",
                     result_id=result["id"], scope=scope, sampling_method="bounded_window" if predicates else "bounded_preview",
                     sampling_note="有界预览样本，未进行随机或分层抽样；缺失和重复统计仅描述所选字段及返回记录，不能推断全表分布。",
                     total_rows_estimate=None if row_filters else table.get("rows"))
    else:
        fields = ["COUNT(*) AS profile_count"]
        approximate = source.get("engine_family") in {"doris", "starrocks"}
        for index, column in enumerate(chosen):
            identifier = quote(column)
            fields.append(f"SUM(CASE WHEN {identifier} IS NULL THEN 1 ELSE 0 END) AS missing_{index}")
            fields.append(f"{'APPROX_COUNT_DISTINCT' if approximate else 'COUNT'}"
                          f"({'DISTINCT ' if not approximate else ''}{identifier}) AS unique_{index}")
        result = execute_query([source["id"]], f"SELECT {', '.join(fields)} FROM {qualified}{where}",
                               source.get("workspace_id", "default"), 1, actor_id=actor_id, should_cancel=should_cancel)
        row = result["data"][0]
        count = int(row["profile_count"])
        column_stats = []
        for index, column in enumerate(chosen):
            meta = next(entry for entry in visible if entry["name"] == column)
            missing = int(row.get(f"missing_{index}") or 0)
            column_stats.append({**meta, "missing": missing, "missing_rate": missing / count if count else 0,
                                 "unique": int(row.get(f"unique_{index}") or 0), "unique_estimated": approximate})
        value = {"table": name, "mode": mode, "rows": count, "column_count": len(chosen),
                 "columns": column_stats, "missing_cells": sum(entry["missing"] for entry in column_stats),
                 "duplicate_rows": None, "quality_score": None, "sampled": False,
                 "accuracy": "approximate" if approximate else "exact", "completeness": "complete",
                 "result_id": result["id"], "scope": scope, "warnings": result.get("warnings", []),
                 "note": "统计覆盖所选字段和时间范围；缺失数为精确统计，去重数为近似统计。" if approximate
                         else "统计覆盖所选字段和时间范围，未计算全行重复率。"}
    value["cached"] = False
    value["checked_at"] = datetime.now().astimezone().isoformat()
    db().put("database_profiles", {"id": key, "profile": value, "timestamp": time.time(), "source_id": source["id"]},
             workspace_id=source.get("workspace_id", "default"))
    return value
