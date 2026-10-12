"""Read-only discovery and demand-loaded schemas for native SQL sources."""
from __future__ import annotations

import hashlib
import warnings

from sqlalchemy import inspect, text

from ..core.database import utcnow
from .data_policy import effective_policy


def discover(connection, schema_name: str | None = None) -> tuple[list[dict], str]:
    dialect = connection.engine.dialect.name
    family = dialect
    if dialect == "mysql":
        comment = connection.execute(text("SHOW VARIABLES LIKE 'version_comment'")).first()
        if comment and "doris" in str(comment[1]).lower():
            family = "doris"
        elif comment and "starrocks" in str(comment[1]).lower():
            family = "starrocks"
        selected = schema_name or connection.execute(text("SELECT DATABASE()")).scalar()
        rows = connection.execute(text(
            "SELECT TABLE_NAME,TABLE_TYPE,TABLE_ROWS,DATA_LENGTH,TABLE_COMMENT "
            "FROM information_schema.tables WHERE TABLE_SCHEMA=:schema ORDER BY TABLE_NAME"
        ), {"schema": selected}).mappings().all()
        counts = dict(connection.execute(text(
            "SELECT TABLE_NAME,COUNT(*) FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=:schema GROUP BY TABLE_NAME"
        ), {"schema": selected}).all())
        return [{
            "name": row["TABLE_NAME"], "source_name": row["TABLE_NAME"],
            "schema_name": selected, "object_type": "view" if row["TABLE_TYPE"] == "VIEW" else "table",
            "rows": max(0, int(row["TABLE_ROWS"])) if row["TABLE_ROWS"] is not None and int(row["TABLE_ROWS"]) >= 0 else None,
            "rows_estimated": True, "data_bytes": row["DATA_LENGTH"],
            "comment": str(row["TABLE_COMMENT"] or ""), "columns": counts.get(row["TABLE_NAME"]),
            "schema_loaded": False,
        } for row in rows], family
    inspector = inspect(connection)
    objects = [(name, "table") for name in inspector.get_table_names(schema=schema_name)]
    objects.extend((name, "view") for name in inspector.get_view_names(schema=schema_name))
    tables = []
    for name, kind in sorted(objects):
        entry = {"name": name, "source_name": name, "schema_name": schema_name,
                 "object_type": kind, "rows": None, "schema_loaded": False}
        # Local SQLite fixtures and local analytical databases retain eager,
        # inexpensive reflection. Remote engines load only requested columns.
        if dialect == "sqlite":
            entry.update(read_columns(connection, entry))
        tables.append(entry)
    return tables, family


def read_columns(connection, table: dict) -> dict:
    if connection.engine.dialect.name == "mysql":
        schema = table.get("schema_name") or connection.execute(text("SELECT DATABASE()")).scalar()
        rows = connection.execute(text(
            "SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_COMMENT "
            "FROM information_schema.columns WHERE TABLE_SCHEMA=:schema AND TABLE_NAME=:table "
            "ORDER BY ORDINAL_POSITION"
        ), {"schema": schema, "table": table["source_name"]}).mappings().all()
        columns = [{"name": row["COLUMN_NAME"], "type": row["COLUMN_TYPE"],
                    "nullable": row["IS_NULLABLE"] == "YES", "comment": str(row["COLUMN_COMMENT"] or "")}
                   for row in rows]
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            rows = inspect(connection).get_columns(table["source_name"], schema=table.get("schema_name"))
        columns = [{"name": row["name"], "type": str(row["type"]),
                    "nullable": bool(row.get("nullable", True)), "comment": str(row.get("comment") or "")}
                   for row in rows]
    return {"schema": columns, "columns": len(columns), "schema_loaded": True,
            "schema_refreshed_at": utcnow(),
            "schema_error": None if columns else "该对象没有可读取的字段，可能是失效视图，请检查其依赖表。"}


def scoped_tables(source: dict) -> list[dict]:
    selected = source.get("analysis_tables")
    return [table for table in source.get("tables") or []
            if not isinstance(selected, list)
            or table.get("source_name", table.get("name")) in selected or table.get("name") in selected]


def upgrade_source(source: dict) -> dict:
    """Lazily upgrade older registrations without changing credentials or ACLs."""
    if source.get("kind") == "database" and not source.get("engine_family") and source.get("credential"):
        from .datasets import refresh_source

        return refresh_source(dict(source))
    return source


def catalog_page(source: dict, *, query: str = "", cursor: str = "", limit: int = 25,
                 actor_id: str | None = None) -> dict:
    from .datasets import db
    from .authorization import actor_role

    source = upgrade_source(source)
    size = max(1, min(int(limit), 100))
    terms = str(query).lower().split()
    tables = sorted(scoped_tables(source), key=lambda table: str(table["name"]))
    matched = [table for table in tables if not terms or all(
        term in f"{table['name']} {table.get('comment', '')}".lower() for term in terms)]
    page = [table for table in matched if str(table["name"]) > cursor][:size + 1]
    role = actor_role(db(), source.get("workspace_id", "default"), actor_id) if actor_id else ""
    items = []
    for table in page[:size]:
        policies = effective_policy(source, table["name"], actor_id or "", role) if actor_id else ([], set())
        protected = bool(policies[0] or policies[1])
        items.append({
            key: table.get(key) for key in ("name", "source_name", "schema_name", "object_type", "comment", "schema_error")
        } | {"query_name": table["name"], "rows": None if protected else table.get("rows"),
             "rows_estimated": bool(table.get("rows_estimated")),
             "columns": None if protected else table.get("columns"),
             "data_bytes": None if protected else table.get("data_bytes")})
    return {"source_id": source["id"], "items": items,
            "next_cursor": items[-1]["name"] if len(page) > size else None,
            "total": len(matched), "catalog_total": len(tables), "query": query}


def ensure_columns(source: dict, names: list[str], *, force: bool = False) -> dict:
    """Hydrate only explicitly selected tables; cache outside the source payload."""
    from .datasets import _configure_read_only, _database_engine, db, settings
    from .security import SecretVault
    from flask import current_app

    source = upgrade_source(source)
    catalog = {table["name"]: table for table in scoped_tables(source)}
    missing = [name for name in names if name not in catalog]
    if missing:
        raise PermissionError(f"数据表不存在或不在当前范围：{', '.join(missing[:5])}")
    result = {**source, "tables": [dict(table) for table in source.get("tables") or []]}
    loaded = {}
    pending = []
    database = db()
    for name in dict.fromkeys(names):
        table = catalog[name]
        cache_id = "schema_" + hashlib.sha256(
            f"{source['id']}:{source.get('last_refreshed_at')}:{name}".encode()
        ).hexdigest()
        cached = None if force else database.get("database_schemas", cache_id, workspace_id=source.get("workspace_id", "default"))
        if not force and table.get("schema_loaded", bool(table.get("schema"))):
            loaded[name] = table
        elif cached and not cached["table"].get("schema_error"):
            loaded[name] = cached["table"]
        else:
            pending.append((name, cache_id))
    if pending:
        url = SecretVault(current_app.config["VAULT_KEY"]).open(source.get("credential", ""), {}).get("url")
        if not url:
            raise ValueError("数据库凭据不可用，请重新建立连接")
        engine = _database_engine(url)
        try:
            with engine.connect() as connection:
                _configure_read_only(connection, settings().query_timeout_seconds)
                for name, cache_id in pending:
                    table = dict(catalog[name])
                    try:
                        table.update(read_columns(connection, table))
                    except Exception:
                        table.update(schema=[], columns=0, schema_loaded=True,
                                     schema_error="字段结构读取失败，请检查视图依赖或数据库访问权限。")
                    loaded[name] = table
                    database.put("database_schemas", {"id": cache_id, "table": table,
                                 "source_id": source["id"]}, workspace_id=source.get("workspace_id", "default"))
        finally:
            engine.dispose()
    result["tables"] = [loaded.get(table["name"], table) for table in result["tables"]]
    return result


def describe_page(source: dict, names: list[str], *, actor_id: str | None = None,
                  offset: int = 0, limit: int = 50, query: str = "") -> dict:
    from .datasets import db
    from .authorization import actor_role

    if len(names) > 5:
        raise ValueError("一次最多读取 5 张表的字段，请按需选择")
    source = ensure_columns(source, names)
    role = actor_role(db(), source.get("workspace_id", "default"), actor_id) if actor_id else ""
    start, size = max(0, int(offset)), max(1, min(int(limit), 100))
    tables = []
    for table in scoped_tables(source):
        if table["name"] not in names:
            continue
        denied = effective_policy(source, table["name"], actor_id or "", role)[1] if actor_id else set()
        columns = [dict(column, distinct=None, sample=[]) for column in table.get("schema") or [] if column["name"] not in denied]
        total_columns = len(columns)
        if query:
            columns = [column for column in columns if query.lower() in f"{column['name']} {column.get('comment', '')}".lower()]
        tables.append({"name": table["name"], "source_name": table["source_name"], "query_name": table["name"],
                       "comment": table.get("comment", ""), "rows": None if source.get("row_policy") else table.get("rows"),
                       "columns": columns[start:start + size], "column_count": total_columns, "matching_column_count": len(columns),
                       "column_offset": start, "next_column_offset": start + size if len(columns) > start + size else None,
                       "schema_error": table.get("schema_error") or ("当前用户没有可访问的字段。" if not total_columns and table.get("schema") else None)})
    return {"source_id": source["id"], "tables": tables, "mode": "columns"}
