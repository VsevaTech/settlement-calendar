"""Holiday calendars end to end: seeding, migration, UI routes, rules, exports."""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.services import calendar_store
from app.services.reconciliation import build_rows, upsert_rule
from app.services.settlement_calendar import (
    RuleType,
    SettlementCalendarError,
    SettlementStatus,
)
from tests.conftest import AS_OF


def upload_payments(client: TestClient, body: bytes) -> None:
    client.post("/upload/payments", files={"file": ("payments.csv", body, "text/csv")})


def save_rule(client: TestClient, provider: str, offset: int, calendar_code: str = "",
              rule_type: str = "BUSINESS_DAYS", follow: bool = False):
    return client.post(
        "/rules",
        data={
            "provider": provider,
            "offset_days": offset,
            "rule_type": rule_type,
            "calendar_code": calendar_code,
        },
        follow_redirects=follow,
    )


# --- storage -------------------------------------------------------------------------


def test_bundled_calendars_are_seeded_once(db_session: Session) -> None:
    calendars = calendar_store.load_calendars(db_session)
    assert {"AE", "TARGET2"} <= set(calendars)
    assert len(calendars["AE"].holidays) == 13
    assert calendar_store.seed_bundled_calendars(db_session) == []
    assert len(calendar_store.load_calendars(db_session)["AE"].holidays) == 13


def test_user_edits_to_a_bundled_calendar_survive_a_restart(db_session: Session) -> None:
    from app import db as db_module

    calendar_store.delete_holiday(db_session, "AE", date(2026, 8, 28))
    db_module.init_db()
    assert date(2026, 8, 28) not in calendar_store.load_calendars(db_session)["AE"].holidays


def test_v01_database_is_migrated_in_place(tmp_path: Path, monkeypatch) -> None:
    from app import db as db_module
    from app.config import reset_settings_cache

    path = tmp_path / "v01.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE provider_rules (id INTEGER PRIMARY KEY, provider VARCHAR(64) UNIQUE,"
                " offset_days INTEGER, rule_type VARCHAR(16), updated_at DATETIME)"
            )
        )
        connection.execute(
            text("INSERT INTO provider_rules (provider, offset_days, rule_type) "
                 "VALUES ('PSP_OLD', 2, 'BUSINESS_DAYS')")
        )
    engine.dispose()

    monkeypatch.setenv("SC_DATABASE_URL", f"sqlite:///{path}")
    reset_settings_cache()
    db_module.reset_engine()
    try:
        db_module.init_db()
        columns = {c["name"] for c in inspect(db_module.get_engine()).get_columns("provider_rules")}
        assert "calendar_code" in columns
        with db_module.session_scope() as session:
            from app.services.reconciliation import load_rules

            rules = load_rules(session)
            assert rules["PSP_OLD"].calendar is None
            assert rules["PSP_OLD"].label == "T+2 business days"
        db_module.init_db()  # idempotent
    finally:
        db_module.reset_engine()
        reset_settings_cache()


def test_unknown_calendar_is_refused(db_session: Session) -> None:
    with pytest.raises(SettlementCalendarError):
        upsert_rule(db_session, "PSP_A", 2, RuleType.BUSINESS_DAYS, calendar_code="NOPE")


def test_calendar_in_use_cannot_be_deleted(db_session: Session) -> None:
    upsert_rule(db_session, "PSP_A", 2, RuleType.BUSINESS_DAYS, calendar_code="AE")
    with pytest.raises(SettlementCalendarError, match="PSP_A"):
        calendar_store.delete_calendar(db_session, "AE")
    calendar_store.delete_calendar(db_session, "TARGET2")
    assert "TARGET2" not in calendar_store.load_calendars(db_session)


def test_adding_a_holiday_turns_overdue_into_due_today(db_session: Session) -> None:
    """The feature request example, end to end through the database."""
    from app.models.db_models import Payment

    session = db_session
    calendar_store.create_calendar(session, "DEMO", "Demo bank calendar", "6,7")
    upsert_rule(session, "PSP_A", 2, RuleType.BUSINESS_DAYS, calendar_code="DEMO")
    session.add(
        Payment(payment_id="p1", provider="PSP_A", payment_date=date(2026, 9, 14),
                amount="100.00", currency="EUR")
    )
    session.commit()

    (row,) = build_rows(session, AS_OF)
    assert row.expected_settlement_date == date(2026, 9, 16)
    assert row.status is SettlementStatus.OVERDUE

    calendar_store.upsert_holidays(session, "DEMO", {date(2026, 9, 16): "Bank Holiday"})
    (row,) = build_rows(session, AS_OF)
    assert row.expected_settlement_date == date(2026, 9, 17)
    assert row.status is SettlementStatus.DUE_TODAY
    assert row.explanation_text == (
        "14 Sep + T+2 → 17 Sep; 16 Sep skipped: bank holiday (Bank Holiday)"
    )


# --- HTTP ------------------------------------------------------------------------------


def test_rules_page_offers_calendars_and_shows_the_assignment(client: TestClient) -> None:
    page = client.get("/rules").text
    assert 'name="calendar_code"' in page
    assert "UAE banking calendar" in page and "TARGET2" in page

    assert save_rule(client, "PSP_A", 2, "AE").status_code == 303
    page = client.get("/rules").text
    assert "UAE banking calendar" in page
    assert "13 dates" in page


def test_rule_with_unknown_calendar_redirects_with_error(client: TestClient) -> None:
    response = save_rule(client, "PSP_A", 2, "NOPE")
    assert response.status_code == 303
    assert "error=unknown+calendar" in response.headers["location"]


def test_payment_detail_explains_the_holiday(client: TestClient) -> None:
    save_rule(client, "PSP_A", 2, "AE")
    upload_payments(
        client,
        b"payment_id,provider,payment_date,amount,currency\n"
        b"pay_hol,PSP_A,2026-08-26,10.00,USD\n",
    )
    page = client.get("/payments/pay_hol").text
    assert "<title>pay_hol</title>" in page
    assert page.count("How the expected date was calculated") == 1
    assert "26 Aug + T+2 → 31 Aug; 28 Aug skipped: bank holiday (Prophet&#39;s Birthday)" in page
    assert "skipped" in page
    dashboard = client.get("/").text
    assert 'class="tag holiday"' in dashboard


def test_calendar_coverage_warning_on_dashboard(client: TestClient) -> None:
    save_rule(client, "PSP_A", 2, "AE")
    upload_payments(
        client,
        b"payment_id,provider,payment_date,amount,currency\n"
        b"pay_2027,PSP_A,2027-01-04,10.00,EUR\n",
    )
    assert "has no holiday data for 2027" in client.get("/").text
    assert "has no holiday data for 2027" in client.get("/payments/pay_2027").text


def test_calendar_crud_through_the_ui(client: TestClient) -> None:
    listing = client.get("/calendars").text
    assert "UAE banking calendar" in listing and "TARGET2 (EUR) calendar" in listing

    response = client.post(
        "/calendars", data={"code": "il", "name": "Israel banks", "weekend_days": "5,6"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/calendars/IL")

    client.post("/calendars/IL/holidays", data={"holiday_date": "2026-09-21", "name": "Yom Kippur"})
    upload = client.post(
        "/calendars/IL/upload",
        files={"file": ("h.csv", b"date,name\n2026-09-26,Sukkot\nbad,row\n", "text/csv")},
        follow_redirects=False,
    )
    assert "1+added" in upload.headers["location"]
    assert "1+skipped" in upload.headers["location"]

    detail = client.get("/calendars/IL").text
    assert "Yom Kippur" in detail and "Sukkot" in detail

    exported = client.get("/calendars/IL/holidays.csv").text
    assert list(csv.reader(io.StringIO(exported))) == [
        ["date", "name"], ["2026-09-21", "Yom Kippur"], ["2026-09-26", "Sukkot"]
    ]

    client.post("/calendars/IL/holidays/delete", data={"holiday_date": "2026-09-26"})
    assert "Sukkot" not in client.get("/calendars/IL").text

    api = {item["code"]: item for item in client.get("/api/calendars").json()}
    assert api["IL"]["weekend_days"] == [5, 6]
    assert api["IL"]["holidays"] == {"2026-09-21": "Yom Kippur"}

    client.post("/calendars/IL/delete")
    assert client.get("/calendars/IL").status_code == 404


def test_duplicate_and_invalid_calendar_codes(client: TestClient) -> None:
    response = client.post("/calendars", data={"code": "AE", "name": "dup"},
                           follow_redirects=False)
    assert "already+exists" in response.headers["location"]
    response = client.post("/calendars", data={"code": "a b", "name": "x"},
                           follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_deleting_a_used_calendar_is_refused_in_the_ui(client: TestClient) -> None:
    save_rule(client, "PSP_A", 2, "AE")
    response = client.post("/calendars/AE/delete", follow_redirects=False)
    assert "error=" in response.headers["location"]
    assert client.get("/calendars/AE").status_code == 200


def test_exports_carry_calendar_and_explanation(client: TestClient) -> None:
    save_rule(client, "PSP_A", 2, "AE")
    upload_payments(
        client,
        b"payment_id,provider,payment_date,amount,currency\n"
        b"pay_hol,PSP_A,2026-08-26,10.00,USD\n",
    )
    rows = list(csv.DictReader(io.StringIO(client.get("/export/overdue.csv").text)))
    assert rows[0]["payment_id"] == "pay_hol"
    assert rows[0]["calendar"] == "AE"
    assert rows[0]["explanation"].startswith("26 Aug + T+2 → 31 Aug; 28 Aug skipped")
