"""Create schemas, metadata tables and load sample business data.

Run: python -m scripts.init_db
"""

from pathlib import Path

from sqlalchemy import text

from app.core.db import meta_engine, sample_engine
from app.semantic.orm import META_SCHEMA, Base

# Imported for the side effect of registering tables on MetaBase.metadata.
from app.security import orm as security_orm  # noqa: F401
from app.semantic import orm as semantic_orm  # noqa: F401

SAMPLE_SCHEMA = "sample"
SQL_FILE = Path(__file__).parent / "sample_data.sql"


def create_schemas() -> None:
    with meta_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{META_SCHEMA}"'))
    with sample_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SAMPLE_SCHEMA}"'))


def create_meta_tables() -> None:
    Base.metadata.create_all(meta_engine)


def load_sample_data() -> None:
    statements = SQL_FILE.read_text(encoding="utf-8")
    with sample_engine.begin() as conn:
        conn.execute(text(statements))


def main() -> None:
    create_schemas()
    create_meta_tables()
    load_sample_data()
    print("database initialised")


if __name__ == "__main__":
    main()