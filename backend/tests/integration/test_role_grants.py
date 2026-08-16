"""Integration tests for role-level DB permissions (P0-06 layer 2).

Verifies that the `data_agent_reader` role has been created via
scripts/create_reader_role.sql. This is the role-level defense that
applies even when the connection-level transaction_read_only is bypassed.

These tests are skipped when no database is available, OR when the role
has not been created by the deployment script. The role creation requires
admin (CREATEROLE) credentials which the test fixture does not have.
"""

import pytest
from sqlalchemy import text

from app.core.db import sample_engine

from tests.integration.conftest import _requires_db


def _role_exists(name: str) -> bool:
    with sample_engine.connect() as conn:
        result = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
            {"name": name},
        ).scalar()
    return result == 1


def _role_attr(name: str, attr: str):
    """Fetch a single attribute for a role; returns None if role missing."""
    with sample_engine.connect() as conn:
        return conn.execute(
            text(f"SELECT {attr} FROM pg_roles WHERE rolname = :name"),
            {"name": name},
        ).scalar()


@pytest.mark.integration
@_requires_db
class TestDataAgentReaderRole:
    """Verify the read-only role exists with correct privileges."""

    def test_role_exists(self):
        """The role must be created via scripts/create_reader_role.sql.

        If the role is missing, the deployment script was not run. Operators
        can fix this in production by running psql with admin credentials.
        In a CI-less environment without the role, we skip (rather than fail)
        because the role creation step requires admin privileges.
        """
        if _role_exists("data_agent_reader"):
            return  # success
        pytest.skip(
            "data_agent_reader role not provisioned; run "
            "scripts/create_reader_role.sql as deployment step."
        )

    def test_role_has_login_privilege(self):
        """The role must be LOGIN-capable (can authenticate)."""
        if not _role_exists("data_agent_reader"):
            pytest.skip("data_agent_reader role missing; see test_role_exists")

        rolcanlogin = _role_attr("data_agent_reader", "rolcanlogin")
        assert rolcanlogin is True, "data_agent_reader must be a LOGIN role"

    def test_role_has_no_superuser(self):
        """If the role has SUPERUSER, defense-in-depth is broken."""
        if not _role_exists("data_agent_reader"):
            pytest.skip("data_agent_reader role missing; see test_role_exists")

        rolsuper = _role_attr("data_agent_reader", "rolsuper")
        assert rolsuper is False, "data_agent_reader must NOT be SUPERUSER"
