from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ...core.database import Database, utcnow


@dataclass(frozen=True)
class DatasetRef:
    ref_id: str
    kind: str
    source_refs: tuple[str, ...]
    engine_id: str
    location: dict[str, Any]
    snapshot_set: dict[str, Any]
    source_time: str | None
    schema_ref: str | None
    grain: str | None
    query_id: str | None
    query_hash: str | None
    contract_version: int
    policy_version: str
    computation_state: str
    result_completeness: str
    accuracy: str
    requested_scope: dict[str, Any]
    actual_scope: dict[str, Any]
    sample_metadata: dict[str, Any]
    row_count: int | None
    encoded_bytes: int | None
    preview_ref: str | None
    provenance_ref: str | None
    retention_until: str | None
    owner_id: str
    acl: dict[str, Any]
    created_at: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.kind not in {"logical_relation", "remote_table", "remote_objects", "bounded_file"}:
            raise ValueError("DatasetRef kind 无效")
        if self.computation_state not in {"planned", "running", "complete", "failed"}:
            raise ValueError("DatasetRef computation_state 无效")
        if self.result_completeness not in {"complete", "partial", "unknown"}:
            raise ValueError("DatasetRef result_completeness 无效")
        if self.accuracy not in {"exact", "approximate", "sample_based", "unknown"}:
            raise ValueError("DatasetRef accuracy 无效")
        if self.computation_state == "complete" and self.kind in {"remote_table", "remote_objects"} and not self.location:
            raise ValueError("已完成远端结果必须有稳定位置")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DatasetRef":
        normalized = dict(value)
        normalized["source_refs"] = tuple(normalized.get("source_refs") or ())
        return cls(**normalized)


@dataclass(frozen=True)
class BoundedTransferPolicy:
    max_rows: int = 100_000
    max_encoded_bytes: int = 50 * 1024 * 1024
    max_decoded_bytes: int = 512 * 1024 * 1024
    max_columns: int = 500
    max_value_bytes: int = 4 * 1024 * 1024
    max_seconds: int = 120
    max_run_egress_bytes: int = 200 * 1024 * 1024

    def approve(self, ref: DatasetRef, *, run_egress_bytes: int = 0) -> None:
        unknown = []
        if ref.row_count is None:
            unknown.append("row_count")
        if ref.encoded_bytes is None:
            unknown.append("encoded_bytes")
        if unknown:
            raise ValueError(f"本地物化前必须知道 {', '.join(unknown)}")
        if ref.row_count > self.max_rows:
            raise ValueError("DatasetRef 行数超过本地物化上限")
        if ref.encoded_bytes > self.max_encoded_bytes:
            raise ValueError("DatasetRef 编码字节超过本地物化上限")
        if run_egress_bytes + ref.encoded_bytes > self.max_run_egress_bytes:
            raise ValueError("任务累计数据出口超过上限")
        declared_columns = int(ref.actual_scope.get("column_count") or 0)
        if declared_columns <= 0 or declared_columns > self.max_columns:
            raise ValueError("DatasetRef 列数未知或超过本地物化上限")


class DatasetRefStore:
    def __init__(self, database: Database):
        self.db = database

    def put(self, ref: DatasetRef, *, workspace_id: str, run_id: str | None = None) -> dict[str, Any]:
        now = utcnow()
        with self.db.transaction() as connection:
            existing = connection.execute("SELECT workspace_id,created_at FROM dataset_refs WHERE id=?", (ref.ref_id,)).fetchone()
            if existing and str(existing["workspace_id"]) != workspace_id:
                raise PermissionError("不能覆盖其他工作空间的 DatasetRef")
            created_at = str(existing["created_at"]) if existing else ref.created_at
            connection.execute(
                """INSERT INTO dataset_refs(id,workspace_id,run_id,payload,created_at,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                   run_id=excluded.run_id,payload=excluded.payload,updated_at=excluded.updated_at""",
                (ref.ref_id, workspace_id, run_id, json.dumps(ref.to_dict(), ensure_ascii=False, default=str), created_at, now),
            )
        return ref.to_dict()

    def get(self, ref_id: str, *, workspace_id: str) -> DatasetRef | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM dataset_refs WHERE id=? AND workspace_id=?", (ref_id, workspace_id),
            ).fetchone()
        return DatasetRef.from_dict(json.loads(row["payload"])) if row else None

    def page(self, *, workspace_id: str, limit: int = 100, cursor: str = "") -> dict[str, Any]:
        args: list[Any] = [workspace_id]
        where = "workspace_id=?"
        if cursor:
            where += " AND id>?"
            args.append(cursor)
        args.append(max(1, min(int(limit), 500)))
        with self.db.connect() as connection:
            rows = connection.execute(
                f"SELECT id,payload FROM dataset_refs WHERE {where} ORDER BY id LIMIT ?",  # noqa: S608
                args,
            ).fetchall()
        items = [json.loads(row["payload"]) for row in rows]
        return {"items": items, "next_cursor": rows[-1]["id"] if len(rows) == args[-1] else None}


def retention_expired(ref: DatasetRef) -> bool:
    if not ref.retention_until:
        return False
    try:
        return datetime.fromisoformat(ref.retention_until) <= datetime.now(timezone.utc)
    except ValueError:
        return True
