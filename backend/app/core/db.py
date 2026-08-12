from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

meta_engine = create_engine(_settings.meta_database_url, pool_pre_ping=True)
MetaSession = sessionmaker(bind=meta_engine, expire_on_commit=False)

# Separate engine for business data: keeps metadata credentials distinct from
# the credentials used to run generated SQL.
sample_engine = create_engine(
    _settings.sample_database_url,
    pool_pre_ping=True,
    connect_args={"options": f"-c statement_timeout={_settings.query_timeout_seconds * 1000}"},
)


def get_meta_session() -> Iterator[Session]:
    session = MetaSession()
    try:
        yield session
    finally:
        session.close()


from app.semantic.orm import Base as MetaBase, META_SCHEMA  # noqa: F401,E402