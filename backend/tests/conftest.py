import pytest
from sqlalchemy.orm import Session

from app.core.db import meta_engine, sample_engine_writable as sample_engine
from app.semantic.orm import Base
from scripts.init_db import create_schemas, load_sample_data
from scripts.seed_roles import seed_roles
from app.core.db import MetaSession

# Imported for the side effect of registering tables on MetaBase.metadata.
from app.security import orm as security_orm  # noqa: F401
from app.semantic import orm as semantic_orm  # noqa: F401
from app.observability import orm as observability_orm  # noqa: F401


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "no_db: mark test as not requiring database"
    )


@pytest.fixture(scope="session", autouse=True)
def prepared_database(request):
    """Create schemas, tables and sample rows once per test session.

    Drop-then-create keeps ORM-defined columns in sync with what tests
    expect — `create_all` only adds missing tables, never columns.

    Skipped if all tests are marked with 'no_db'.
    """
    # Check if all tests are marked no_db
    if request.config.option.markexpr and "no_db" in str(request.config.option.markexpr):
        yield  # Skip DB setup
        return

    create_schemas()
    Base.metadata.drop_all(meta_engine)
    Base.metadata.create_all(meta_engine)
    with MetaSession() as session:
        seed_roles(session)
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