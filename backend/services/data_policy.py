"""Small, explicit row and column policies for governed data sources.

Policies are evaluated before a query sees source rows.  The same policy is
used for local frames, database relation rewrites, and asset metadata.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pandas as pd
import sqlglot
from sqlglot import exp


def _rules(value: Any, name: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是对象")
    rules = value.get("rules", [value])
    if not isinstance(rules, list) or not rules or len(rules) > 50:
        raise ValueError(f"{name}.rules 必须包含 1–50 条规则")
    if any(not isinstance(rule, dict) for rule in rules):
        raise ValueError(f"{name}.rules 的每项必须是对象")
    return rules


def _grants(value: Any, *, scalar: bool) -> dict[str, list]:
    if not isinstance(value, dict) or not value:
        raise ValueError("权限规则需要非空的 allow 或 deny 映射")
    result = {}
    for principal, values in value.items():
        principal = str(principal)
        if principal != "*" and not principal.startswith("user:") and principal not in {
            "role:owner", "role:editor", "role:analyst", "role:viewer",
        }:
            raise ValueError(f"权限主体无效：{principal}")
        if not isinstance(values, list) or len(values) > 500:
            raise ValueError("权限值必须是最多 500 项的数组")
        if scalar and any(not isinstance(item, (str, int, float, bool)) or isinstance(item, float) and not pd.notna(item) for item in values):
            raise ValueError("行权限只支持有限的标量值")
        if not scalar and any(not isinstance(item, str) or not item for item in values):
            raise ValueError("列权限只支持非空字段名")
        result[principal] = list(dict.fromkeys(values))
    return result


def normalize_policies(row_policy: Any, column_policies: Any, schema: dict) -> tuple[dict | None, dict | None]:
    """Validate administrator input against the source's known tables/columns."""
    tables = {
        str(table["name"]): {str(column["name"]) for column in table.get("columns") or []}
        for table in schema.get("tables") or []
    }
    aliases = {}
    for table in schema.get("tables") or []:
        name = str(table["name"])
        for alias in (name, table.get("source_name")):
            if alias:
                aliases[str(alias)] = name

    def targets(raw_table: Any) -> tuple[str, list[set[str]]]:
        selected = str(raw_table or "*")
        if selected == "*":
            if not tables:
                raise ValueError("数据源没有可配置权限的数据表")
            return selected, list(tables.values())
        canonical = aliases.get(selected)
        if canonical is None:
            raise ValueError("权限规则指定的数据表不存在")
        return canonical, [tables[canonical]]

    normalized = []
    for rule in _rules(row_policy, "row_policy"):
        table, target_columns = targets(rule.get("table"))
        column = str(rule.get("column") or "")
        if not column or any(column not in columns for columns in target_columns):
            raise ValueError("行权限字段不存在于指定数据表")
        normalized.append({"table": table, "column": column, "allow": _grants(rule.get("allow"), scalar=True)})
    rows = {"rules": normalized} if normalized else None
    normalized = []
    for rule in _rules(column_policies, "column_policies"):
        table, target_columns = targets(rule.get("table"))
        deny = _grants(rule.get("deny"), scalar=False)
        if any(column not in columns for columns in target_columns for items in deny.values() for column in items):
            raise ValueError("列权限字段不存在于指定数据表")
        normalized.append({"table": table, "deny": deny})
    columns = {"rules": normalized} if normalized else None
    return rows, columns


def policy_fingerprint(sources: list[dict], *, actor_id: str, role: str = "") -> str:
    relevant = [{
        "id": source.get("id"), "authorized_user_ids": source.get("authorized_user_ids"),
        "row_policy": source.get("row_policy"), "column_policies": source.get("column_policies"),
        "analysis_tables": source.get("analysis_tables"), "lineage": source.get("lineage"),
    } for source in sources]
    payload = {"actor_id": actor_id, "role": role, "sources": relevant}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _principal_values(grants: dict, actor_id: str, role: str) -> list:
    keys = ("*", f"role:{role}", f"user:{actor_id}")
    return list(dict.fromkeys(item for key in keys for item in grants.get(key, [])))


def effective_policy(source: dict, table: str, actor_id: str, role: str) -> tuple[list[tuple[str, list]], set[str]]:
    row_filters = []
    denied = set()
    for rule in _rules(source.get("row_policy"), "row_policy"):
        if rule.get("table", "*") in {"*", table}:
            row_filters.append((str(rule["column"]), _principal_values(rule["allow"], actor_id, role)))
    for rule in _rules(source.get("column_policies"), "column_policies"):
        if rule.get("table", "*") in {"*", table}:
            denied.update(_principal_values(rule["deny"], actor_id, role))
    return row_filters, denied


def filter_frame(frame: pd.DataFrame, source: dict, table: str, actor_id: str, role: str) -> pd.DataFrame:
    filters, denied = effective_policy(source, table, actor_id, role)
    if not filters and not denied:
        return frame.copy()
    output = frame
    for column, values in filters:
        if column not in output.columns:
            raise PermissionError("行权限字段缺失，拒绝读取数据")
        output = output.loc[output[column].isin(values)]
    if denied:
        output = output.drop(columns=[column for column in denied if column in output.columns])
    if not len(output.columns):
        raise PermissionError("所有字段均受列权限保护")
    return output.copy()


def rewrite_database_sql(sql: str, source: dict, *, actor_id: str, role: str, dialect: str) -> str:
    """Replace each base relation with a secured subquery before aggregation.

    CTE references are left alone; their underlying base relations are rewritten.
    The read-only/table-scope validator must run before this function.
    """
    if not source.get("row_policy") and not source.get("column_policies"):
        return sql
    statement = sqlglot.parse_one(sql, read=dialect)
    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    catalogs = {
        str(alias).lower(): table for table in source.get("tables") or []
        for alias in (table.get("name"), table.get("source_name")) if alias
    }
    for relation in list(statement.find_all(exp.Table)):
        name = relation.name.lower()
        if name in cte_names and not relation.db:
            continue
        table = catalogs.get(name)
        if table is None:
            raise PermissionError("权限策略无法识别查询的数据表")
        row_filters, denied = effective_policy(source, str(table.get("name") or table.get("source_name")), actor_id, role)
        if not row_filters and not denied:
            continue
        columns = [str(item["name"]) for item in table.get("schema") or []]
        if not columns or any(column not in columns for column, _ in row_filters):
            raise PermissionError("受保护数据表缺少可验证的字段结构")
        visible = [column for column in columns if column not in denied]
        if not visible:
            raise PermissionError("所有字段均受列权限保护")
        secured = exp.select(*(exp.column(column) for column in visible)).from_(relation.copy())
        for column, values in row_filters:
            predicate = exp.false() if not values else exp.column(column).isin(*(exp.convert(value) for value in values))
            secured = secured.where(predicate)
        alias = relation.alias_or_name
        relation.replace(secured.subquery(alias=alias))
    return statement.sql(dialect=dialect)


def reject_denied_columns(sql: str, sources: list[dict], *, actor_id: str, role: str, dialect: str | None) -> None:
    """Return an authorization error before a SQL engine can expose a hidden field."""
    denied = set()
    for source in sources:
        for table in source.get("tables") or []:
            denied.update(effective_policy(
                source, str(table.get("name") or table.get("source_name")), actor_id, role,
            )[1])
    if not denied:
        return
    parsed = sqlglot.parse_one(sql, read=dialect)
    if any(column.name in denied for column in parsed.find_all(exp.Column)):
        raise PermissionError("查询引用了未授权字段")
