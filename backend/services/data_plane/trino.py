from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from requests.auth import HTTPBasicAuth

from ...core.database import Database, utcnow
from ..security import safe_http_request, validate_outbound_url
from ..sql_security import validate_read_only_sql
from .contracts import DatasetRef, DatasetRefStore


@dataclass(frozen=True)
class TrinoConfig:
    engine_id: str
    endpoint: str
    user: str
    catalog: str
    schema: str
    source: str = "meridian-data-agent"
    username: str = ""
    password: str = ""
    scratch_catalog: str = ""
    scratch_schema: str = ""
    scratch_prefix: str = "meridian_run"
    timeout_seconds: int = 60
    max_preview_rows: int = 300
    max_response_bytes: int = 16 * 1024 * 1024

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TrinoConfig":
        endpoint = validate_outbound_url(str(value.get("endpoint") or value.get("url") or "").rstrip("/"))
        return cls(
            engine_id=str(value.get("engine_id") or "trino-reference")[:128], endpoint=endpoint,
            user=str(value.get("user") or value.get("username") or "meridian")[:128],
            catalog=str(value.get("catalog") or "")[:128], schema=str(value.get("schema") or "")[:128],
            source=str(value.get("source") or "meridian-data-agent")[:128],
            username=str(value.get("username") or ""), password=str(value.get("password") or ""),
            scratch_catalog=str(value.get("scratch_catalog") or value.get("catalog") or "")[:128],
            scratch_schema=str(value.get("scratch_schema") or "meridian_results")[:128],
            scratch_prefix=str(value.get("scratch_prefix") or "meridian_run")[:64],
            timeout_seconds=max(1, min(int(value.get("timeout_seconds") or 60), 3600)),
            max_preview_rows=max(1, min(int(value.get("max_preview_rows") or 300), 5000)),
            max_response_bytes=max(1024, min(int(value.get("max_response_bytes") or 16 * 1024 * 1024), 64 * 1024 * 1024)),
        )


class TrinoAdapter:
    """Trino statement-protocol adapter with durable nextUri reconciliation."""

    capabilities = {
        "dialect": "trino", "discover": True, "estimate": "explain_io",
        "asynchronous_query_id": True, "cancel": True, "result_ref": True,
        "snapshot": "iceberg_connector_dependent", "row_column_policy": "delegated_identity_or_views",
        "native_limits": "resource_groups_and_query_limits", "read_page": True,
    }

    def __init__(self, database: Database, workspace_id: str, config: TrinoConfig):
        self.db = database
        self.workspace_id = workspace_id
        self.config = config

    def discover(self, *, catalog: str | None = None, schema: str | None = None, limit: int = 100, cursor: str = "") -> dict:
        chosen_catalog = _identifier(catalog or self.config.catalog)
        if not chosen_catalog:
            rows = self._execute_small("SHOW CATALOGS", limit=limit + 1, validate=False)
            names = [str(row[0]) for row in rows if str(row[0]) > cursor]
            return _page_names(names, limit)
        chosen_schema = _identifier(schema or "")
        if not chosen_schema:
            rows = self._execute_small(f"SHOW SCHEMAS FROM {_quote(chosen_catalog)}", limit=limit + 1, validate=False)
            names = [str(row[0]) for row in rows if str(row[0]) > cursor]
            return _page_names(names, limit)
        rows = self._execute_small(
            f"SHOW TABLES FROM {_quote(chosen_catalog)}.{_quote(chosen_schema)}", limit=limit + 1, validate=False,
        )
        names = [str(row[0]) for row in rows if str(row[0]) > cursor]
        return _page_names(names, limit)

    def search(self, query: str, *, catalog: str | None = None, schema: str | None = None, limit: int = 50) -> dict:
        # information_schema is queried with a literal, never an identifier supplied by the model.
        pattern = str(query or "").strip().replace("'", "''")[:200]
        chosen_catalog = _identifier(catalog or self.config.catalog)
        clauses = [f"lower(table_name) LIKE lower('%{pattern}%')"]
        if schema:
            clauses.append(f"table_schema='{str(schema).replace(chr(39), chr(39) * 2)}'")
        rows = self._execute_small(
            f"SELECT table_schema,table_name FROM {_quote(chosen_catalog)}.information_schema.tables "  # noqa: S608
            f"WHERE {' AND '.join(clauses)} ORDER BY table_schema,table_name",
            limit=max(1, min(limit, 200)),
        )
        return {"items": [{"schema": row[0], "table": row[1]} for row in rows], "limited": len(rows) >= limit}

    def describe(self, catalog: str, schema: str, table: str) -> dict:
        qualified = ".".join(_quote(_identifier(item)) for item in (catalog, schema, table))
        rows = self._execute_small(f"DESCRIBE {qualified}", limit=1000, validate=False)
        return {"catalog": catalog, "schema": schema, "table": table, "columns": [
            {"name": row[0], "type": row[1], "extra": row[2] if len(row) > 2 else ""} for row in rows
        ]}

    def estimate(self, sql: str) -> dict:
        statement = validate_read_only_sql(sql, "trino")
        rows = self._execute_small(f"EXPLAIN (TYPE IO, FORMAT JSON) {statement}", limit=10, validate=False)
        raw = "\n".join(str(row[0]) for row in rows)
        estimate: dict[str, Any] = {
            "status": "available" if raw else "unknown", "source": "trino_explain_io",
            "estimated_rows": None, "estimated_scan_bytes": None, "raw": raw[:100_000],
        }
        try:
            parsed = json.loads(raw)
            estimate["raw_json"] = parsed
        except (json.JSONDecodeError, TypeError):
            pass
        return estimate

    def submit(
        self, sql: str, *, run_id: str, action_id: str,
        result_mode: str = "preview", source_refs: list[str] | None = None,
    ) -> dict:
        statement = validate_read_only_sql(sql, "trino")
        mode = str(result_mode or "preview")
        if mode not in {"preview", "materialize"}:
            raise ValueError("Trino result_mode 仅支持 preview 或 materialize")
        materialized_location = None
        submitted_statement = statement
        if mode == "materialize":
            if not self.config.scratch_catalog or not self.config.scratch_schema:
                raise ValueError("Trino 大结果物化需要配置 scratch_catalog 与 scratch_schema")
            prefix = _identifier(self.config.scratch_prefix)
            table = f"{prefix}_{hashlib.sha256(f'{run_id}:{action_id}:{statement}'.encode()).hexdigest()[:24]}"
            qualified = ".".join(
                _quote(_identifier(value))
                for value in (self.config.scratch_catalog, self.config.scratch_schema, table)
            )
            # User/model SQL is read-only validated above. Only this server-generated
            # CTAS may write, and only into the administrator-configured scratch namespace.
            submitted_statement = f"CREATE TABLE {qualified} AS {statement}"
            materialized_location = {
                "catalog": self.config.scratch_catalog,
                "schema": self.config.scratch_schema,
                "table": table,
                "qualified_name": qualified,
            }
        response = self._request(
            "POST", f"{self.config.endpoint}/v1/statement", data=submitted_statement.encode("utf-8"),
        )
        payload = self._payload(response)
        query_id = str(payload.get("id") or "")
        if not query_id:
            raise ConnectionError("Trino 未返回 query id")
        record = {
            "id": query_id, "workspace_id": self.workspace_id, "run_id": run_id,
            "action_id": action_id, "engine_id": self.config.engine_id,
            "sql_hash": hashlib.sha256(statement.encode("utf-8")).hexdigest(),
            "sql": statement, "status": _state(payload), "next_uri": payload.get("nextUri"),
            "columns": payload.get("columns") or [], "preview": (payload.get("data") or [])[:self.config.max_preview_rows],
            "stats": payload.get("stats") or {}, "update_count": payload.get("updateCount"),
            "error": payload.get("error"),
            "result_mode": mode, "materialized_location": materialized_location,
            "submitted_at": utcnow(), "updated_at": utcnow(),
            "source_refs": [str(value) for value in source_refs or []],
        }
        self.db.put("warehouse_queries", record, workspace_id=self.workspace_id)
        if payload.get("error"):
            raise RuntimeError(_trino_error(payload["error"]))
        public = self.public_query(record)
        return {
            **public, "engine_status": public.get("status"),
            "status": "ACCEPTED" if record["next_uri"] else "SUCCEEDED", "query_id": query_id,
        }

    def poll(self, query_id: str) -> dict:
        record = self._query(query_id)
        if not record.get("next_uri"):
            return self.public_query(record)
        response = self._request("GET", str(record["next_uri"]))
        payload = self._payload(response)
        preview = list(record.get("preview") or [])
        room = max(0, self.config.max_preview_rows - len(preview))
        preview.extend((payload.get("data") or [])[:room])
        changes = {
            "status": _state(payload), "next_uri": payload.get("nextUri"),
            "columns": payload.get("columns") or record.get("columns") or [],
            "preview": preview, "stats": payload.get("stats") or record.get("stats") or {},
            "update_count": payload.get("updateCount", record.get("update_count")),
            "error": payload.get("error"), "updated_at": utcnow(),
        }
        record = self.db.patch("warehouse_queries", query_id, changes, workspace_id=self.workspace_id) or record
        if payload.get("error"):
            raise RuntimeError(_trino_error(payload["error"]))
        return self.public_query(record)

    def reconcile(self, query_id: str, *, max_pages: int = 10_000) -> dict:
        record = self._query(query_id)
        pages = 0
        while record.get("next_uri") and pages < max_pages:
            record = self.poll(query_id)
            pages += 1
        if record.get("next_uri"):
            return {**self.public_query(record), "status": "UNKNOWN", "reason": "client_page_budget_exceeded"}
        return self.public_query(record)

    def cancel(self, query_id: str) -> dict:
        record = self._query(query_id)
        if not record.get("next_uri"):
            return {"query_id": query_id, "cancel_requested": False, "status": record.get("status")}
        response = self._request("DELETE", str(record["next_uri"]))
        if response.status_code not in {200, 202, 204}:
            raise ConnectionError(f"Trino 取消请求失败：HTTP {response.status_code}")
        updated = self.db.patch(
            "warehouse_queries", query_id,
            {"status": "cancelling", "cancel_requested_at": utcnow(), "updated_at": utcnow()},
            workspace_id=self.workspace_id,
        )
        return {"query_id": query_id, "cancel_requested": True, "status": (updated or {}).get("status")}

    def result_ref(
        self,
        query_id: str,
        *,
        owner_id: str,
        contract_version: int,
        policy_version: str,
        location: dict[str, Any] | None = None,
        retention_until: str | None = None,
    ) -> DatasetRef:
        record = self._query(query_id)
        if record.get("status") != "finished" or record.get("next_uri"):
            raise ValueError("Trino 查询尚未完成，不能登记完整结果")
        stats = record.get("stats") or {}
        durable_location = location or record.get("materialized_location")
        output_positions = stats.get("outputPositions")
        preview_count = len(record.get("preview") or [])
        if not durable_location and output_positions is not None and int(output_positions) != preview_count:
            raise ValueError("Trino 大结果尚未物化到稳定位置，query id 不能作为完整 DatasetRef")
        ref = DatasetRef(
            ref_id=self.db.new_id("dsref"), kind="remote_table" if durable_location else "logical_relation",
            source_refs=tuple(str(value) for value in record.get("source_refs") or []),
            engine_id=self.config.engine_id, location=durable_location or {"query_id": query_id},
            snapshot_set={}, source_time=utcnow(), schema_ref=None, grain=None,
            query_id=query_id, query_hash=record.get("sql_hash"), contract_version=contract_version,
            policy_version=policy_version, computation_state="complete", result_completeness="complete",
            accuracy="exact", requested_scope={}, actual_scope={
                "column_count": len(record.get("columns") or []), "processed_rows": stats.get("processedRows"),
            }, sample_metadata={},
            row_count=record.get("update_count") if durable_location else stats.get("outputPositions"),
            encoded_bytes=stats.get("outputDataSizeBytes"),
            preview_ref=f"warehouse_query:{query_id}", provenance_ref=f"warehouse_query:{query_id}",
            retention_until=retention_until, owner_id=owner_id,
            acl={"workspace_id": self.workspace_id, "actor_ids": [owner_id]},
        )
        DatasetRefStore(self.db).put(ref, workspace_id=self.workspace_id, run_id=record.get("run_id"))
        return ref

    def read_page(self, query_id: str, *, offset: int = 0, limit: int = 100) -> dict:
        record = self._query(query_id)
        rows = list(record.get("preview") or [])
        start, size = max(0, int(offset)), max(1, min(int(limit), 500))
        page = rows[start:start + size]
        return {
            "query_id": query_id, "columns": record.get("columns") or [], "data": page,
            "offset": start, "next_offset": start + size if start + size < len(rows) else None,
            "completeness": "partial" if record.get("next_uri") or len(rows) >= self.config.max_preview_rows else "complete",
        }

    def stats(self, query_id: str) -> dict:
        record = self._query(query_id)
        return {"query_id": query_id, "engine_id": self.config.engine_id, "raw": record.get("stats") or {}}

    def public_query(self, record: dict[str, Any]) -> dict[str, Any]:
        return {key: record.get(key) for key in (
            "id", "run_id", "engine_id", "status", "columns", "preview", "stats", "error", "updated_at",
        )} | {"query_id": record["id"], "has_next": bool(record.get("next_uri"))}

    def _execute_small(self, sql: str, *, limit: int, validate: bool = True) -> list[list[Any]]:
        statement = validate_read_only_sql(sql, "trino") if validate else sql
        response = self._request("POST", f"{self.config.endpoint}/v1/statement", data=statement.encode("utf-8"))
        payload = self._payload(response)
        rows = list(payload.get("data") or [])
        pages = 0
        while payload.get("nextUri") and len(rows) < limit and pages < 1000:
            payload = self._payload(self._request("GET", str(payload["nextUri"])))
            if payload.get("error"):
                raise RuntimeError(_trino_error(payload["error"]))
            rows.extend(payload.get("data") or [])
            pages += 1
        if payload.get("nextUri"):
            self._request("DELETE", str(payload["nextUri"]))
        return rows[:limit]

    def _query(self, query_id: str) -> dict:
        record = self.db.get("warehouse_queries", query_id, workspace_id=self.workspace_id)
        if not record:
            raise FileNotFoundError("Trino 查询不存在")
        return record

    def _request(self, method: str, url: str, **kwargs: Any):
        headers = {
            "X-Trino-User": self.config.user, "X-Trino-Source": self.config.source,
            "X-Trino-Catalog": self.config.catalog, "X-Trino-Schema": self.config.schema,
            **kwargs.pop("headers", {}),
        }
        auth = HTTPBasicAuth(self.config.username, self.config.password) if self.config.username else None
        return safe_http_request(
            method, url, headers=headers, auth=auth, timeout=self.config.timeout_seconds,
            max_response_bytes=self.config.max_response_bytes, **kwargs,
        )

    @staticmethod
    def _payload(response: Any) -> dict[str, Any]:
        if response.status_code not in {200, 201}:
            raise ConnectionError(f"Trino 请求失败：HTTP {response.status_code}")
        try:
            value = response.json()
        except ValueError as exc:
            raise ConnectionError("Trino 返回了无效 JSON") from exc
        if not isinstance(value, dict):
            raise ConnectionError("Trino 返回对象格式无效")
        return value


def _identifier(value: str) -> str:
    value = str(value or "").strip()
    if value and (len(value) > 128 or "\x00" in value):
        raise ValueError("Trino 标识符无效")
    return value


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _page_names(names: list[str], limit: int) -> dict:
    size = max(1, min(int(limit), 500))
    ordered = sorted(dict.fromkeys(names))
    return {"items": ordered[:size], "next_cursor": ordered[size - 1] if len(ordered) > size else None}


def _state(payload: dict[str, Any]) -> str:
    if payload.get("error"):
        return "failed"
    if payload.get("nextUri"):
        return "running"
    return "finished"


def _trino_error(error: Any) -> str:
    if not isinstance(error, dict):
        return str(error)
    name = error.get("errorName") or error.get("errorType") or "TRINO_ERROR"
    return f"{name}: {error.get('message') or 'query failed'}"
