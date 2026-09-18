"""Shared fixtures. Every test pins ``as_of`` so nothing depends on the real clock."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.config import reset_settings_cache
from app.services.settlement_calendar import RuleType

AS_OF = date(2026, 9, 17)  # a Thursday
REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = REPO_ROOT / "demo-data"


@pytest.fixture
def db_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    monkeypatch.setenv("SC_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SC_AS_OF_DATE", AS_OF.isoformat())
    monkeypatch.setenv("SC_WEEKEND_DAYS", "6,7")
    reset_settings_cache()

    from app import db as db_module

    db_module.reset_engine()
    db_module.init_db()
    session = db_module.get_session_factory()()
    try:
        yield session
    finally:
        session.close()
        db_module.reset_engine()
        reset_settings_cache()


@pytest.fixture
def demo_rules(db_session: Session) -> Session:
    from app.services.reconciliation import upsert_rule

    upsert_rule(db_session, "PSP_A", 2, RuleType.BUSINESS_DAYS)
    upsert_rule(db_session, "PSP_B", 1, RuleType.BUSINESS_DAYS)
    upsert_rule(db_session, "PSP_C", 3, RuleType.CALENDAR_DAYS)
    return db_session


@pytest.fixture
def client(db_session: Session) -> Iterator[object]:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
