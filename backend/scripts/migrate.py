#!/usr/bin/env python3
"""Database migration runner for data-agent.

This script manages applying SQL migrations from the migrations/ directory
to the metadata database. Migrations are tracked in _meta.schema_migrations
table to prevent duplicate application.

Usage:
    python scripts/migrate.py              # Apply all pending migrations
    python scripts/migrate.py --check      # Report pending migrations without applying
    python scripts/migrate.py --status     # Show migration status

Environment:
    Uses settings from .env or environment variables (META_DATABASE_URL, etc)
"""

import os
import re
import sys
import time
from pathlib import Path
from typing import List

# Add backend to path so we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DatabaseError, IntegrityError

from app.core.config import get_settings


def get_migrations_dir() -> Path:
    """Get the migrations directory path."""
    return Path(__file__).parent.parent / "migrations"


def parse_migration_files() -> list[tuple[int, str, Path]]:
    """Parse migration files, return list of (version, name, path) sorted by version.

    Filenames must match pattern: NNN_descriptive_name.sql
    Where NNN is a 3-digit version number (001, 002, etc).

    Returns:
        List of (version_int, name_str, path) tuples sorted by version
    """
    migrations = []
    migrations_dir = get_migrations_dir()

    if not migrations_dir.exists():
        return migrations

    for file in migrations_dir.glob("*.sql"):
        match = re.match(r"^(\d+)_(.+)\.sql$", file.name)
        if match:
            version = int(match.group(1))
            name = match.group(2)
            migrations.append((version, name, file))

    # Sort by version number
    migrations.sort(key=lambda x: x[0])
    return migrations


def get_applied_versions(engine) -> set[int]:
    """Get set of already-applied migration versions from database.

    Creates schema_migrations table if it doesn't exist.

    Args:
        engine: SQLAlchemy engine connected to metadata database

    Returns:
        Set of version numbers that have been applied
    """
    try:
        with engine.connect() as conn:
            # Ensure table exists (idempotent)
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS _meta.schema_migrations (
                    version INT PRIMARY KEY,
                    name VARCHAR(255) NOT NULL UNIQUE,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_schema_migrations_applied_at
                  ON _meta.schema_migrations(applied_at DESC);
            """))
            conn.commit()

            # Get list of applied versions
            result = conn.execute(text(
                "SELECT version FROM _meta.schema_migrations ORDER BY version"
            ))
            # Convert result to list to ensure full iteration
            rows = result.fetchall()
            return {row[0] for row in rows}
    except DatabaseError as e:
        print(f"ERROR: Failed to check applied migrations: {e}", file=sys.stderr)
        sys.exit(1)


def apply_migration(engine, version: int, name: str, path: Path) -> bool:
    """Apply a single migration file.

    Reads SQL from file and executes it within a transaction.
    Records the migration version in schema_migrations on success.
    On failure, transaction is rolled back and migration is not recorded.

    Args:
        engine: SQLAlchemy engine
        version: Migration version number
        name: Migration name (from filename)
        path: Path to SQL file

    Returns:
        True if successful, False if failed
    """
    try:
        with open(path, encoding="utf-8") as f:
            sql = f.read()
    except IOError as e:
        print(f"ERROR: Failed to read migration file {path}: {e}", file=sys.stderr)
        return False

    try:
        with engine.begin() as conn:
            # Execute the migration SQL
            conn.execute(text(sql))

            # Record the migration as applied
            conn.execute(text("""
                INSERT INTO _meta.schema_migrations (version, name)
                VALUES (:version, :name)
            """), {"version": version, "name": name})

        print(f"✓ Applied: {version:03d}_{name}.sql")
        return True
    except (DatabaseError, IntegrityError) as e:
        print(f"✗ Failed to apply {version:03d}_{name}.sql: {e}", file=sys.stderr)
        return False


def status_migrations(migrations: list[tuple[int, str, Path]], applied: set[int]) -> None:
    """Print migration status report."""
    if not migrations:
        print("No migrations found in migrations/ directory")
        return

    pending = [(v, n, p) for v, n, p in migrations if v not in applied]
    applied_list = [(v, n, p) for v, n, p in migrations if v in applied]

    if applied_list:
        print("\nApplied migrations:")
        for v, n, _ in applied_list:
            print(f"  ✓ {v:03d}_{n}.sql")

    if pending:
        print(f"\nPending migrations ({len(pending)}):")
        for v, n, _ in pending:
            print(f"  ○ {v:03d}_{n}.sql")
    else:
        if applied_list:
            print("\nAll migrations applied ✓")


def main() -> int:
    """Main entry point."""
    # Parse command line arguments
    check_mode = "--check" in sys.argv
    status_mode = "--status" in sys.argv
    help_mode = "--help" in sys.argv or "-h" in sys.argv

    if help_mode:
        print(__doc__)
        return 0

    # Load settings and create engine
    try:
        settings = get_settings()
    except Exception as e:
        print(f"ERROR: Failed to load settings: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        engine = create_engine(settings.meta_database_url, pool_pre_ping=True)
    except Exception as e:
        print(f"ERROR: Failed to create database engine: {e}", file=sys.stderr)
        sys.exit(1)

    # Parse and sort migrations
    migrations = parse_migration_files()
    applied_versions = get_applied_versions(engine)
    pending = [(v, n, p) for v, n, p in migrations if v not in applied_versions]

    # Handle status mode
    if status_mode:
        status_migrations(migrations, applied_versions)
        return 0

    # Handle check mode
    if check_mode:
        if pending:
            print(f"! {len(pending)} pending migration(s):")
            for v, n, _ in pending:
                print(f"  {v:03d}_{n}.sql")
            return 1
        else:
            print("✓ All migrations applied")
            return 0

    # Apply pending migrations
    if not pending:
        print("✓ All migrations already applied")
        return 0

    print(f"Applying {len(pending)} migration(s)...")
    failed = 0
    for v, n, p in pending:
        if not apply_migration(engine, v, n, p):
            failed += 1

    if failed:
        print(f"\n✗ {failed} migration(s) failed", file=sys.stderr)
        return 1
    else:
        print(f"\n✓ Successfully applied {len(pending)} migration(s)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
