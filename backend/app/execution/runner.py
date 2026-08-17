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

import re
import time
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError

from app.core.config import Settings
from app.core.db import sample_engine

# Match an existing LIMIT clause so we can replace it instead of nesting.
_LIMIT_PATTERN = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)


# PG SQLSTATE codes — language-independent, version-independent.
# 57014: query_canceled (statement timeout)
# 08xxx: connection-related
# 53xxx: insufficient resources
# 40001: serialization_failure
# 25006: read_only_sql_transaction (connection-level transaction_read_only)
_TIMEOUT_SQLSTATES = ("57014",)
_CONNECTION_SQLSTATES_PREFIX = ("08",)
_RESOURCE_SQLSTATES_PREFIX = ("53",)
_SERIALIZATION_SQLSTATES = ("40001",)
_READ_ONLY_SQLSTATE = "25006"


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
        # No SQLSTATE — fall back to message-text classification. This path
        # covers tests that mock OperationalError without a real driver, and
        # driver-level errors that the DBAPI didn't tag. SQLSTATE remains
        # authoritative when present.
        message = str(error).lower()
        if "timeout" in message or "canceling" in message:
            return "timeout"
        if any(
            marker in message
            for marker in ("closed the connection", "connection reset", "could not connect", "terminating connection")
        ):
            return "connection"
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
    if sqlstate == _READ_ONLY_SQLSTATE:
        # P0-06 layer 2: connection-level transaction_read_only is on.
        # Retry will not help — the pool sends another read-only connection.
        return "read_only"
    # Other SQLSTATE codes (42P01 undefined_table, 42703 undefined_column,
    # etc.) are user/compiler errors — not retryable.
    return "sql"


def _is_retryable(kind: str) -> bool:
    """True if the error kind is worth retrying."""
    # `read_only` is not retryable: the pool's connection options
    # force transaction_read_only on every new connection, so a retry
    # would hit the same wall.
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
    existing_match = _LIMIT_PATTERN.search(sql)
    if existing_match:
        existing_limit = int(existing_match.group().split()[-1])
        # The probe requests ``min(existing, row_limit) + 1`` rows — if the
        # DB returns that many, the binding cap (``row_limit``) was reached
        # and we mark truncated. The probe never widens the user's top-N
        # intent back up to ``row_limit`` (G-021 regression guard).
        effective_limit = min(existing_limit, row_limit) + 1
        delivered_limit = min(existing_limit, row_limit)
        executed_sql = _LIMIT_PATTERN.sub(
            f"LIMIT {effective_limit}", sql, count=1
        )
    else:
        effective_limit = row_limit + 1
        delivered_limit = row_limit
        executed_sql = f"SELECT * FROM ({sql}) AS _q LIMIT {effective_limit}"
    cursor = connection.execute(text(executed_sql))
    rows = cursor.fetchall()
    elapsed = int((time.perf_counter() - started) * 1000)

    actual_rows = tuple(tuple(row) for row in rows)
    # ``truncated`` reports only binding-cap truncation (``row_limit``).
    # The compiler's own LIMIT (e.g. top-N) is user intent, not truncation:
    # ``SELECT ... LIMIT 3`` returning 3 rows from a wider table is the
    # correct shape for that query.
    truncated = len(actual_rows) > row_limit
    if len(actual_rows) > delivered_limit:
        actual_rows = actual_rows[:delivered_limit]

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
            # A caller-provided connection is reused for every attempt —
            # the caller chose to manage its lifecycle (transaction,
            # mock, dialect pin). The "fresh connection on retry" rule
            # only applies when we own the connection ourselves.
            if connection is not None:
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