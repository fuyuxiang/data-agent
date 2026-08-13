"""Create schemas, metadata tables and load sample business data.

Run: python -m scripts.init_db

WARNING: This script contains DROP TABLE and TRUNCATE operations.
It is NOT safe to run in production. The check below will prevent accidental
execution if ENVIRONMENT=production.
"""

import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import MetaSession, meta_engine, sample_engine_writable as sample_engine
from app.semantic.orm import META_SCHEMA, Base

# Imported for the side effect of registering tables on MetaBase.metadata.
from app.security import orm as security_orm  # noqa: F401
from app.semantic import orm as semantic_orm  # noqa: F401
from app.observability import orm as observability_orm  # noqa: F401

from scripts.seed_roles import seed_roles

SAMPLE_SCHEMA = "sample"
SQL_FILE = Path(__file__).parent / "sample_data.sql"


def check_production_safety() -> None:
    """Prevent accidental database initialization in production."""
    settings = get_settings()
    if settings.environment == "production":
        raise RuntimeError(
            "FATAL: Cannot run init_db.py in production environment. "
            "This script contains destructive operations (DROP TABLE, TRUNCATE). "
            "Set ENVIRONMENT=development to proceed, or manage schema manually."
        )


def create_schemas() -> None:
    with meta_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{META_SCHEMA}"'))
    with sample_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SAMPLE_SCHEMA}"'))


def create_meta_tables() -> None:
    Base.metadata.create_all(meta_engine)


def seed_platform_roles() -> None:
    """Seed the six platform roles into the freshly built metadata schema."""
    with MetaSession() as session:
        seed_roles(session)


def load_sample_data() -> None:
    statements = SQL_FILE.read_text(encoding="utf-8")
    with sample_engine.begin() as conn:
        conn.execute(text(statements))
        # Without ANALYZE the planner falls back to hard-coded estimates
        # (PostgreSQL default is ~150 rows per Seq Scan), which makes
        # EXPLAIN-based cost guardrails misleading on freshly loaded data.
        conn.execute(text("ANALYZE sample.orders"))


def main() -> None:
    check_production_safety()
    create_schemas()
    create_meta_tables()
    seed_platform_roles()
    load_sample_data()
    print("database initialised")


if __name__ == "__main__":
    main()