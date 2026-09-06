from __future__ import annotations

import io
import hashlib
import json
import os
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import duckdb
import numpy as np
import pandas as pd
import sqlglot
from sqlglot import expressions as sql_exp
from flask import current_app
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import URL, make_url
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from ..core.database import Database, utcnow
from .security import SecretVault, safe_http_request, validate_outbound_host, validate_outbound_url
from .authorization import require_sources_access
from .sql_security import bounded_read_only_sql, validate_query_tables, validate_read_only_sql


SUPPORTED_FILE_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".parquet"}


def db() -> Database:
    return current_app.extensions["meridian_db"]


def settings():
    return current_app.config["SETTINGS"]


def _dialect_name(engine) -> str:
    return {
        "postgresql": "postgres",
        "mssql": "tsql",
    }.get(engine.dialect.name, engine.dialect.name)


def _configure_read_only(connection, timeout_seconds: int) -> None:
    dialect = connection.engine.dialect.name
    timeout_ms = max(1000, timeout_seconds * 1000)
    if dialect == "sqlite":
        connection.exec_driver_sql("PRAGMA query_only = ON")
        connection.exec_driver_sql(f"PRAGMA busy_timeout = {timeout_ms}")
        deadline = time.monotonic() + timeout_seconds
        raw = connection.connection.driver_connection
        raw.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    elif dialect == "postgresql":
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout_ms}")
    elif dialect == "mysql":
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        connection.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {timeout_ms}")
    elif dialect == "mssql":
        connection.exec_driver_sql(f"SET LOCK_TIMEOUT {timeout_ms}")
        connection.exec_driver_sql("SET DEADLOCK_PRIORITY LOW")


def _database_engine(url: str):
    engine = create_engine(url, pool_pre_ping=True, pool_recycle=300)
    if engine.dialect.name == "mssql":
        timeout = settings().query_timeout_seconds

        @event.listens_for(engine, "before_cursor_execute")
        def _set_query_timeout(_conn, cursor, _statement, _parameters, _context, _executemany):
            cursor.timeout = timeout
    return engine


def _qualified_table(engine, table: dict) -> str:
    quote = engine.dialect.identifier_preparer.quote
    name = quote(str(table["source_name"]))
    schema_name = str(table.get("schema_name") or "").strip()
    return f"{quote(schema_name)}.{name}" if schema_name else name


def _json_safe(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) or np.isinf(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, np.ndarray):
        return [_json_safe(item) for item in value.tolist()]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def frame_records(frame: pd.DataFrame, limit: int = 200) -> list[dict]:
    safe = frame.head(max(0, limit)).replace({np.nan: None})
    return [_json_safe(item) for item in safe.to_dict(orient="records")]


def _sanitize_table_name(value: str, fallback: str = "data") -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(value)).strip("_")
    if not value:
        value = fallback
    if value[0].isdigit():
        value = f"t_{value}"
    return value[:80]


def _read_tabular_file(path: Path) -> dict[str, pd.DataFrame]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        try:
            return {"data": pd.read_csv(path)}
        except UnicodeDecodeError:
            return {"data": pd.read_csv(path, encoding="gb18030")}
    if suffix == ".tsv":
        return {"data": pd.read_csv(path, sep="\t")}
    if suffix in {".xlsx", ".xls"}:
        return {str(name): frame for name, frame in pd.read_excel(path, sheet_name=None).items()}
    if suffix == ".json":
        try:
            return {"data": pd.read_json(path)}
        except ValueError:
            return {"data": pd.read_json(path, lines=True)}
    if suffix == ".parquet":
        return {"data": pd.read_parquet(path)}
    raise ValueError(f"不支持的文件格式：{suffix}")


def _validate_compressed_size(path: Path) -> None:
    expanded_limit = min(max(settings().max_upload_bytes * 8, 128 * 1024 * 1024), 1024 * 1024 * 1024)
    if path.suffix.lower() == ".xlsx":
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 10_000 or sum(item.file_size for item in members) > expanded_limit:
                raise ValueError("Excel 文件解压后超过安全大小上限")
            if any(item.file_size > 10 * 1024 * 1024 and item.file_size > max(1, item.compress_size) * 1000 for item in members):
                raise ValueError("Excel 文件包含异常压缩比内容")
    elif path.suffix.lower() == ".parquet":
        import pyarrow.parquet as parquet

        metadata = parquet.ParquetFile(path).metadata
        expanded = sum(
            metadata.row_group(group).column(column).total_uncompressed_size
            for group in range(metadata.num_row_groups)
            for column in range(metadata.row_group(group).num_columns)
        )
        if expanded > expanded_limit:
            raise ValueError("Parquet 文件解压后超过安全大小上限")


def _validate_frame_limits(frames: dict[str, pd.DataFrame]) -> None:
    total_cells = 0
    for name, frame in frames.items():
        if len(frame) > settings().max_ingest_rows:
            raise ValueError(f"数据表 {name} 超过 {settings().max_ingest_rows} 行导入上限")
        total_cells += int(len(frame)) * int(len(frame.columns))
        if total_cells > settings().max_ingest_cells:
            raise ValueError(f"文件超过 {settings().max_ingest_cells} 个单元格导入上限")


def register_upload(file: FileStorage, workspace_id: str) -> dict:
    original = secure_filename(file.filename or "")
    if not original:
        raise ValueError("请选择要上传的文件")
    suffix = Path(original).suffix.lower()
    if suffix not in SUPPORTED_FILE_EXTENSIONS:
        raise ValueError("仅支持 CSV、TSV、Excel、JSON 和 Parquet 文件")
    source_id = db().new_id("src")
    target = settings().upload_dir / f"{source_id}{suffix}"
    file.save(target)
    try:
        _validate_compressed_size(target)
        frames = _read_tabular_file(target)
        _validate_frame_limits(frames)
        tables = []
        for name, frame in frames.items():
            tables.append({
                "name": _sanitize_table_name(name),
                "source_name": str(name),
                "rows": int(len(frame)),
                "columns": int(len(frame.columns)),
            })
    except (ValueError, PermissionError):
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise ValueError("文件无法安全解析，请检查文件格式和完整性") from exc
    record = db().put(
        "sources",
        {
            "id": source_id,
            "workspace_id": workspace_id,
            "name": Path(original).stem,
            "kind": "file",
            "format": suffix.lstrip("."),
            "filename": original,
            "path": str(target),
            "tables": tables,
            "status": "ready",
            "last_refreshed_at": utcnow(),
        },
        workspace_id=workspace_id,
    )
    db().audit("source.uploaded", workspace_id=workspace_id, object_type="source", object_id=source_id, detail={"filename": original})
    return record


def _checked_sqlite_path(url: URL, workspace_id: str) -> str:
    path = Path(str(url.database or "")).expanduser().resolve()
    workspace = db().get("workspaces", workspace_id) or {}
    allowed = [settings().storage_dir.resolve()]
    if workspace.get("mounted_path"):
        allowed.append(Path(workspace["mounted_path"]).resolve())
    if not any(path == root or root in path.parents for root in allowed):
        raise ValueError("SQLite 文件必须位于工作区存储或已挂载目录内")
    return str(path)


def _harden_database_url(raw_url: str | URL, config: dict, workspace_id: str) -> str:
    try:
        url = make_url(str(raw_url)) if not isinstance(raw_url, URL) else raw_url
    except Exception as exc:
        raise ValueError("数据库连接字符串无效") from exc
    backend = url.get_backend_name().lower()
    supported = {
        "sqlite": {"sqlite"},
        "postgresql": {"postgresql", "postgresql+psycopg2"},
        "mysql": {"mysql", "mysql+pymysql"},
        "mssql": {"mssql", "mssql+pyodbc"},
    }
    if backend not in supported or url.drivername not in supported[backend]:
        raise ValueError("数据库驱动必须是 SQLite、psycopg2、PyMySQL 或 pyodbc")
    if backend == "sqlite":
        path = _checked_sqlite_path(url, workspace_id)
        return URL.create("sqlite", database=path).render_as_string(hide_password=False)
    if not url.host:
        raise ValueError("数据库连接必须包含主机名")
    default_ports = {"postgresql": 5432, "mysql": 3306, "mssql": 1433}
    port = int(url.port or default_ports[backend])
    if not 1 <= port <= 65535:
        raise ValueError("数据库端口必须在 1-65535 之间")
    production = settings().environment == "production"
    database_allowlist = {
        item.strip().lower().rstrip(".")
        for item in os.getenv("MERIDIAN_DATABASE_HOST_ALLOWLIST", "").split(",")
        if item.strip()
    }
    if production and not database_allowlist:
        raise ValueError("生产环境连接数据库前必须配置 MERIDIAN_DATABASE_HOST_ALLOWLIST")
    validate_outbound_host(
        str(url.host), port, allowlist=database_allowlist,
        allow_private=os.getenv("MERIDIAN_DATABASE_ALLOW_PRIVATE_NETWORK", "0") == "1",
    )
    query = {str(key): str(value) for key, value in url.query.items()}
    if "odbc_connect" in {key.lower() for key in query}:
        raise ValueError("不允许使用可绕过安全配置的 odbc_connect 参数")
    connect_timeout = max(1, min(int(config.get("connect_timeout") or 10), 60))
    drivername = {
        "postgresql": "postgresql+psycopg2",
        "mysql": "mysql+pymysql",
        "mssql": "mssql+pyodbc",
    }[backend]
    if backend == "postgresql":
        ssl_mode = str(config.get("ssl_mode") or query.get("sslmode") or ("verify-full" if production else "prefer")).lower()
        if ssl_mode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
            raise ValueError("PostgreSQL ssl_mode 无效")
        if production and ssl_mode not in {"verify-ca", "verify-full"}:
            raise ValueError("生产环境 PostgreSQL 必须验证 TLS 证书")
        query = {key: value for key, value in query.items() if key.lower() not in {"sslmode", "connect_timeout"}}
        query.update({"sslmode": ssl_mode, "connect_timeout": str(connect_timeout)})
    elif backend == "mysql":
        ssl_mode = str(config.get("ssl_mode") or ("verify-identity" if production else "preferred")).lower()
        if ssl_mode not in {"disabled", "preferred", "required", "verify-ca", "verify-identity"}:
            raise ValueError("MySQL ssl_mode 无效")
        if production and ssl_mode != "verify-identity":
            raise ValueError("生产环境 MySQL 必须验证 TLS 证书和主机名")
        query = {
            key: value for key, value in query.items()
            if key.lower() not in {"connect_timeout", "ssl_disabled", "ssl_verify_cert", "ssl_verify_identity"}
        }
        query["connect_timeout"] = str(connect_timeout)
        if ssl_mode == "disabled":
            query["ssl_disabled"] = "true"
        elif ssl_mode != "preferred":
            query["ssl_verify_cert"] = "true" if ssl_mode in {"verify-ca", "verify-identity"} else "false"
            query["ssl_verify_identity"] = "true" if ssl_mode == "verify-identity" else "false"
    else:
        query = {
            key: value for key, value in query.items()
            if key.lower().replace(" ", "") not in {
                "driver", "encrypt", "trustservercertificate", "connectiontimeout", "logintimeout",
            }
        }
        query.update({
            "driver": "ODBC Driver 18 for SQL Server",
            "Encrypt": "yes",
            "TrustServerCertificate": "no",
            "Connection Timeout": str(connect_timeout),
        })
    return url.set(drivername=drivername, port=port, query=query).render_as_string(hide_password=False)


def _build_database_url(config: dict, workspace_id: str) -> str:
    if config.get("url"):
        return _harden_database_url(str(config["url"]), config, workspace_id)
    driver = str(config.get("driver", "sqlite")).lower()
    dialects = {
        "postgresql": "postgresql+psycopg2", "postgres": "postgresql+psycopg2",
        "mysql": "mysql+pymysql", "sqlserver": "mssql+pyodbc", "mssql": "mssql+pyodbc",
    }
    if driver == "sqlite":
        raw = URL.create("sqlite", database=str(config.get("database") or ""))
    elif driver in dialects:
        raw = URL.create(
            dialects[driver], username=str(config.get("username") or "") or None,
            password=str(config.get("password") or "") or None, host=str(config.get("host") or ""),
            port=int(config["port"]) if config.get("port") else None,
            database=str(config.get("database") or "") or None,
        )
    else:
        raise ValueError("数据库类型必须是 SQLite、PostgreSQL、MySQL 或 SQL Server")
    return _harden_database_url(raw, config, workspace_id)


def register_database(config: dict, workspace_id: str) -> dict:
    url = _build_database_url(config, workspace_id)
    engine = _database_engine(url)
    schema_name = str(config.get("schema") or "").strip()[:128] or None
    tables = []
    try:
        with engine.connect() as connection:
            _configure_read_only(connection, settings().query_timeout_seconds)
            connection.execute(text("SELECT 1"))
            inspector = inspect(connection)
            objects = [(name, "table") for name in inspector.get_table_names(schema=schema_name)]
            objects.extend((name, "view") for name in inspector.get_view_names(schema=schema_name))
            for name, object_type in objects[:200]:
                columns = inspector.get_columns(name, schema=schema_name)
                tables.append({
                    "name": name, "source_name": name, "schema_name": schema_name,
                    "object_type": object_type, "columns": len(columns),
                    "schema": [
                        {"name": col["name"], "type": str(col["type"]), "nullable": bool(col.get("nullable", True))}
                        for col in columns
                    ],
                })
    finally:
        engine.dispose()
    source_id = db().new_id("src")
    vault = SecretVault(current_app.config["VAULT_KEY"])
    parsed = make_url(url)
    record = db().put(
        "sources",
        {
            "id": source_id,
            "workspace_id": workspace_id,
            "name": str(config.get("name") or config.get("database") or parsed.host or "SQL 数据源"),
            "kind": "database",
            "driver": str(config.get("driver") or parsed.get_backend_name()),
            "endpoint": parsed.host or "local",
            "schema_name": schema_name,
            "credential": vault.seal({"url": url}),
            "tables": tables,
            "status": "ready",
            "last_refreshed_at": utcnow(),
        },
        workspace_id=workspace_id,
    )
    return public_source(record)


def register_http(config: dict, workspace_id: str) -> dict:
    url = validate_outbound_url(str(config.get("url") or "").strip())
    headers = config.get("headers") if isinstance(config.get("headers"), dict) else {}
    auth_type = str(config.get("auth_type") or "none").strip().lower()
    auth_value = str(config.get("auth_value") or "").strip()
    if auth_type not in {"none", "bearer", "api_key"}:
        raise ValueError("认证方式无效，支持: none / bearer / api_key")
    if auth_type == "bearer" and auth_value:
        headers = {**headers, "Authorization": f"Bearer {auth_value}"}
    elif auth_type == "api_key" and auth_value:
        headers = {**headers, "X-API-Key": auth_value}
    json_path = str(config.get("json_path") or "").strip()
    pagination = _http_pagination(config)
    frame, page_meta = _load_http_dataset(url, headers, json_path, pagination)
    if frame.empty:
        raise ValueError("API 响应解析后为空，无法加载数据")
    _validate_frame_limits({"data": frame})
    source_id = db().new_id("src")
    cache_path = settings().upload_dir / f"{source_id}.json"
    frame.to_json(cache_path, orient="records", force_ascii=False)
    vault = SecretVault(current_app.config["VAULT_KEY"])
    record = db().put(
        "sources",
        {
            "id": source_id,
            "workspace_id": workspace_id,
            "name": str(config.get("name") or urlparse(url).netloc),
            "kind": "http",
            "format": "json",
            "endpoint": url,
            "credential": vault.seal({
                "headers": headers, "json_path": json_path,
                "auth_type": auth_type, "auth_value": auth_value,
                "pagination": pagination,
            }),
            "path": str(cache_path),
            "tables": [{"name": "data", "source_name": "data", "rows": len(frame), "columns": len(frame.columns)}],
            "status": "ready",
            "ingestion_completeness": "complete" if page_meta["complete"] else "partial",
            "pagination": page_meta,
            "last_refreshed_at": utcnow(),
        },
        workspace_id=workspace_id,
    )
    return public_source(record)


def _flatten_http_json(payload: Any, json_path: str = "") -> pd.DataFrame:
    data = payload
    for part in filter(None, json_path.split(".")):
        data = data[int(part)] if isinstance(data, list) and part.isdigit() else data[part]
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list) and value:
                data = value
                break
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError(f"无法将 {type(data).__name__} 格式的 API 响应转换为表格")
    return pd.json_normalize(data)


def _json_path_value(payload: Any, path: str) -> Any:
    value = payload
    for part in filter(None, str(path or "").split(".")):
        if isinstance(value, list) and part.isdigit():
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def _http_pagination(config: dict) -> dict[str, Any]:
    raw = config.get("pagination")
    value = dict(raw) if isinstance(raw, dict) else {}
    mode = str(value.get("mode") or config.get("pagination_mode") or "none").lower()
    if mode not in {"none", "page", "cursor", "next_url"}:
        raise ValueError("API 分页模式仅支持 none、page、cursor 或 next_url")
    return {
        "mode": mode,
        "page_param": str(value.get("page_param") or "page")[:100],
        "start_page": max(0, int(value.get("start_page", 1))),
        "page_size_param": str(value.get("page_size_param") or "")[:100],
        "page_size": max(0, min(int(value.get("page_size") or 0), 10_000)),
        "cursor_param": str(value.get("cursor_param") or "cursor")[:100],
        "next_cursor_path": str(value.get("next_cursor_path") or "next_cursor")[:500],
        "next_url_path": str(value.get("next_url_path") or "next")[:500],
        "max_pages": max(1, min(int(value.get("max_pages") or 100), 1000)),
        "max_records": max(
            1, min(int(value.get("max_records") or settings().max_ingest_rows), settings().max_ingest_rows),
        ),
    }


def _load_http_dataset(
    url: str, headers: dict[str, Any], json_path: str, pagination: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    frames: list[pd.DataFrame] = []
    mode = pagination["mode"]
    current_url = url
    cursor: Any = None
    page = pagination["start_page"]
    fingerprints: set[str] = set()
    complete = False
    total = 0
    for _page_index in range(pagination["max_pages"]):
        params: dict[str, Any] = {}
        if mode == "page":
            params[pagination["page_param"]] = page
            if pagination["page_size_param"] and pagination["page_size"]:
                params[pagination["page_size_param"]] = pagination["page_size"]
        elif mode == "cursor" and cursor not in (None, ""):
            params[pagination["cursor_param"]] = cursor
        response = safe_http_request("GET", current_url, headers=headers, params=params, timeout=20)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        raw_text = response.text.strip()
        payload = None
        if "csv" in content_type or (raw_text and not raw_text.startswith(("{", "["))):
            try:
                current = pd.read_csv(io.StringIO(response.text))
            except Exception:
                payload = response.json()
                current = _flatten_http_json(payload, json_path)
        else:
            payload = response.json()
            current = _flatten_http_json(payload, json_path)
        response_bytes = getattr(response, "content", None)
        if response_bytes is None:
            response_bytes = response.text.encode("utf-8")
        fingerprint = hashlib.sha256(response_bytes).hexdigest()
        if fingerprint in fingerprints and mode != "none":
            raise ValueError("API 分页返回重复页面，已停止以避免无限循环")
        fingerprints.add(fingerprint)
        remaining = pagination["max_records"] - total
        if remaining <= 0:
            break
        frames.append(current.head(remaining))
        total += min(len(current), remaining)
        if mode == "none":
            complete = True
            break
        if current.empty:
            complete = True
            break
        if total >= pagination["max_records"]:
            break
        if mode == "page":
            if pagination["page_size"] and len(current) < pagination["page_size"]:
                complete = True
                break
            page += 1
        elif mode == "cursor":
            next_cursor = _json_path_value(payload, pagination["next_cursor_path"])
            if next_cursor in (None, ""):
                complete = True
                break
            if str(next_cursor) == str(cursor):
                raise ValueError("API 分页游标没有前进")
            cursor = next_cursor
        else:
            next_url = _json_path_value(payload, pagination["next_url_path"])
            if not next_url:
                complete = True
                break
            current_url = validate_outbound_url(str(next_url))
    frame = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    return frame, {
        "mode": mode, "pages_fetched": len(frames), "records_fetched": len(frame),
        "complete": complete, "limit_reason": None if complete else "configured_page_or_record_limit",
    }


def register_google_sheet(config: dict, workspace_id: str) -> dict:
    creds = config.get("creds_dict") or config.get("credentials")
    if not creds and config.get("creds_json"):
        try:
            creds = json.loads(config["creds_json"]) if isinstance(config["creds_json"], str) else config["creds_json"]
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("服务账号 JSON 格式无效") from exc
    if creds:
        return _register_google_service_account(config, workspace_id, creds)
    value = str(config.get("spreadsheet_id") or config.get("url") or "").strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", value)
    spreadsheet_id = match.group(1) if match else value
    if not re.fullmatch(r"[a-zA-Z0-9_-]{12,}", spreadsheet_id):
        raise ValueError("请输入有效的 Google Sheets 链接或 Spreadsheet ID")
    gid = str(config.get("gid") or "0")
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={quote_plus(gid)}"
    response = safe_http_request("GET", url, timeout=30)
    response.raise_for_status()
    frame = pd.read_csv(io.BytesIO(response.content))
    source_id = db().new_id("src")
    path = settings().upload_dir / f"{source_id}.csv"
    frame.to_csv(path, index=False)
    record = db().put(
        "sources",
        {
            "id": source_id, "workspace_id": workspace_id,
            "name": str(config.get("name") or "Google Sheet")[:120],
            "kind": "google_sheet", "format": "csv", "endpoint": url, "path": str(path),
            "tables": [{"name": "data", "source_name": "data", "rows": len(frame), "columns": len(frame.columns)}],
            "status": "ready", "last_refreshed_at": utcnow(),
        },
        workspace_id=workspace_id,
    )
    return public_source(record)


def _google_frame(rows: list[list[Any]]) -> pd.DataFrame | None:
    if len(rows) < 2:
        return None
    # Tolerate title or preamble rows and choose the
    # first dense row as the header from the initial sample.
    sample = rows[:20]
    width = max((sum(value not in (None, "") for value in row) for row in sample), default=0)
    header_index = next(
        (index for index, row in enumerate(sample) if sum(value not in (None, "") for value in row) >= max(1, width)),
        0,
    )
    header = [str(value).strip() for value in rows[header_index]]
    used: dict[str, int] = {}
    columns = []
    for index, value in enumerate(header, 1):
        base = _sanitize_table_name(value, f"column_{index}")
        used[base] = used.get(base, 0) + 1
        columns.append(base if used[base] == 1 else f"{base}_{used[base]}")
    data = [list(row[:len(columns)]) + [None] * max(0, len(columns) - len(row)) for row in rows[header_index + 1:]]
    frame = pd.DataFrame(data, columns=columns).replace("", pd.NA).dropna(how="all")
    return None if frame.empty else frame


def _register_google_service_account(config: dict, workspace_id: str, creds: dict) -> dict:
    if not isinstance(creds, dict):
        raise ValueError("服务账号 JSON 必须是对象")
    creds = dict(creds)
    spreadsheet = str(
        config.get("spreadsheet") or config.get("spreadsheet_id") or config.get("url") or ""
    ).strip()
    if not spreadsheet:
        raise ValueError("电子表格 URL 或 ID 不能为空")
    token_uri = str(creds.get("token_uri") or "https://oauth2.googleapis.com/token")
    if token_uri != "https://oauth2.googleapis.com/token":  # noqa: S105 -- official endpoint, not a token
        raise ValueError("Google 服务账号 token_uri 必须使用官方 HTTPS 端点")
    creds["token_uri"] = token_uri
    if spreadsheet.startswith("http"):
        parsed = urlparse(spreadsheet)
        if parsed.scheme != "https" or parsed.hostname != "docs.google.com" or "/spreadsheets/d/" not in parsed.path:
            raise ValueError("Google Sheets URL 必须使用 docs.google.com 官方 HTTPS 地址")
    elif not re.fullmatch(r"[a-zA-Z0-9_-]{12,}", spreadsheet):
        raise ValueError("Google Spreadsheet ID 格式无效")
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise ValueError("服务账号连接需要 gspread 和 google-auth") from exc

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    credentials = Credentials.from_service_account_info(creds, scopes=scopes)
    client = gspread.authorize(credentials)
    if hasattr(client, "set_timeout"):
        client.set_timeout(20)
    document = client.open_by_url(spreadsheet) if spreadsheet.startswith("http") else client.open_by_key(spreadsheet)
    frames: dict[str, pd.DataFrame] = {}
    for worksheet in document.worksheets():
        row_limit = settings().max_ingest_rows
        if int(getattr(worksheet, "row_count", 0) or 0) > row_limit:
            raise ValueError(f"Google 工作表 {worksheet.title} 超过 {row_limit} 行导入上限")
        if (
            int(getattr(worksheet, "row_count", 0) or 0)
            * int(getattr(worksheet, "col_count", 0) or 0) > settings().max_ingest_cells
        ):
            raise ValueError(
                f"Google 工作表 {worksheet.title} 超过 "
                f"{settings().max_ingest_cells} 个单元格导入上限",
            )
        frame = _google_frame(worksheet.get_all_values())
        if frame is not None:
            frames[str(worksheet.title)] = frame
    if not frames:
        raise ValueError("Google Spreadsheet 中未发现有效工作表")
    _validate_frame_limits(frames)

    source_id = db().new_id("src")
    path = settings().upload_dir / f"{source_id}.xlsx"
    tables = []
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for index, (name, frame) in enumerate(frames.items(), 1):
            sheet = re.sub(r"[\\/*?:\[\]]", "_", name).strip()[:31] or f"sheet_{index}"
            frame.to_excel(writer, sheet_name=sheet, index=False)
            tables.append({
                "name": _sanitize_table_name(sheet), "source_name": sheet,
                "rows": int(len(frame)), "columns": int(len(frame.columns)),
            })
    secret = {"creds_dict": creds, "spreadsheet": spreadsheet}
    record = db().put(
        "sources",
        {
            "id": source_id, "workspace_id": workspace_id,
            "name": str(config.get("name") or getattr(document, "title", "Google Sheets"))[:120],
            "kind": "google_sheet", "format": "xlsx", "endpoint": spreadsheet,
            "credential": SecretVault(current_app.config["VAULT_KEY"]).seal(secret),
            "path": str(path), "tables": tables, "service_account": True,
            "status": "ready", "last_refreshed_at": utcnow(),
        },
        workspace_id=workspace_id,
    )
    return public_source(record)


def _lark_records(secret: dict) -> pd.DataFrame:
    token_response = safe_http_request(
        "POST",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": secret["app_id"], "app_secret": secret["app_secret"]}, timeout=20,
    )
    token_response.raise_for_status()
    token_payload = token_response.json()
    token = token_payload.get("tenant_access_token")
    if not token:
        raise ValueError(token_payload.get("msg") or "无法获取飞书访问令牌")
    headers = {"Authorization": f"Bearer {token}"}
    records = []
    page_token = ""
    while True:
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{secret['app_token']}/tables/{secret['table_id']}/records?page_size=500"
        if page_token:
            url += f"&page_token={quote_plus(page_token)}"
        response = safe_http_request("GET", url, headers=headers, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (None, 0):
            raise ValueError(payload.get("msg") or "读取多维表格失败")
        data = payload.get("data", {})
        records.extend({"record_id": item.get("record_id"), **(item.get("fields") or {})} for item in data.get("items", []))
        if len(records) > settings().max_ingest_rows:
            raise ValueError(f"飞书多维表格超过 {settings().max_ingest_rows} 行导入上限")
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
    frame = pd.json_normalize(records)
    _validate_frame_limits({"data": frame})
    return frame


def register_lark_table(config: dict, workspace_id: str) -> dict:
    required = ["app_id", "app_secret", "app_token", "table_id"]
    if any(not config.get(key) for key in required):
        raise ValueError("飞书多维表格连接需要 app_id、app_secret、app_token 和 table_id")
    secret = {key: str(config[key]) for key in required}
    frame = _lark_records(secret)
    source_id = db().new_id("src")
    path = settings().upload_dir / f"{source_id}.json"
    frame.to_json(path, orient="records", force_ascii=False)
    record = db().put(
        "sources",
        {
            "id": source_id, "workspace_id": workspace_id,
            "name": str(config.get("name") or "协作表格")[:120], "kind": "lark_table", "format": "json",
            "endpoint": f"bitable:{secret['app_token']}:{secret['table_id']}",
            "credential": SecretVault(current_app.config["VAULT_KEY"]).seal(secret), "path": str(path),
            "tables": [{"name": "data", "source_name": "data", "rows": len(frame), "columns": len(frame.columns)}],
            "status": "ready", "last_refreshed_at": utcnow(),
        },
        workspace_id=workspace_id,
    )
    return public_source(record)


def public_source(source: dict) -> dict:
    value = dict(source)
    value.pop("credential", None)
    value.pop("path", None)
    return value


def register_derived_tables(
    frames: dict[str, pd.DataFrame], workspace_id: str, *, name: str = "分析结果",
    source_ids: list[str] | None = None, actor_id: str = "local-default",
) -> dict:
    if not frames:
        raise ValueError("没有可保存的分析结果表")
    from .authorization import inherited_sources_policy, require_sources_access

    parent_ids = list(dict.fromkeys(str(value) for value in source_ids or [] if value))
    parents = require_sources_access(
        db(), parent_ids, workspace_id=workspace_id, actor_id=actor_id, action="analyze",
    )
    source_id = db().new_id("src")
    path = settings().upload_dir / f"{source_id}.xlsx"
    used: set[str] = set()
    tables = []
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for index, (raw_name, frame) in enumerate(frames.items(), 1):
            if not len(frame.columns):
                continue
            sheet = re.sub(r"[\\/*?:\[\]]", "_", str(raw_name)).strip()[:31] or f"table_{index}"
            base, counter = sheet, 2
            while sheet in used:
                suffix = f"_{counter}"
                sheet = f"{base[:31 - len(suffix)]}{suffix}"
                counter += 1
            used.add(sheet)
            frame.to_excel(writer, sheet_name=sheet, index=False)
            tables.append({
                "name": _sanitize_table_name(sheet), "source_name": sheet,
                "rows": int(len(frame)), "columns": int(len(frame.columns)),
            })
        if not tables:
            raise ValueError("分析结果表没有可保存的字段")
    record = db().put(
        "sources",
        {
            "id": source_id, "workspace_id": workspace_id, "name": str(name)[:120],
            "kind": "derived", "format": "xlsx", "path": str(path), "tables": tables,
            "status": "ready", "last_refreshed_at": utcnow(),
            "lineage": {"operation": "derived_analysis", "source_ids": parent_ids},
            **inherited_sources_policy(parents),
        },
        workspace_id=workspace_id,
    )
    db().audit(
        "analysis.tables_created", workspace_id=workspace_id,
        object_type="source", object_id=source_id,
        actor=actor_id,
        detail={"tables": [table["name"] for table in tables], "source_ids": parent_ids},
    )
    return record


def delete_derived_tables(
    table_names: list[str], workspace_id: str, *, actor_id: str = "local-default",
) -> dict:
    """Remove exact tables from derived workbooks while protecting raw sources."""
    requested = {str(value).strip() for value in table_names if str(value).strip()}
    if not requested:
        raise ValueError("请提供至少一个要删除的分析表名")
    deleted: list[str] = []
    archived_sources: list[str] = []
    updated_sources: list[str] = []
    for source in db().list("sources", workspace_id=workspace_id, limit=5000):
        if source.get("kind") != "derived":
            continue
        try:
            require_sources_access(
                db(), [source["id"]], workspace_id=workspace_id,
                actor_id=actor_id, action="delete",
            )
        except PermissionError:
            continue
        tables = list(source.get("tables") or [])
        matched = [
            table for table in tables
            if str(table.get("name") or "") in requested or str(table.get("source_name") or "") in requested
        ]
        if not matched:
            continue
        matched_names = {
            str(value) for table in matched for value in (table.get("name"), table.get("source_name")) if value
        }
        remaining = [table for table in tables if table not in matched]
        deleted.extend(str(table.get("name") or table.get("source_name")) for table in matched)
        if not remaining:
            db().archive("sources", source["id"], workspace_id=workspace_id)
            archived_sources.append(source["id"])
            continue
        frames = source_frames(source)
        remaining_frames = {
            str(table["source_name"]): frames[str(table["source_name"])]
            for table in remaining
            if str(table.get("source_name") or "") in frames
        }
        if len(remaining_frames) != len(remaining):
            raise RuntimeError(f"分析表存储与元数据不一致：{source['id']}")
        path = Path(str(source.get("path") or ""))
        temporary = path.with_name(f".{path.stem}.{db().new_id('rewrite')}.xlsx")
        try:
            with pd.ExcelWriter(temporary, engine="openpyxl") as writer:
                for sheet, frame in remaining_frames.items():
                    frame.to_excel(writer, sheet_name=sheet[:31], index=False)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        db().patch("sources", source["id"], {"tables": remaining}, workspace_id=workspace_id)
        updated_sources.append(source["id"])
        requested -= matched_names
    missing = sorted(requested)
    db().audit(
        "analysis.tables_deleted", workspace_id=workspace_id, object_type="derived_table",
        detail={
            "deleted": deleted, "missing": missing,
            "archived_sources": archived_sources, "updated_sources": updated_sources,
        },
    )
    return {
        "deleted": deleted, "deleted_tables": deleted, "missing": missing,
        "archived_sources": archived_sources, "updated_sources": updated_sources,
    }


def source_frames(source: dict) -> dict[str, pd.DataFrame]:
    if source["kind"] in {
        "file", "http", "derived", "workspace", "google_sheet", "lark_table", "lark_table_snapshot",
    }:
        return _read_tabular_file(Path(source["path"]))
    if source["kind"] in {"database", "warehouse"}:
        raise ValueError("远程数据源不允许经 source_frames 隐式拉取；请通过只读 SQL 下推并使用受控结果引用")
    raise ValueError("未知数据源类型")


def source_table(source: dict, table_name: str | None = None) -> tuple[str, pd.DataFrame]:
    if source.get("kind") in {"database", "warehouse"}:
        raise ValueError("远程数据源不允许经 source_table 转为 DataFrame；请先执行有范围的只读查询")
    frames = source_frames(source)
    if not frames:
        raise ValueError("数据源中没有可分析的数据表")
    if table_name:
        for source_name, frame in frames.items():
            if source_name == table_name or _sanitize_table_name(source_name) == table_name:
                return _sanitize_table_name(source_name), frame
        raise ValueError(f"数据表不存在：{table_name}")
    name, frame = next(iter(frames.items()))
    return _sanitize_table_name(name), frame


def schema_for_source(source: dict) -> dict:
    if source.get("kind") == "warehouse":
        return {
            "source_id": source["id"], "engine_id": source.get("engine_id"),
            "catalog": source.get("catalog"), "schema": source.get("schema"),
            "lazy": True, "tables": [],
            "catalog_url": f"/api/warehouse/engines/{source.get('engine_id')}/catalog",
        }
    if source.get("kind") == "database":
        return {
            "source_id": source["id"],
            "tables": [
                {
                    "name": table["name"], "source_name": table.get("source_name", table["name"]),
                    "rows": table.get("rows"),
                    "columns": [
                        {
                            "name": column["name"], "type": column.get("type", "unknown"),
                            "nullable": bool(column.get("nullable", True)), "distinct": None, "sample": [],
                        }
                        for column in table.get("schema", [])
                    ],
                }
                for table in source.get("tables", [])
            ],
        }
    tables = []
    for name, frame in source_frames(source).items():
        columns = []
        for column in frame.columns:
            series = frame[column]
            columns.append({
                "name": str(column),
                "type": str(series.dtype),
                "nullable": bool(series.isna().any()),
                "distinct": int(series.nunique(dropna=True)),
                "sample": [_json_safe(value) for value in series.dropna().head(3).tolist()],
            })
        tables.append({
            "name": _sanitize_table_name(name),
            "source_name": str(name),
            "rows": int(len(frame)),
            "columns": columns,
        })
    return {"source_id": source["id"], "tables": tables}


def preview_source(source: dict, table_name: str | None = None, limit: int = 100) -> dict:
    name, frame = source_table(source, table_name)
    return {
        "source_id": source["id"],
        "table": name,
        "rows": int(len(frame)),
        "columns": [str(column) for column in frame.columns],
        "data": frame_records(frame, limit),
    }


def _query_table_scope(sources: list[dict]) -> set[str]:
    allowed: set[str] = set()
    if len(sources) == 1 and sources[0].get("kind") == "database":
        source = sources[0]
        selected = {str(value) for value in source.get("analysis_tables") or [] if str(value)}
        for table in source.get("tables") or []:
            name = str(table.get("source_name") or table.get("name") or "").strip()
            if selected and name not in selected and str(table.get("name") or "") not in selected:
                continue
            schema_name = str(table.get("schema_name") or source.get("schema_name") or "").strip()
            if name:
                allowed.add(name.lower())
                if schema_name:
                    allowed.add(f"{schema_name}.{name}".lower())
        return allowed
    used: set[str] = set()
    for source in sources:
        for table_name in source_frames(source):
            base = _sanitize_table_name(table_name)
            name = base if base not in used else _sanitize_table_name(f"{source['name']}_{base}")
            used.add(name)
            allowed.add(name.lower())
    return allowed


def _enforce_result_size(frame: pd.DataFrame) -> None:
    max_cell = settings().max_query_cell_bytes
    for column in frame.select_dtypes(include=["object", "string"]).columns:
        oversized = frame[column].dropna().map(
            lambda value: len(str(value).encode("utf-8", errors="replace")) > max_cell,
        )
        if bool(oversized.any()):
            raise ValueError("查询结果包含超过单元格安全上限的内容")
    if int(frame.memory_usage(index=True, deep=True).sum()) > settings().max_query_bytes:
        raise ValueError("查询结果超过服务端字节上限，请先聚合或缩小范围")


def execute_query(
    source_ids: list[str], sql: str, workspace_id: str, limit: int = 1000, *,
    actor_id: str = "local-default",
) -> dict:
    source_ids = list(dict.fromkeys(str(value) for value in source_ids if str(value)))
    if not source_ids:
        raise ValueError("请选择数据源")
    sources = require_sources_access(
        db(), source_ids, workspace_id=workspace_id, actor_id=actor_id, action="query",
    )
    if len(sources) > 1 and any(source.get("kind") == "database" for source in sources):
        raise ValueError("数据库数据源不能与其他数据源直接联邦查询；请先生成受控快照后再关联")
    limit = max(1, min(int(limit), settings().max_query_rows))
    dialect = None
    engine = None
    if len(sources) == 1 and sources[0]["kind"] == "database":
        vault = SecretVault(current_app.config["VAULT_KEY"])
        url = vault.open(sources[0].get("credential", ""), {}).get("url")
        engine = _database_engine(url)
        dialect = _dialect_name(engine)
    try:
        statement = validate_read_only_sql(sql, dialect)
        referenced_tables = validate_query_tables(statement, _query_table_scope(sources), dialect)
    except Exception:
        if engine is not None:
            engine.dispose()
        raise
    parsed = sqlglot.parse_one(statement, read=dialect)
    raw_limit = parsed.args.get("limit")
    requested_top_n = None
    if raw_limit and isinstance(raw_limit.expression, sql_exp.Literal) and not raw_limit.expression.is_string:
        try:
            requested_top_n = int(raw_limit.expression.this)
        except (TypeError, ValueError):
            requested_top_n = None
    probe_limit = limit if requested_top_n is not None and requested_top_n <= limit else limit + 1
    # A single database source executes on its native engine to preserve dialect.
    if len(sources) == 1 and sources[0]["kind"] == "database":
        source = sources[0]
        if engine is None:
            raise RuntimeError("数据库查询引擎未初始化")
        try:
            with engine.connect() as connection:
                _configure_read_only(connection, settings().query_timeout_seconds)
                bounded = bounded_read_only_sql(statement, probe_limit, _dialect_name(engine))
                frame = pd.read_sql(text(bounded), connection)
        finally:
            engine.dispose()
    else:
        connection = duckdb.connect(database=":memory:")
        try:
            connection.execute("SET enable_external_access=false")
            connection.execute("SET memory_limit='1GB'")
            connection.execute("SET threads=2")
            used: set[str] = set()
            for source in sources:
                for table_name, frame in source_frames(source).items():
                    base = _sanitize_table_name(table_name)
                    name = base
                    if name in used:
                        name = _sanitize_table_name(f"{source['name']}_{base}")
                    used.add(name)
                    connection.register(name, frame)
            bounded = bounded_read_only_sql(statement, probe_limit, "duckdb")
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="meridian-query")
            future = executor.submit(lambda: connection.execute(bounded).fetchdf())
            try:
                frame = future.result(timeout=settings().query_timeout_seconds)
            except FutureTimeoutError as exc:
                connection.interrupt()
                raise ValueError("查询执行时间超过限制") from exc
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
        finally:
            connection.close()
    # The extra probe row is private evidence that the server bound, including an
    # oversized user LIMIT, changed the requested result. Never publish that row.
    system_truncated = len(frame) > limit
    if system_truncated:
        frame = frame.head(limit).copy()
    _enforce_result_size(frame)
    source_partial = any(source.get("ingestion_completeness") == "partial" for source in sources)
    completeness = "system_truncated" if system_truncated else "source_partial" if source_partial else "complete"
    query_id = db().new_id("qry")
    result_path = settings().export_dir / f"{query_id}.csv"
    frame.to_csv(result_path, index=False)
    if result_path.stat().st_size > settings().max_query_bytes:
        result_path.unlink(missing_ok=True)
        raise ValueError("查询结果超过服务端字节上限，请先聚合或缩小范围")
    result = db().put(
        "query_results",
        {
            "id": query_id,
            "workspace_id": workspace_id,
            "source_ids": source_ids,
            "actor_id": actor_id,
            "sql": statement,
            "referenced_tables": referenced_tables,
            "encoded_bytes": result_path.stat().st_size,
            "rows": int(len(frame)),
            "returned_rows": int(len(frame)),
            "total_rows": int(len(frame)) if completeness == "complete" else None,
            "completeness": completeness,
            "accuracy": "exact",
            "user_top_n": requested_top_n,
            "columns": [str(column) for column in frame.columns],
            "data": frame_records(frame, 300),
            "path": str(result_path),
        },
        workspace_id=workspace_id,
    )
    db().audit(
        "query.executed", workspace_id=workspace_id, actor=actor_id,
        object_type="query", object_id=query_id,
        detail={"sql": statement, "rows": len(frame), "tables": referenced_tables},
    )
    return result


def load_result_frame(result_id: str) -> pd.DataFrame:
    result = db().get("query_results", result_id)
    if not result:
        raise ValueError("查询结果不存在")
    if result.get("dataset_ref_kind") in {"trino_query", "iceberg_table", "spark_output", "object_prefix"}:
        raise ValueError("远程结果引用不得隐式转为 DataFrame")
    path = Path(str(result.get("path") or ""))
    if not path.is_file():
        raise ValueError("查询结果文件不存在")
    max_bytes = min(int(settings().max_upload_bytes), 50 * 1024 * 1024)
    if path.stat().st_size > max_bytes or int(result.get("rows") or 0) > settings().max_query_rows:
        raise ValueError("结果超过本地完整读取门禁，请在仓内继续计算")
    return pd.read_csv(path)


def refresh_source(source: dict) -> dict:
    if source["kind"] == "http":
        vault = SecretVault(current_app.config["VAULT_KEY"])
        secret = vault.open(source.get("credential", ""), {})
        frame, page_meta = _load_http_dataset(
            source["endpoint"], secret.get("headers", {}), str(secret.get("json_path", "")),
            _http_pagination({"pagination": secret.get("pagination") or {"mode": "none"}}),
        )
        _validate_frame_limits({"data": frame})
        frame.to_json(source["path"], orient="records", force_ascii=False)
        source["ingestion_completeness"] = "complete" if page_meta["complete"] else "partial"
        source["pagination"] = page_meta
    elif source["kind"] == "google_sheet":
        secret = SecretVault(current_app.config["VAULT_KEY"]).open(source.get("credential", ""), {})
        if secret.get("creds_dict"):
            replacement = _register_google_service_account(
                {
                    "creds_dict": secret["creds_dict"], "spreadsheet": secret["spreadsheet"],
                    "name": source.get("name"),
                },
                source.get("workspace_id", "default"),
            )
            generated = db().get("sources", replacement["id"])
            Path(source["path"]).unlink(missing_ok=True)
            Path(generated["path"]).replace(source["path"])
            db().delete("sources", generated["id"])
        else:
            response = safe_http_request("GET", source["endpoint"], timeout=30)
            response.raise_for_status()
            pd.read_csv(io.BytesIO(response.content)).to_csv(source["path"], index=False)
    elif source["kind"] == "lark_table":
        vault = SecretVault(current_app.config["VAULT_KEY"])
        frame = _lark_records(vault.open(source.get("credential", ""), {}))
        frame.to_json(source["path"], orient="records", force_ascii=False)
    schema = schema_for_source(source)
    source["tables"] = [
        {"name": table["name"], "source_name": table["source_name"], "rows": table["rows"], "columns": len(table["columns"])}
        for table in schema["tables"]
    ]
    source["last_refreshed_at"] = utcnow()
    return db().put("sources", source, workspace_id=source.get("workspace_id", "default"))
