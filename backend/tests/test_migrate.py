"""Tests for database migration system.

Tests the migrate.py script and migration infrastructure:
- Migration file parsing
- Applied migrations tracking
- Migration execution
- Error handling
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, DatabaseError

# We'll test the migrate module by importing key functions
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.migrate import (
    parse_migration_files,
    get_applied_versions,
    apply_migration,
    status_migrations,
)


class TestParseMigrationFiles:
    """Test migration file discovery and parsing."""

    def test_parse_valid_migrations(self, tmp_path):
        """Parse valid migration files with correct naming."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()

        # Create test migration files
        (migrations_dir / "001_init.sql").write_text("SELECT 1;")
        (migrations_dir / "002_add_users.sql").write_text("CREATE TABLE users;")
        (migrations_dir / "010_add_index.sql").write_text("CREATE INDEX idx;")

        # Mock get_migrations_dir to return our temp dir
        with patch("scripts.migrate.get_migrations_dir", return_value=migrations_dir):
            migrations = parse_migration_files()

        assert len(migrations) == 3
        assert migrations[0] == (1, "init", migrations_dir / "001_init.sql")
        assert migrations[1] == (2, "add_users", migrations_dir / "002_add_users.sql")
        assert migrations[2] == (10, "add_index", migrations_dir / "010_add_index.sql")

    def test_parse_ignores_invalid_filenames(self, tmp_path):
        """Skip files that don't match the migration naming pattern."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()

        (migrations_dir / "001_init.sql").write_text("SELECT 1;")
        (migrations_dir / "invalid_name.sql").write_text("SELECT 1;")
        (migrations_dir / "README.md").write_text("# Migrations")
        (migrations_dir / "abc_name.sql").write_text("SELECT 1;")

        with patch("scripts.migrate.get_migrations_dir", return_value=migrations_dir):
            migrations = parse_migration_files()

        assert len(migrations) == 1
        assert migrations[0][1] == "init"

    def test_parse_empty_directory(self, tmp_path):
        """Handle empty migrations directory gracefully."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()

        with patch("scripts.migrate.get_migrations_dir", return_value=migrations_dir):
            migrations = parse_migration_files()

        assert migrations == []

    def test_parse_sorts_by_version(self, tmp_path):
        """Migrations are sorted by version number, not filename."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()

        # Create files out of order
        (migrations_dir / "010_second.sql").write_text("SELECT 1;")
        (migrations_dir / "001_first.sql").write_text("SELECT 1;")
        (migrations_dir / "005_third.sql").write_text("SELECT 1;")

        with patch("scripts.migrate.get_migrations_dir", return_value=migrations_dir):
            migrations = parse_migration_files()

        versions = [v for v, _, _ in migrations]
        assert versions == [1, 5, 10]


class TestAppliedVersions:
    """Test tracking of applied migrations in database."""

    def test_get_applied_versions_creates_table(self):
        """Ensure schema_migrations table is created if missing."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.connect.return_value.__enter__.return_value = mock_conn

        # Mock SELECT to return empty result
        mock_select_result = MagicMock()
        mock_select_result.__iter__ = lambda: iter([])
        mock_conn.execute.return_value = mock_select_result
        mock_conn.commit.return_value = None

        applied = get_applied_versions(mock_engine)

        assert applied == set()

    def test_get_applied_versions_returns_set(self):
        """Return set of applied version numbers."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.connect.return_value.__enter__.return_value = mock_conn

        # Mock SELECT result with fetchall returning 3 versions
        mock_select_result = MagicMock()
        mock_select_result.fetchall.return_value = [(1,), (2,), (5,)]
        mock_conn.execute.return_value = mock_select_result
        mock_conn.commit.return_value = None

        applied = get_applied_versions(mock_engine)

        assert applied == {1, 2, 5}


class TestApplyMigration:
    """Test individual migration application."""

    def test_apply_migration_success(self, tmp_path):
        """Successfully apply a migration file."""
        migration_file = tmp_path / "001_test.sql"
        migration_file.write_text("CREATE TABLE test (id INT);")

        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__.return_value = mock_conn

        result = apply_migration(mock_engine, 1, "test", migration_file)

        assert result is True
        # Verify execute was called twice (SQL + INSERT)
        assert mock_conn.execute.call_count == 2

    def test_apply_migration_file_not_found(self, tmp_path):
        """Handle missing migration file gracefully."""
        migration_file = tmp_path / "nonexistent.sql"

        mock_engine = MagicMock()
        result = apply_migration(mock_engine, 1, "test", migration_file)

        assert result is False

    def test_apply_migration_sql_error(self, tmp_path):
        """Handle SQL errors during migration."""
        migration_file = tmp_path / "001_bad.sql"
        migration_file.write_text("INVALID SQL SYNTAX !!!;")

        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.side_effect = DatabaseError("syntax error", None, None)

        result = apply_migration(mock_engine, 1, "bad", migration_file)

        assert result is False

    def test_apply_migration_duplicate_version(self, tmp_path):
        """Handle duplicate version error."""
        migration_file = tmp_path / "001_test.sql"
        migration_file.write_text("SELECT 1;")

        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__.return_value = mock_conn
        # First execute (SQL) succeeds, second (INSERT) fails with duplicate
        mock_conn.execute.side_effect = [None, IntegrityError("duplicate", None, None)]

        result = apply_migration(mock_engine, 1, "test", migration_file)

        assert result is False


class TestStatusMigrations:
    """Test migration status reporting."""

    def test_status_no_migrations(self, capsys):
        """Handle case with no migrations."""
        status_migrations([], set())
        captured = capsys.readouterr()
        assert "No migrations found" in captured.out

    def test_status_all_applied(self, capsys):
        """Report when all migrations are applied."""
        migrations = [
            (1, "init", Path("001_init.sql")),
            (2, "add_users", Path("002_add_users.sql")),
        ]
        applied = {1, 2}

        status_migrations(migrations, applied)
        captured = capsys.readouterr()

        assert "Pending migrations (0)" in captured.out or "All migrations applied" in captured.out
        assert "001_init.sql" in captured.out
        assert "002_add_users.sql" in captured.out

    def test_status_mixed(self, capsys):
        """Report mix of applied and pending."""
        migrations = [
            (1, "init", Path("001_init.sql")),
            (2, "add_users", Path("002_add_users.sql")),
            (3, "add_roles", Path("003_add_roles.sql")),
        ]
        applied = {1}

        status_migrations(migrations, applied)
        captured = capsys.readouterr()

        assert "001_init.sql" in captured.out
        assert "002_add_users.sql" in captured.out
        assert "003_add_roles.sql" in captured.out
        assert "Pending migrations (2)" in captured.out
