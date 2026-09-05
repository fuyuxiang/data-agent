from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Small transactional repository used by every feature module."""

    GLOBAL_COLLECTIONS = frozenset({"workspaces", "users", "email_codes"})

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self.connect() as connection:
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS records (
            collection TEXT NOT NULL,
            id TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            PRIMARY KEY (collection, id)
        );
        CREATE INDEX IF NOT EXISTS idx_records_collection_workspace
            ON records(collection, workspace_id, updated_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email
            ON records(json_extract(payload, '$.email')) WHERE collection='users';
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages(session_id, created_at);
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            object_type TEXT,
            object_id TEXT,
            detail TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_workspace
            ON audit_log(workspace_id, id DESC);
        CREATE TABLE IF NOT EXISTS job_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        """
        with self.transaction() as connection:
            connection.executescript(schema)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,description,applied_at) VALUES(1,?,?)",
                ("initial records, messages, audit, jobs and quota indexes", utcnow()),
            )
        self._seed()

    def _seed(self) -> None:
        if self.list("workspaces", include_archived=True):
            return
        workspace = self.put(
            "workspaces",
            {
                "id": "default",
                "name": "默认分析空间",
                "description": "用于数据接入、分析、流程编排与成果交付",
                "permission": "write",
            },
            workspace_id="default",
        )
        self.put(
            "sessions",
            {"id": "welcome", "name": "首次分析", "workspace_id": workspace["id"], "status": "active"},
            workspace_id="default",
        )
        self.put(
            "providers",
            {
                "id": "environment-default",
                "name": "环境变量模型",
                "base_url": "",
                "model": "",
                "enabled": True,
                "secret_source": "environment",
            },
        )

    def ping(self) -> str:
        with self.connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return "ready"

    @staticmethod
    def new_id(prefix: str = "") -> str:
        value = uuid.uuid4().hex[:16]
        return f"{prefix}_{value}" if prefix else value

    def put(self, collection: str, payload: dict[str, Any], *, workspace_id: str = "default") -> dict[str, Any]:
        value = dict(payload)
        record_id = str(value.get("id") or self.new_id(collection.rstrip("s")[:4]))
        value["id"] = record_id
        if collection not in self.GLOBAL_COLLECTIONS:
            payload_workspace = str(value.get("workspace_id") or workspace_id)
            if payload_workspace != workspace_id:
                raise PermissionError("记录工作空间与写入范围不一致")
            value["workspace_id"] = workspace_id
        now = utcnow()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT workspace_id, created_at FROM records WHERE collection=? AND id=?",
                (collection, record_id),
            ).fetchone()
            if (
                existing and collection not in self.GLOBAL_COLLECTIONS
                and str(existing["workspace_id"]) != workspace_id
            ):
                raise PermissionError("不能覆盖其他工作空间中的同名记录")
            created_at = existing["created_at"] if existing else now
            value.setdefault("created_at", created_at)
            value["updated_at"] = now
            connection.execute(
                """INSERT INTO records(collection,id,workspace_id,payload,created_at,updated_at,archived_at)
                   VALUES(?,?,?,?,?,?,NULL)
                   ON CONFLICT(collection,id) DO UPDATE SET
                     workspace_id=excluded.workspace_id,payload=excluded.payload,
                     updated_at=excluded.updated_at,archived_at=NULL""",
                (collection, record_id, workspace_id, json.dumps(value, ensure_ascii=False), created_at, now),
            )
        return value

    def create_user(self, payload: dict[str, Any], *, allow_additional: bool = False) -> dict[str, Any]:
        value = dict(payload)
        value["id"] = str(value.get("id") or self.new_id("usr"))
        now = utcnow()
        with self.transaction() as connection:
            duplicate = connection.execute(
                "SELECT 1 FROM records WHERE collection='users' AND json_extract(payload, '$.email')=? LIMIT 1",
                (str(value.get("email") or ""),),
            ).fetchone()
            if duplicate:
                raise ValueError("该邮箱已经注册")
            has_users = connection.execute(
                "SELECT 1 FROM records WHERE collection='users' LIMIT 1",
            ).fetchone()
            if has_users and not allow_additional:
                raise PermissionError("当前实例未开放自助注册，请联系系统所有者添加成员")
            value["role"] = "member" if has_users else "owner"
            value["created_at"] = now
            value["updated_at"] = now
            connection.execute(
                "INSERT INTO records(collection,id,workspace_id,payload,created_at,updated_at,archived_at) VALUES('users',?,?,?,?,?,NULL)",
                (value["id"], "default", json.dumps(value, ensure_ascii=False), now, now),
            )
        return value

    def put_if_absent(
        self, collection: str, payload: dict[str, Any], *, workspace_id: str = "default",
    ) -> tuple[dict[str, Any], bool]:
        value = dict(payload)
        record_id = str(value.get("id") or self.new_id(collection.rstrip("s")[:4]))
        value["id"] = record_id
        if collection not in self.GLOBAL_COLLECTIONS:
            payload_workspace = str(value.get("workspace_id") or workspace_id)
            if payload_workspace != workspace_id:
                raise PermissionError("记录工作空间与写入范围不一致")
            value["workspace_id"] = workspace_id
        now = utcnow()
        value.setdefault("created_at", now)
        value["updated_at"] = now
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO records(
                       collection,id,workspace_id,payload,created_at,updated_at,archived_at
                   ) VALUES(?,?,?,?,?,?,NULL)""",
                (collection, record_id, workspace_id, json.dumps(value, ensure_ascii=False), now, now),
            )
            created = cursor.rowcount == 1
            if not created:
                row = connection.execute(
                    "SELECT workspace_id,payload FROM records WHERE collection=? AND id=?",
                    (collection, record_id),
                ).fetchone()
                if not row or (
                    collection not in self.GLOBAL_COLLECTIONS and str(row["workspace_id"]) != workspace_id
                ):
                    raise PermissionError("不能读取其他工作空间中的同名记录")
                value = json.loads(row["payload"])
        return value, created

    def get(self, collection: str, record_id: str, *, include_archived: bool = False) -> dict[str, Any] | None:
        query = "SELECT payload, archived_at FROM records WHERE collection=? AND id=?"
        with self.connect() as connection:
            row = connection.execute(query, (collection, record_id)).fetchone()
        if not row or (row["archived_at"] and not include_archived):
            return None
        value = json.loads(row["payload"])
        if row["archived_at"]:
            value["archived_at"] = row["archived_at"]
        return value

    def list(
        self,
        collection: str,
        *,
        workspace_id: str | None = None,
        include_archived: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        where = ["collection=?"]
        args: list[Any] = [collection]
        if workspace_id is not None:
            where.append("workspace_id=?")
            args.append(workspace_id)
        if not include_archived:
            where.append("archived_at IS NULL")
        args.append(max(1, min(limit, 5000)))
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT payload, archived_at FROM records WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT ?",
                args,
            ).fetchall()
        result = []
        for row in rows:
            value = json.loads(row["payload"])
            if row["archived_at"]:
                value["archived_at"] = row["archived_at"]
            result.append(value)
        return result

    def patch(self, collection: str, record_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        now = utcnow()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT workspace_id,payload FROM records WHERE collection=? AND id=? AND archived_at IS NULL",
                (collection, record_id),
            ).fetchone()
            if not row:
                return None
            current = json.loads(row["payload"])
            if collection not in self.GLOBAL_COLLECTIONS:
                requested_workspace = str(changes.get("workspace_id") or row["workspace_id"])
                if requested_workspace != str(row["workspace_id"]):
                    raise PermissionError("不能将记录迁移到其他工作空间")
            current.update(changes)
            current["id"] = record_id
            if collection not in self.GLOBAL_COLLECTIONS:
                current["workspace_id"] = str(row["workspace_id"])
            current["updated_at"] = now
            connection.execute(
                "UPDATE records SET payload=?,updated_at=? WHERE collection=? AND id=?",
                (json.dumps(current, ensure_ascii=False), now, collection, record_id),
            )
        return current

    def archive(self, collection: str, record_id: str) -> bool:
        now = utcnow()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE records SET archived_at=?, updated_at=? WHERE collection=? AND id=? AND archived_at IS NULL",
                (now, now, collection, record_id),
            )
        return cursor.rowcount > 0

    def restore(self, collection: str, record_id: str) -> bool:
        now = utcnow()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE records SET archived_at=NULL, updated_at=? WHERE collection=? AND id=? AND archived_at IS NOT NULL",
                (now, collection, record_id),
            )
        return cursor.rowcount > 0

    def delete(self, collection: str, record_id: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM records WHERE collection=? AND id=?",
                (collection, record_id),
            )
        return cursor.rowcount > 0

    def add_message(self, session_id: str, role: str, content: str, metadata: dict | None = None) -> dict:
        message = {
            "id": self.new_id("msg"),
            "session_id": session_id,
            "role": role,
            "content": content,
            "metadata": metadata or {},
            "created_at": utcnow(),
        }
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO messages(id,session_id,role,content,metadata,created_at) VALUES(?,?,?,?,?,?)",
                (
                    message["id"], session_id, role, content,
                    json.dumps(message["metadata"], ensure_ascii=False), message["created_at"],
                ),
            )
        return message

    def messages(self, session_id: str, limit: int = 200) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY created_at ASC LIMIT ?",
                (session_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [
            {
                "id": row["id"], "session_id": row["session_id"], "role": row["role"],
                "content": row["content"], "metadata": json.loads(row["metadata"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def replace_messages(self, session_id: str, messages: list[dict]) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            for item in messages:
                message_id = item.get("id") or self.new_id("msg")
                if item.get("session_id") and item.get("session_id") != session_id:
                    message_id = self.new_id("msg")
                connection.execute(
                    "INSERT INTO messages(id,session_id,role,content,metadata,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        message_id, session_id, item.get("role", "user"),
                        str(item.get("content", "")), json.dumps(item.get("metadata", {}), ensure_ascii=False),
                        item.get("created_at") or utcnow(),
                    ),
                )

    def audit(
        self,
        event_type: str,
        *,
        workspace_id: str = "default",
        actor: str | None = None,
        object_type: str = "",
        object_id: str = "",
        detail: dict | None = None,
    ) -> None:
        if actor is None:
            try:
                from flask import has_request_context, session

                actor = str(session.get("user_id") or "local-default") if has_request_context() else "system"
            except RuntimeError:
                actor = "system"
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_log(workspace_id,event_type,actor,object_type,object_id,detail,created_at) VALUES(?,?,?,?,?,?,?)",
                (workspace_id, event_type, actor, object_type, object_id, json.dumps(detail or {}, ensure_ascii=False), utcnow()),
            )

    def audit_entries(self, workspace_id: str = "default", limit: int = 100) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_log WHERE workspace_id=? ORDER BY id DESC LIMIT ?",
                (workspace_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) | {"detail": json.loads(row["detail"])} for row in rows]

    def usage_total(self, workspace_id: str, since: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT COALESCE(SUM(CAST(json_extract(payload, '$.total_tokens') AS INTEGER)), 0) AS total
                   FROM records
                   WHERE collection='usage_events' AND workspace_id=? AND archived_at IS NULL AND created_at>=?""",
                (workspace_id, since),
            ).fetchone()
        return int(row["total"] or 0)

    def job_event(self, job_id: str, event_type: str, payload: dict) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO job_events(job_id,event_type,payload,created_at) VALUES(?,?,?,?)",
                (job_id, event_type, json.dumps(payload, ensure_ascii=False), utcnow()),
            )
        return int(cursor.lastrowid)

    def job_events(self, after: int = 0, limit: int = 500) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_events WHERE sequence>? ORDER BY sequence ASC LIMIT ?",
                (after, max(1, min(limit, 2000))),
            ).fetchall()
        return [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]
