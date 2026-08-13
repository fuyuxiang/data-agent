"""Query execution (spec 5.4).

Retries are deliberately narrow: timeouts and connection drops are transient,
everything else is not. A statement that fails on a missing column will fail
identically on retry — under a compiler architecture that failure means the
semantic model and the physical table have drifted, which retrying cannot fix.
"""

import time
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError

from app.core.config import Settings
from app.core.db import sample_engine

_TRANSIENT_MARKERS = (
    "timeout",
    "canceling statement",
    "server closed the connection",
    "connection reset",
    "could not connect",
    "terminating connection",
)


@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    row_count: int
    truncated: bool
    elapsed_ms: int


class ExecutionFailedError(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(detail)


class _Executable(Protocol):
    sql: str
    row_limit: int


def _classify(error: Exception) -> str:
    message = str(error).lower()
    if any(marker in message for marker in _TRANSIENT_MARKERS):
        return "timeout" if "timeout" in message or "canceling" in message else "connection"
    if isinstance(error, (DBAPIError, SQLAlchemyError)):
        return "sql"
    return "unknown"


def _run_once(connection, sql: str, row_limit: int) -> QueryResult:
    started = time.perf_counter()
    cursor = connection.execute(text(sql))
    rows = cursor.fetchall()
    elapsed = int((time.perf_counter() - started) * 1000)

    return QueryResult(
        columns=tuple(cursor.keys()),
        rows=tuple(tuple(row) for row in rows),
        row_count=len(rows),
        truncated=len(rows) >= row_limit,
        elapsed_ms=elapsed,
    )


def execute(
    secured: _Executable, settings: Settings, *, connection: Connection | None = None
) -> QueryResult:
    attempts = settings.execution_retry_attempts + 1
    last_kind = "unknown"
    last_detail = ""

    for attempt in range(attempts):
        try:
            if connection is not None:
                return _run_once(connection, secured.sql, secured.row_limit)
            with sample_engine.connect() as own_connection:
                return _run_once(own_connection, secured.sql, secured.row_limit)
        except (OperationalError, SQLAlchemyError) as error:
            last_kind = _classify(error)
            last_detail = f"{error.__class__.__name__}: {error}"
            if last_kind not in ("timeout", "connection"):
                break
            if attempt == attempts - 1:
                break

    raise ExecutionFailedError(last_kind, last_detail)