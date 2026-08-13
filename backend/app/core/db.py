from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

meta_engine = create_engine(_settings.meta_database_url, pool_pre_ping=True)
MetaSession = sessionmaker(bind=meta_engine, expire_on_commit=False)


def _build_sample_engine(*, read_only: bool) -> Engine:
    """Build a sample engine with or without connection-level read-only.

    The runtime path forces `default_transaction_read_only=on` so the
    warehouse physically cannot accept writes — defence in depth on top
    of the AST guardrails. The test setup path needs DDL (schema create,
    sample-data load) so it builds a writable engine. Both share the same
    URL; only the per-connection options differ.
    """
    options = [
        f"-c statement_timeout={_settings.query_timeout_seconds * 1000}",
        "-c lock_timeout=2000",
        "-c idle_in_transaction_session_timeout=10000",
    ]
    if read_only:
        options.append("-c default_transaction_read_only=on")
    return create_engine(
        _settings.sample_database_url,
        pool_pre_ping=True,
        connect_args={"options": " ".join(options)},
    )


# Runtime engine: every connection from this pool refuses writes.
sample_engine = _build_sample_engine(read_only=True)
# Test-only engine: writable, used by the conftest fixtures to load sample
# data. Not exposed via a dependency — nothing in the runtime ever sees it.
sample_engine_writable = _build_sample_engine(read_only=False)


def get_meta_session() -> Iterator[Session]:
    """Per-request metadata session with explicit commit/rollback boundary.

    The previous implementation only closed the session, never committed —
    a request that wrote a Turn row would lose it the moment the session
    was closed, because every subsequent request would get a new session
    that sees the rolled-back transaction. The fix has two halves:

    - On a clean exit, `commit()` so the writes survive the request.
    - On an exception, `rollback()` and re-raise so partial writes do not
      leak into the metadata DB (which would let a 404 leave a Turn row
      pointing at a Conversation that does not exist).
    """
    session = MetaSession()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_sample_connection() -> Iterator["Connection"]:
    """Read-only business-data connection. Committing is never needed here;
    the connection is closed per request so a cancelled query cannot leak."""
    with sample_engine.connect() as connection:
        yield connection


from app.semantic.orm import Base as MetaBase, META_SCHEMA  # noqa: F401,E402