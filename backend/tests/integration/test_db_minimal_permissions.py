"""Integration tests for DB minimal permissions (P0-06).

These tests verify that the runtime sample_engine connection actually
enforces read-only at the DB layer, not just at the application AST
guardrail. If an attacker bypasses the AST, the DB itself must still
reject writes.

The tests are skipped when no database is available (see conftest).

PG SQLSTATE 25006 = read_only_sql_transaction
"""

import pytest
from sqlalchemy import text

from app.core.db import sample_engine

from tests.integration.conftest import _requires_db, sample_connection


@pytest.mark.integration
@_requires_db
class TestSampleEngineReadOnly:
    """Verify sample_engine forces read-only connections."""

    def test_select_succeeds(self, sample_connection):
        """SELECT must work — that's the primary use case."""
        result = sample_connection.execute(text("SELECT 1 AS n")).scalar()
        assert result == 1

    def test_insert_raises(self, sample_connection):
        """INSERT must fail with a read-only error."""
        with pytest.raises(Exception) as exc_info:
            sample_connection.execute(
                text("INSERT INTO sample.orders (order_id) VALUES (999)")
            )
        # Confirm it's a read-only error (PG SQLSTATE 25006)
        assert (
            "read-only" in str(exc_info.value).lower()
            or "25006" in str(exc_info.value)
        ), f"Expected read-only error, got: {exc_info.value!r}"

    def test_update_raises(self, sample_connection):
        """UPDATE must fail."""
        with pytest.raises(Exception) as exc_info:
            sample_connection.execute(
                text("UPDATE sample.orders SET order_id = 1 WHERE 1=0")
            )
        assert (
            "read-only" in str(exc_info.value).lower()
            or "25006" in str(exc_info.value)
        ), f"Expected read-only error, got: {exc_info.value!r}"

    def test_delete_raises(self, sample_connection):
        """DELETE must fail."""
        with pytest.raises(Exception) as exc_info:
            sample_connection.execute(
                text("DELETE FROM sample.orders WHERE 1=0")
            )
        assert (
            "read-only" in str(exc_info.value).lower()
            or "25006" in str(exc_info.value)
        ), f"Expected read-only error, got: {exc_info.value!r}"

    def test_create_table_raises(self, sample_connection):
        """CREATE TABLE must fail (DDL also blocked)."""
        with pytest.raises(Exception) as exc_info:
            sample_connection.execute(
                text("CREATE TABLE sample.tmp_test (id INT)")
            )
        assert (
            "read-only" in str(exc_info.value).lower()
            or "25006" in str(exc_info.value)
            or "permission denied" in str(exc_info.value).lower()
        ), f"Expected read-only or permission error, got: {exc_info.value!r}"

    def test_drop_table_raises(self, sample_connection):
        """DROP TABLE must fail."""
        with pytest.raises(Exception) as exc_info:
            sample_connection.execute(
                text("DROP TABLE IF EXISTS sample.orders")
            )
        assert (
            "read-only" in str(exc_info.value).lower()
            or "25006" in str(exc_info.value)
            or "permission denied" in str(exc_info.value).lower()
        ), f"Expected read-only or permission error, got: {exc_info.value!r}"


@pytest.mark.integration
@_requires_db
class TestConnectionReuse:
    """Verify each new connection inherits the read-only setting."""

    def test_five_consecutive_connections_all_read_only(self):
        """Open 5 connections in a row; each must be read-only.

        Regression check: the read-only option must be applied per
        connection, not just on the first one.
        """
        for i in range(5):
            with sample_engine.connect() as conn:
                with pytest.raises(Exception) as exc_info:
                    conn.execute(
                        text(
                            "INSERT INTO sample.orders (order_id) "
                            f"VALUES ({i * 1000})"
                        )
                    )
                assert (
                    "read-only" in str(exc_info.value).lower()
                    or "25006" in str(exc_info.value)
                ), f"Connection {i} not read-only: {exc_info.value!r}"


@pytest.mark.integration
@_requires_db
class TestConcurrentConnections:
    """Read-only must hold under concurrent use too."""

    def test_concurrent_reads_succeed(self):
        """Multiple readers can run concurrently without issues."""
        results = []
        for _ in range(3):
            with sample_engine.connect() as conn:
                results.append(conn.execute(text("SELECT 1")).scalar())
        assert results == [1, 1, 1]
