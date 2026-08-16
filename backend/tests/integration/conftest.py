"""Integration test fixtures.

These tests require a real PostgreSQL database and complement the unit
tests in tests/execution/. They are skipped when no DB is available.

The conftest sets up the `pytest.mark.integration` marker and builds
helpers that detect DB availability.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.db import sample_engine


def pytest_configure(config):
    """Register the integration marker on pytest startup."""
    config.addinivalue_line(
        "markers",
        "integration: integration tests requiring a real database",
    )


def _db_available() -> bool:
    """Check whether sample_engine can actually connect.

    Returns False on any OperationalError so a CI without a Postgres instance
    silently skips these tests instead of erroring out.
    """
    try:
        with sample_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False


# Recompute at module import so the skipif is final when the test module
# loads. We do this once per session.
_DB_READY = _db_available()


_requires_db = pytest.mark.skipif(
    not _DB_READY,
    reason="Sample database unavailable; cannot run integration test",
)


@pytest.fixture
def sample_connection():
    """Yield a sample_engine connection (closed after test).

    The connection inherits the `default_transaction_read_only=on` option
    configured on sample_engine, so any write attempt must fail.
    """
    with sample_engine.connect() as conn:
        yield conn
