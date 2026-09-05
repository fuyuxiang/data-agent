from __future__ import annotations

import json
from collections.abc import Iterable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .security import safe_http_request


API_ROOT = "https://open.feishu.cn/open-apis"
MAX_RECORDS = 500


def parse_bitable_reference(value: object, table_id: object = "") -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw or len(raw) > 500 or any(character.isspace() for character in raw):
        raise ValueError("请提供有效的飞书多维表格链接或 app_token")
    resolved_table = str(table_id or "").strip()
    token = raw
    if "://" in raw:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if not (
            host == "feishu.cn" or host.endswith(".feishu.cn")
            or host == "larksuite.com" or host.endswith(".larksuite.com")
        ):
            raise ValueError("只支持飞书或 Lark 多维表格链接")
        parts = [part for part in parsed.path.split("/") if part]
        try:
            token = parts[parts.index("base") + 1]
        except (ValueError, IndexError) as exc:
            raise ValueError("无法从链接识别 app_token") from exc
        resolved_table = resolved_table or (parse_qs(parsed.query).get("table") or [""])[0]
    if len(token) > 300 or not token:
        raise ValueError("app_token 格式不正确")
    return token, resolved_table


def bitable_url(app_token: str, table_id: str = "", app_url: str = "") -> str:
    parsed = urlparse(app_url or f"https://feishu.cn/base/{app_token}")
    query = parse_qs(parsed.query)
    if table_id:
        query["table"] = [table_id]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def tenant_token(credentials: dict) -> str:
    app_id = str(credentials.get("app_id") or "").strip()
    app_secret = str(credentials.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        raise ValueError("未配置飞书应用 App ID / App Secret")
    response = safe_http_request(
        "POST", f"{API_ROOT}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret}, timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("tenant_access_token")
    if payload.get("code") not in (None, 0) or not token:
        raise ConnectionError(payload.get("msg") or "无法获取飞书访问令牌")
    return str(token)


def request_feishu(
    method: str, path: str, credentials: dict, *, params: dict | None = None, payload: dict | None = None,
) -> dict:
    response = safe_http_request(
        method, f"{API_ROOT}{path}", headers={"Authorization": f"Bearer {tenant_token(credentials)}"},
        params=params, json=payload, timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("code") not in (None, 0):
        message = body.get("msg") if isinstance(body, dict) else ""
        raise ConnectionError(message or "飞书 API 请求失败，请检查应用权限和文件授权")
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def list_tables(credentials: dict, bitable: object) -> dict:
    app_token, _table_id = parse_bitable_reference(bitable)
    tables = []
    page_token = ""
    while len(tables) < MAX_RECORDS:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        data = request_feishu(
            "GET", f"/bitable/v1/apps/{app_token}/tables", credentials, params=params,
        )
        for item in data.get("items") or []:
            if isinstance(item, dict) and item.get("table_id"):
                tables.append({
                    "table_id": str(item["table_id"]), "name": str(item.get("name") or "未命名数据表"),
                    "url": bitable_url(app_token, str(item["table_id"])),
                })
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "")
        if not page_token:
            break
    return {"app_token": app_token, "tables": tables}


def _cell(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return ", ".join(str(_cell(item)) for item in value if item is not None)
    if isinstance(value, dict):
        for key in ("text", "name", "url", "link"):
            if isinstance(value.get(key), (str, int, float, bool)):
                return value[key]
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def read_records(
    credentials: dict, bitable: object, table_id: object = "", max_records: int = MAX_RECORDS,
) -> dict:
    app_token, resolved_table = parse_bitable_reference(bitable, table_id)
    if not resolved_table:
        raise ValueError("请提供 table_id，或使用包含 ?table=... 的链接")
    limit = max(1, min(int(max_records), MAX_RECORDS))
    rows = []
    page_token = ""
    has_more = False
    while len(rows) < limit:
        params = {"page_size": min(100, limit - len(rows))}
        if page_token:
            params["page_token"] = page_token
        data = request_feishu(
            "GET", f"/bitable/v1/apps/{app_token}/tables/{resolved_table}/records",
            credentials, params=params,
        )
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            row = {str(key): _cell(value) for key, value in fields.items()}
            if item.get("record_id"):
                row["_feishu_record_id"] = str(item["record_id"])
            rows.append(row)
            if len(rows) >= limit:
                break
        has_more = bool(data.get("has_more"))
        if not has_more or len(rows) >= limit:
            break
        page_token = str(data.get("page_token") or "")
        if not page_token:
            break
    return {
        "app_token": app_token, "table_id": resolved_table,
        "url": bitable_url(app_token, resolved_table), "records": rows,
        "record_count": len(rows), "limited": has_more,
    }


def _write_records(records: Iterable[object], limit: int = 100) -> list[dict]:
    output = []
    for raw in records:
        if not isinstance(raw, dict) or not raw:
            raise ValueError("每条记录必须是非空字段对象")
        fields = {str(key).strip(): value for key, value in raw.items() if str(key).strip()}
        if not fields:
            raise ValueError("记录至少需要一个有效字段")
        output.append({"fields": fields})
    if not output or len(output) > limit:
        raise ValueError(f"一次需要 1–{limit} 条记录")
    return output


def append_records(credentials: dict, bitable: object, table_id: object, records: Iterable[object]) -> dict:
    app_token, resolved_table = parse_bitable_reference(bitable, table_id)
    if not resolved_table:
        raise ValueError("请提供 table_id")
    normalized = _write_records(records)
    data = request_feishu(
        "POST", f"/bitable/v1/apps/{app_token}/tables/{resolved_table}/records/batch_create",
        credentials, payload={"records": normalized},
    )
    created = data.get("records") if isinstance(data.get("records"), list) else []
    return {
        "app_token": app_token, "table_id": resolved_table,
        "url": bitable_url(app_token, resolved_table), "record_count": len(normalized),
        "record_ids": [str(item["record_id"]) for item in created if isinstance(item, dict) and item.get("record_id")],
    }


def update_record(
    credentials: dict, bitable: object, table_id: object, record_id: object, fields: object,
) -> dict:
    app_token, resolved_table = parse_bitable_reference(bitable, table_id)
    resolved_record = str(record_id or "").strip()
    if not resolved_table or not resolved_record or not isinstance(fields, dict) or not fields:
        raise ValueError("更新需要 table_id、record_id 和非空 fields")
    request_feishu(
        "PUT", f"/bitable/v1/apps/{app_token}/tables/{resolved_table}/records/{resolved_record}",
        credentials, payload={"fields": fields},
    )
    return {
        "app_token": app_token, "table_id": resolved_table, "record_id": resolved_record,
        "url": bitable_url(app_token, resolved_table), "updated_fields": sorted(str(key) for key in fields),
    }


def create_bitable(
    credentials: dict, *, name: str, table_name: str, fields: Iterable[object],
    records: Iterable[object] = (), folder_token: str = "",
) -> dict:
    clean_fields = []
    seen = set()
    for raw in fields:
        field = str(raw or "").strip()[:100]
        if not field or field in seen:
            raise ValueError("字段名不能为空或重复")
        seen.add(field)
        clean_fields.append({"field_name": field, "type": 1})
    if not 1 <= len(clean_fields) <= 50:
        raise ValueError("新表需要 1–50 个字段")
    payload = {"name": str(name or "").strip()[:100]}
    if folder_token:
        payload["folder_token"] = str(folder_token).strip()
    app_data = request_feishu("POST", "/bitable/v1/apps", credentials, payload=payload)
    app = app_data.get("app") if isinstance(app_data.get("app"), dict) else app_data
    app_token = str(app.get("app_token") or "")
    if not app_token:
        raise ConnectionError("飞书未返回新建多维表格标识")
    table_data = request_feishu(
        "POST", f"/bitable/v1/apps/{app_token}/tables", credentials,
        payload={"table": {"name": str(table_name or "数据表")[:100], "fields": clean_fields}},
    )
    table = table_data.get("table") if isinstance(table_data.get("table"), dict) else table_data
    table_id = str(table.get("table_id") or "")
    if not table_id:
        raise ConnectionError("飞书未返回新建数据表标识")
    record_values = list(records)
    normalized = _write_records(record_values, MAX_RECORDS) if record_values else []
    for start in range(0, len(normalized), 100):
        request_feishu(
            "POST", f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
            credentials, payload={"records": normalized[start:start + 100]},
        )
    return {
        "app_token": app_token, "table_id": table_id, "url": bitable_url(app_token, table_id),
        "name": payload["name"], "table_name": str(table_name), "record_count": len(normalized),
    }
