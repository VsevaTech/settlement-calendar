"""Database engine, session factory and schema bootstrap."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, ensure_sqlite_directory, get_settings
from app.models.db_models import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def build_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    ensure_sqlite_directory(settings)
    is_sqlite = settings.database_url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    return create_engine(settings.database_url, future=True, connect_args=connect_args)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


def _migrate(engine: Engine) -> None:
    """Additive, idempotent schema changes for databases created by v0.1.

    ``create_all`` adds new tables but never new columns, so the one column
    added to an existing table is handled here. No data is rewritten.
    """
    columns = {column["name"] for column in inspect(engine).get_columns("provider_rules")}
    if "calendar_code" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE provider_rules ADD COLUMN calendar_code VARCHAR(32)")
            )


def init_db() -> None:
    """Create/upgrade the schema and seed the bundled holiday calendars.

    Safe on an empty database and on every restart: bundled calendars are only
    inserted when their code is missing, so edits made in the UI survive.
    """
    from app.services.calendar_store import seed_bundled_calendars

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _migrate(engine)
    with Session(engine) as session:
        seed_bundled_calendars(session)


def reset_engine() -> None:
    """Drop cached engine/session factory - used by tests."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
