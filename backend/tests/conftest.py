import pytest
from sqlalchemy.orm import Session

from app.core.db import meta_engine, sample_engine
from app.semantic.orm import Base
from scripts.init_db import create_schemas, load_sample_data

# Imported for the side effect of registering tables on MetaBase.metadata.
from app.security import orm as security_orm  # noqa: F401
from app.semantic import orm as semantic_orm  # noqa: F401


@pytest.fixture(scope="session", autouse=True)
def prepared_database():
    """Create schemas, tables and sample rows once per test session."""
    create_schemas()
    Base.metadata.create_all(meta_engine)
    load_sample_data()
    yield


@pytest.fixture
def meta_session() -> Session:
    """Metadata session rolled back after each test."""
    connection = meta_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def sample_conn():
    connection = sample_engine.connect()
    try:
        yield connection
    finally:
        connection.close()