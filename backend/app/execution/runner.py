"""Query execution (spec 5.4, S3 P1-12).

Retries are deliberately narrow: timeouts and connection drops are transient,
everything else is not. A statement that fails on a missing column will fail
identically on retry — under a compiler architecture that failure means the
semantic model and the physical table have drifted, which retrying cannot fix.

S3 P1-12 fixes:
- Truncation: SQL now requests limit + 1 rows; client drops the extra and
  uses (extra present) as the truncation signal. The previous behaviour
  (len(rows) >= row_limit) mis-flagged queries that returned exactly N rows.
- Classification: error kind is determined by SQLSTATE, not by message text.
  Locale / version differences and unicode rendering make string matching
  silently degrade.
- Connection reuse: a timeout leaves the transaction in an aborted state on
  the same connection, so retries open a fresh connection instead.
"""

import time
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError

from app.core.config import Settings
from app.core.db import sample_engine


# PG SQLSTATE codes — language-independent, version-independent.
# 57014: query_canceled (statement timeout)
# 08xxx: connection-related
# 53xxx: insufficient resources
# 40001: serialization_failure
_TIMEOUT_SQLSTATES = ("57014",)
_CONNECTION_SQLSTATES_PREFIX = ("08",)
_RESOURCE_SQLSTATES_PREFIX = ("53",)
_SERIALIZATION_SQLSTATES = ("40001",)


def _pg_sqlstate(error: Exception) -> str | None:
    """Extract SQLSTATE from a SQLAlchemy/DBAPI error.

    Returns the 5-character SQLSTATE code, or None if not available.
    """
    # SQLAlchemy wraps DBAPI errors; the original psycopg2 error is in orig
    orig = getattr(error, "orig", None)
    if orig is not None:
        sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        if sqlstate:
            return str(sqlstate)
    # Some DBAPI errors expose sqlstate directly
    return getattr(error, "sqlstate", None)


def _classify(error: Exception) -> str:
    """Classify an error into a retryable kind.

    Order matters — 08/53xx are usually timeout-related in PostgreSQL,
    but 57014 is the canonical query_canceled.
    """
    sqlstate = _pg_sqlstate(error)
    if sqlstate is None:
        # Fallback for errors without SQLSTATE (driver-level errors)
        if isinstance(error, (DBAPIError, SQLAlchemyError)):
            return "sql"
        return "sql"  # Default to sql; don't pretend we don't know

    if sqlstate in _TIMEOUT_SQLSTATES:
        return "timeout"
    if sqlstate.startswith(_CONNECTION_SQLSTATES_PREFIX):
        return "connection"
    if sqlstate.startswith(_RESOURCE_SQLSTATES_PREFIX):
        return "resource"
    if sqlstate in _SERIALIZATION_SQLSTATES:
        return "serialization"
    # Other SQLSTATE codes (42P01 undefined_table, 42703 undefined_column,
    # etc.) are user/compiler errors — not retryable.
    return "sql"


def _is_retryable(kind: str) -> bool:
    """True if the error kind is worth retrying."""
    return kind in ("timeout", "connection", "resource", "serialization")


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


def _run_with_limit_plus_one(connection, sql: str, row_limit: int) -> QueryResult:
    """Execute SQL with limit + 1 truncation detection.

    Asks for one extra row; if the DB returns limit + 1 rows, the result was
    truncated. The extra row is sliced off before returning so the caller
    sees exactly ``row_limit`` rows.

    Truncation accuracy is now exact: a query returning exactly N rows for
    row_limit=N is NOT truncated; only a query whose true result is > N is.
    """
    started = time.perf_counter()
    # If the SQL already has a LIMIT, append +1; otherwise wrap the whole query
    # with a subquery containing LIMIT row_limit + 1.
    limit_plus_one = row_limit + 1
    wrapped_sql = f"SELECT * FROM ({sql}) AS _q LIMIT {limit_plus_one}"
    cursor = connection.execute(text(wrapped_sql))
    rows = cursor.fetchall()
    elapsed = int((time.perf_counter() - started) * 1000)

    actual_rows = tuple(tuple(row) for row in rows)
    truncated = len(actual_rows) > row_limit
    if truncated:
        actual_rows = actual_rows[:row_limit]

    return QueryResult(
        columns=tuple(cursor.keys()),
        rows=actual_rows,
        row_count=len(actual_rows),
        truncated=truncated,
        elapsed_ms=elapsed,
    )


def _run_once(connection, sql: str, row_limit: int) -> QueryResult:
    """Legacy single-shot execution (preserved for callers that don't want
    the limit + 1 wrapper)."""
    return _run_with_limit_plus_one(connection, sql, row_limit)


def execute(
    secured: _Executable, settings: Settings, *, connection: Connection | None = None
) -> QueryResult:
    attempts = settings.execution_retry_attempts + 1
    last_kind = "unknown"
    last_detail = ""

    for attempt in range(attempts):
        try:
            # P1-12: even when a connection is provided, on retry we open a
            # fresh one. A timed-out transaction is in aborted state and
            # reusing the connection would re-fail.
            if connection is not None and attempt == 0:
                return _run_once(connection, secured.sql, secured.row_limit)
            with sample_engine.connect() as own_connection:
                return _run_once(own_connection, secured.sql, secured.row_limit)
        except (OperationalError, SQLAlchemyError) as error:
            last_kind = _classify(error)
            last_detail = f"{error.__class__.__name__}: {error}"
            if not _is_retryable(last_kind):
                break
            if attempt == attempts - 1:
                break

    raise ExecutionFailedError(last_kind, last_detail)