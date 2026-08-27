"""Database engine and session factory.

All state lives in Postgres (Railway's filesystem is ephemeral). The engine is
created lazily so the app can boot and show a clear health status even before
DATABASE_URL is configured.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.settings import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine | None:
    global _engine, _session_factory
    if _engine is None:
        url = get_settings().sqlalchemy_url
        if url is None:
            return None
        _engine = create_engine(url, pool_pre_ping=True)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def db_session() -> Iterator[Session]:
    engine = get_engine()
    if engine is None or _session_factory is None:
        raise RuntimeError("DATABASE_URL is not configured")
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_db() -> str:
    """Health probe: 'ok', 'absent' (no DATABASE_URL), or 'error: ...'."""
    engine = get_engine()
    if engine is None:
        return "absent"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:  # pragma: no cover - depends on live DB
        return f"error: {type(exc).__name__}"
