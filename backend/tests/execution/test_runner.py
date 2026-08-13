"""Query execution runner: timeouts/connection drops retry, everything else does not."""

import pytest

from app.core.config import Settings
from app.execution.runner import ExecutionFailedError, execute


def _settings(**overrides) -> Settings:
    base = {"max_result_rows": 1000, "execution_retry_attempts": 2}
    base.update(overrides)
    return Settings(**base)


class _FakeSecured:
    """Minimal stand-in: the runner only needs sql and row_limit."""

    def __init__(self, sql: str, row_limit: int = 1000) -> None:
        self.sql = sql
        self.row_limit = row_limit


def test_execute_returns_columns_and_rows(sample_conn):
    result = execute(
        _FakeSecured("SELECT region_code, SUM(amount) AS total FROM sample.orders GROUP BY 1"),
        _settings(),
        connection=sample_conn,
    )

    assert result.columns == ("region_code", "total")
    assert result.row_count == len(result.rows) > 0
    assert result.elapsed_ms >= 0


def test_execute_marks_truncation_at_the_limit(sample_conn):
    result = execute(
        _FakeSecured("SELECT * FROM sample.orders LIMIT 3", row_limit=3),
        _settings(),
        connection=sample_conn,
    )
    assert result.truncated is True


def test_execute_does_not_mark_truncation_below_the_limit(sample_conn):
    result = execute(
        _FakeSecured("SELECT * FROM sample.orders LIMIT 3", row_limit=100),
        _settings(),
        connection=sample_conn,
    )
    assert result.truncated is False


def test_empty_result_is_not_an_error(sample_conn):
    result = execute(
        _FakeSecured("SELECT * FROM sample.orders WHERE region_code = 'ZZ'"),
        _settings(),
        connection=sample_conn,
    )

    assert result.row_count == 0
    assert result.columns


def test_sql_error_is_classified_and_not_retried(sample_conn, monkeypatch):
    attempts = {"count": 0}
    original = sample_conn.execute

    def counting_execute(*args, **kwargs):
        attempts["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(sample_conn, "execute", counting_execute)

    with pytest.raises(ExecutionFailedError) as excinfo:
        execute(
            _FakeSecured("SELECT no_such_column FROM sample.orders"),
            _settings(),
            connection=sample_conn,
        )

    assert excinfo.value.kind == "sql"
    # Retrying a broken statement only wastes time; it will fail identically.
    assert attempts["count"] == 1


def test_timeout_is_retried_then_reported(monkeypatch):
    from sqlalchemy.exc import OperationalError

    calls = {"count": 0}

    class _FlakyConnection:
        def execute(self, *args, **kwargs):
            calls["count"] += 1
            raise OperationalError(
                "SELECT 1", {}, Exception("canceling statement due to timeout")
            )

    with pytest.raises(ExecutionFailedError) as excinfo:
        execute(
            _FakeSecured("SELECT 1"),
            _settings(execution_retry_attempts=2),
            connection=_FlakyConnection(),
        )

    assert excinfo.value.kind == "timeout"
    # One initial attempt plus two retries.
    assert calls["count"] == 3


def test_transient_failure_then_success_returns_data(monkeypatch, sample_conn):
    from sqlalchemy.exc import OperationalError

    calls = {"count": 0}
    original = sample_conn.execute

    def flaky(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OperationalError(
                "SELECT 1", {}, Exception("server closed the connection")
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(sample_conn, "execute", flaky)

    result = execute(
        _FakeSecured("SELECT COUNT(*) AS n FROM sample.orders"),
        _settings(),
        connection=sample_conn,
    )

    assert calls["count"] == 2
    assert result.row_count == 1


def test_execution_error_detail_is_admin_facing(sample_conn):
    with pytest.raises(ExecutionFailedError) as excinfo:
        execute(
            _FakeSecured("SELECT no_such_column FROM sample.orders"),
            _settings(),
            connection=sample_conn,
        )
    assert excinfo.value.detail