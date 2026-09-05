from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import duckdb
import numpy as np
import pandas as pd
import requests
from flask import current_app
from sqlalchemy import create_engine, inspect, text
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from ..core.database import Database, utcnow
from .security import SecretVault, safe_http_request, validate_outbound_url
from .sql_security import validate_read_only_sql


SUPPORTED_FILE_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".parquet"}


def db() -> Database:
    return current_app.extensions["meridian_db"]


def settings():
    return current_app.config["SETTINGS"]


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
        frames = _read_tabular_file(target)
        tables = []
        for name, frame in frames.items():
            tables.append({
                "name": _sanitize_table_name(name),
                "source_name": str(name),
                "rows": int(len(frame)),
                "columns": int(len(frame.columns)),
            })
    except Exception:
        target.unlink(missing_ok=True)
        raise
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


def _build_database_url(config: dict, workspace_id: str) -> str:
    if config.get("url"):
        url = str(config["url"])
        parsed = urlparse(url)
        driver = parsed.scheme.split("+", 1)[0].lower()
        if driver == "sqlite":
            path = Path(parsed.path).resolve()
            workspace = db().get("workspaces", workspace_id) or {}
            allowed = [settings().storage_dir.resolve()]
            if workspace.get("mounted_path"):
                allowed.append(Path(workspace["mounted_path"]).resolve())
            if not any(path == root or root in path.parents for root in allowed):
                raise ValueError("SQLite 文件必须位于工作区存储或已挂载目录内")
        elif parsed.hostname:
            validate_outbound_url(f"https://{parsed.hostname}:{parsed.port or 443}")
        return url
    driver = str(config.get("driver", "sqlite")).lower()
    if driver == "sqlite":
        path = Path(str(config.get("database") or "")).expanduser().resolve()
        workspace = db().get("workspaces", workspace_id) or {}
        allowed = [settings().storage_dir.resolve()]
        if workspace.get("mounted_path"):
            allowed.append(Path(workspace["mounted_path"]).resolve())
        if not any(path == root or root in path.parents for root in allowed):
            raise ValueError("SQLite 文件必须位于工作区存储或已挂载目录内")
        return f"sqlite:///{path}"
    dialects = {
        "postgresql": "postgresql+psycopg2",
        "postgres": "postgresql+psycopg2",
        "mysql": "mysql+pymysql",
        "sqlserver": "mssql+pyodbc",
        "mssql": "mssql+pyodbc",
    }
    dialect = dialects.get(driver)
    if not dialect:
        raise ValueError("数据库类型必须是 SQLite、PostgreSQL、MySQL 或 SQL Server")
    user = quote_plus(str(config.get("username") or ""))
    password = quote_plus(str(config.get("password") or ""))
    host = str(config.get("host") or "localhost")
    port = str(config.get("port") or "")
    database = quote_plus(str(config.get("database") or ""))
    auth = f"{user}:{password}@" if user else ""
    address = f"{host}:{port}" if port else host
    validate_outbound_url(f"https://{address}")
    suffix = "?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes" if dialect.startswith("mssql") else ""
    return f"{dialect}://{auth}{address}/{database}{suffix}"


def register_database(config: dict, workspace_id: str) -> dict:
    url = _build_database_url(config, workspace_id)
    engine = create_engine(url, pool_pre_ping=True)
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    inspector = inspect(engine)
    tables = []
    for name in inspector.get_table_names()[:200]:
        columns = inspector.get_columns(name)
        tables.append({
            "name": name,
            "source_name": name,
            "columns": len(columns),
            "schema": [{"name": col["name"], "type": str(col["type"])} for col in columns],
        })
    source_id = db().new_id("src")
    vault = SecretVault(current_app.config["SECRET_KEY"])
    parsed = urlparse(url)
    record = db().put(
        "sources",
        {
            "id": source_id,
            "workspace_id": workspace_id,
            "name": str(config.get("name") or config.get("database") or parsed.hostname or "SQL 数据源"),
            "kind": "database",
            "driver": str(config.get("driver") or parsed.scheme.split("+")[0]),
            "endpoint": parsed.hostname or "local",
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
    response = safe_http_request("GET", url, headers=headers, timeout=20)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").lower()
    json_path = str(config.get("json_path") or "").strip()
    raw_text = response.text.strip()
    if "csv" in content_type or (raw_text and not raw_text.startswith(("{", "["))):
        try:
            frame = pd.read_csv(io.StringIO(response.text))
        except Exception:
            payload = response.json()
            frame = _flatten_http_json(payload, json_path)
    else:
        frame = _flatten_http_json(response.json(), json_path)
    if frame.empty:
        raise ValueError("API 响应解析后为空，无法加载数据")
    source_id = db().new_id("src")
    cache_path = settings().upload_dir / f"{source_id}.json"
    frame.to_json(cache_path, orient="records", force_ascii=False)
    vault = SecretVault(current_app.config["SECRET_KEY"])
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
            }),
            "path": str(cache_path),
            "tables": [{"name": "data", "source_name": "data", "rows": len(frame), "columns": len(frame.columns)}],
            "status": "ready",
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
    response = requests.get(url, timeout=30)
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
    # Match the reference behavior: tolerate title/preamble rows and choose the
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
    spreadsheet = str(
        config.get("spreadsheet") or config.get("spreadsheet_id") or config.get("url") or ""
    ).strip()
    if not spreadsheet:
        raise ValueError("电子表格 URL 或 ID 不能为空")
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
        frame = _google_frame(worksheet.get_all_values())
        if frame is not None:
            frames[str(worksheet.title)] = frame
    if not frames:
        raise ValueError("Google Spreadsheet 中未发现有效工作表")

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
            "credential": SecretVault(current_app.config["SECRET_KEY"]).seal(secret),
            "path": str(path), "tables": tables, "service_account": True,
            "status": "ready", "last_refreshed_at": utcnow(),
        },
        workspace_id=workspace_id,
    )
    return public_source(record)


def _lark_records(secret: dict) -> pd.DataFrame:
    token_response = requests.post(
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
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (None, 0):
            raise ValueError(payload.get("msg") or "读取多维表格失败")
        data = payload.get("data", {})
        records.extend({"record_id": item.get("record_id"), **(item.get("fields") or {})} for item in data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
    return pd.json_normalize(records)


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
            "credential": SecretVault(current_app.config["SECRET_KEY"]).seal(secret), "path": str(path),
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
) -> dict:
    if not frames:
        raise ValueError("没有可保存的分析结果表")
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
        },
        workspace_id=workspace_id,
    )
    db().audit(
        "analysis.tables_created", workspace_id=workspace_id,
        object_type="source", object_id=source_id,
        detail={"tables": [table["name"] for table in tables]},
    )
    return record


def source_frames(source: dict) -> dict[str, pd.DataFrame]:
    if source["kind"] in {
        "file", "http", "derived", "workspace", "google_sheet", "lark_table", "lark_table_snapshot",
    }:
        return _read_tabular_file(Path(source["path"]))
    if source["kind"] == "database":
        vault = SecretVault(current_app.config["SECRET_KEY"])
        secret = vault.open(source.get("credential", ""), {})
        engine = create_engine(secret["url"], pool_pre_ping=True)
        result: dict[str, pd.DataFrame] = {}
        with engine.connect() as connection:
            selected = source.get("analysis_tables")
            candidates = source.get("tables", [])
            if isinstance(selected, list):
                selected_names = {str(value) for value in selected}
                candidates = [
                    table for table in candidates
                    if table.get("name") in selected_names or table.get("source_name") in selected_names
                ]
            for table in candidates[:50]:
                name = table["source_name"]
                quoted = engine.dialect.identifier_preparer.quote(name)
                result[table["name"]] = pd.read_sql(text(f"SELECT * FROM {quoted}"), connection)
        return result
    raise ValueError("未知数据源类型")


def source_table(source: dict, table_name: str | None = None) -> tuple[str, pd.DataFrame]:
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


def execute_query(source_ids: list[str], sql: str, workspace_id: str, limit: int = 1000) -> dict:
    statement = validate_read_only_sql(sql)
    sources = [db().get("sources", source_id) for source_id in source_ids]
    if not all(sources):
        raise ValueError("一个或多个数据源不存在")
    if any(source.get("workspace_id", "default") != workspace_id for source in sources):
        raise ValueError("一个或多个数据源不属于当前工作空间")
    # A single database source should execute on its native engine to preserve dialect.
    if len(sources) == 1 and sources[0]["kind"] == "database":
        source = sources[0]
        vault = SecretVault(current_app.config["SECRET_KEY"])
        url = vault.open(source.get("credential", ""), {}).get("url")
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as connection:
            frame = pd.read_sql(text(statement), connection).head(limit)
    else:
        connection = duckdb.connect(database=":memory:")
        try:
            connection.execute("SET enable_external_access=false")
            used: set[str] = set()
            for source in sources:
                for table_name, frame in source_frames(source).items():
                    base = _sanitize_table_name(table_name)
                    name = base
                    if name in used:
                        name = _sanitize_table_name(f"{source['name']}_{base}")
                    used.add(name)
                    connection.register(name, frame)
            frame = connection.execute(statement).fetchdf().head(limit)
        finally:
            connection.close()
    query_id = db().new_id("qry")
    result_path = settings().export_dir / f"{query_id}.csv"
    frame.to_csv(result_path, index=False)
    result = db().put(
        "query_results",
        {
            "id": query_id,
            "workspace_id": workspace_id,
            "source_ids": source_ids,
            "sql": statement,
            "rows": int(len(frame)),
            "columns": [str(column) for column in frame.columns],
            "data": frame_records(frame, 300),
            "path": str(result_path),
        },
        workspace_id=workspace_id,
    )
    db().audit("query.executed", workspace_id=workspace_id, object_type="query", object_id=query_id, detail={"sql": statement, "rows": len(frame)})
    return result


def load_result_frame(result_id: str) -> pd.DataFrame:
    result = db().get("query_results", result_id)
    if not result:
        raise ValueError("查询结果不存在")
    return pd.read_csv(result["path"])


def refresh_source(source: dict) -> dict:
    if source["kind"] == "http":
        vault = SecretVault(current_app.config["SECRET_KEY"])
        secret = vault.open(source.get("credential", ""), {})
        response = safe_http_request("GET", source["endpoint"], headers=secret.get("headers", {}), timeout=20)
        response.raise_for_status()
        data = response.json()
        for part in filter(None, str(secret.get("json_path", "")).split(".")):
            data = data[int(part)] if isinstance(data, list) and part.isdigit() else data[part]
        if isinstance(data, dict):
            data = data.get("data", data.get("items", [data]))
        frame = pd.json_normalize(data)
        frame.to_json(source["path"], orient="records", force_ascii=False)
    elif source["kind"] == "google_sheet":
        secret = SecretVault(current_app.config["SECRET_KEY"]).open(source.get("credential", ""), {})
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
        vault = SecretVault(current_app.config["SECRET_KEY"])
        frame = _lark_records(vault.open(source.get("credential", ""), {}))
        frame.to_json(source["path"], orient="records", force_ascii=False)
    schema = schema_for_source(source)
    source["tables"] = [
        {"name": table["name"], "source_name": table["source_name"], "rows": table["rows"], "columns": len(table["columns"])}
        for table in schema["tables"]
    ]
    source["last_refreshed_at"] = utcnow()
    return db().put("sources", source, workspace_id=source.get("workspace_id", "default"))
