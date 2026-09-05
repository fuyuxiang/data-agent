from __future__ import annotations

from pathlib import PurePath

import sqlglot
from sqlglot import expressions as exp


EXTERNAL_FUNCTIONS = frozenset({
    "delta_scan",
    "glob",
    "http_get",
    "http_post",
    "iceberg_scan",
    "load_file",
    "lo_import",
    "mysql_scan",
    "parquet_scan",
    "pg_read_binary_file",
    "pg_read_file",
    "postgres_scan",
    "query_table",
    "read_blob",
    "read_csv",
    "read_csv_auto",
    "read_json",
    "read_json_auto",
    "read_ndjson",
    "read_parquet",
    "read_text",
    "read_xlsx",
    "sqlite_scan",
    "st_read",
    "benchmark",
    "dblink",
    "dblink_connect",
    "get_lock",
    "load_extension",
    "lo_export",
    "nextval",
    "opendatasource",
    "openrowset",
    "pg_advisory_lock",
    "pg_advisory_xact_lock",
    "pg_cancel_backend",
    "pg_logical_emit_message",
    "pg_sleep",
    "pg_terminate_backend",
    "readfile",
    "release_lock",
    "set_config",
    "setval",
    "sleep",
    "writefile",
    "xp_cmdshell",
})

BLOCKED_NODE_TYPES = (
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Insert,
    exp.Merge,
    exp.Transaction,
    exp.Update,
)

READ_ONLY_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)
FILE_SUFFIXES = frozenset({
    ".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".parquet",
    ".xlsx", ".xls", ".db", ".sqlite", ".sqlite3", ".txt", ".log",
})
URL_PREFIXES = ("http://", "https://", "ftp://", "s3://", "gs://", "azure://", "hdfs://")


def _function_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name or "").lower()
    if isinstance(node, exp.Func):
        try:
            return str(node.sql_name() or "").lower()
        except (AttributeError, TypeError):
            return type(node).__name__.lower()
    return ""


def _looks_external_table(name: str) -> bool:
    value = str(name or "").strip().lower()
    if value.startswith(URL_PREFIXES) or "/" in value or "\\" in value:
        return True
    return PurePath(value).suffix in FILE_SUFFIXES


def validate_read_only_sql(sql: str, dialect: str | None = None) -> str:
    statement = str(sql or "").strip().rstrip(";").strip()
    if not statement:
        raise ValueError("查询不能为空")
    try:
        parsed = [item for item in sqlglot.parse(statement, read=dialect) if item is not None]
    except sqlglot.errors.ParseError as exc:
        raise ValueError(f"SQL 语法无法解析：{exc}") from exc
    if len(parsed) != 1:
        raise ValueError("一次只能执行一条 SQL")
    root = parsed[0]
    if not isinstance(root, READ_ONLY_ROOTS):
        raise ValueError("仅允许单条 SELECT、WITH 或集合查询")
    for node in root.walk():
        if isinstance(node, BLOCKED_NODE_TYPES):
            raise ValueError(f"查询包含被禁止的数据库操作：{type(node).__name__}")
        function_name = _function_name(node)
        if function_name in EXTERNAL_FUNCTIONS:
            raise ValueError(f"查询禁止访问外部文件、网络或数据库：{function_name}")
        if isinstance(node, exp.Table) and _looks_external_table(node.name):
            raise ValueError("查询禁止把文件路径或网络地址作为数据表")
    return statement


def bounded_read_only_sql(sql: str, limit: int, dialect: str | None = None) -> str:
    """Validate a query and enforce a server-side outer row limit."""
    statement = validate_read_only_sql(sql, dialect)
    root = sqlglot.parse_one(statement, read=dialect)
    existing = root.args.get("limit")
    existing_value = None
    if existing and isinstance(existing.expression, exp.Literal) and not existing.expression.is_string:
        try:
            existing_value = int(existing.expression.this)
        except (TypeError, ValueError):
            existing_value = None
    if existing_value is None or existing_value > limit:
        root = root.limit(limit, copy=False)
    return root.sql(dialect=dialect)
