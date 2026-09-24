"""The bundled demo dataset must always show the documented numbers."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.demo.generate import (
    DEFAULT_AS_OF,
    TARGET_DUE_TODAY,
    TARGET_OVERDUE,
    TARGET_SETTLED_LATE,
    generate,
    status_summary,
)
from app.services.import_service import import_file
from app.services.reconciliation import build_rows, status_counts
from app.services.settlement_calendar import RuleType, SettlementStatus
from tests.conftest import AS_OF, DEMO_DIR


def test_generator_is_deterministic() -> None:
    first = generate(DEFAULT_AS_OF)
    second = generate(DEFAULT_AS_OF)
    assert [(p.payment_id, p.amount, p.settlement_date) for p in first] == [
        (p.payment_id, p.amount, p.settlement_date) for p in second
    ]


def test_generator_hits_the_documented_counts() -> None:
    payments = generate(DEFAULT_AS_OF)
    assert 150 <= len(payments) <= 300
    counts = status_summary(payments, DEFAULT_AS_OF)
    assert counts["OVERDUE"] == TARGET_OVERDUE
    assert counts["DUE_TODAY"] == TARGET_DUE_TODAY
    assert counts["SETTLED_LATE"] == TARGET_SETTLED_LATE
    assert counts["SETTLED"] > 0
    assert counts["EXPECTED"] > 0


def test_committed_demo_files_reproduce_the_documented_counts(demo_rules: Session) -> None:
    session = demo_rules
    payments_csv = (DEMO_DIR / "payments.csv").read_bytes()
    settlements_csv = (DEMO_DIR / "settlements.csv").read_bytes()

    payments_result = import_file(session, payments_csv, "payments.csv", "payments")
    settlements_result = import_file(session, settlements_csv, "settlements.csv", "settlements")
    assert payments_result.skipped == 0
    assert settlements_result.skipped == 0

    counts = status_counts(build_rows(session, AS_OF))
    assert counts["OVERDUE"] == TARGET_OVERDUE
    assert counts["DUE_TODAY"] == TARGET_DUE_TODAY
    assert counts["SETTLED_LATE"] == TARGET_SETTLED_LATE
    assert counts["NO_RULE"] == 0


def test_demo_dataset_covers_the_interesting_cases(demo_rules: Session) -> None:
    session = demo_rules
    import_file(session, (DEMO_DIR / "payments.csv").read_bytes(), "payments.csv", "payments")
    import_file(
        session, (DEMO_DIR / "settlements.csv").read_bytes(), "settlements.csv", "settlements"
    )
    rows = build_rows(session, AS_OF)

    assert {row.currency for row in rows} == {"EUR", "USD", "ILS"}
    assert {row.provider for row in rows} == {"PSP_A", "PSP_B", "PSP_C"}
    # Friday payments settling after the weekend, and weekend payments.
    fridays = [row for row in rows if row.payment_date.isoweekday() == 5]
    assert fridays, "demo data must contain Friday payments"
    assert any(row.payment_date.isoweekday() in (6, 7) for row in rows)
    friday_business = [
        row
        for row in fridays
        if row.provider == "PSP_A"
        and row.expected_settlement_date is not None
        and not row.holiday_adjusted
    ]
    assert friday_business
    assert all(
        row.expected_settlement_date.isoweekday() == 2 for row in friday_business
    ), "Friday + T+2 business days must land on Tuesday when no bank holiday intervenes"


def test_demo_dataset_crosses_a_real_uae_bank_holiday(demo_rules: Session) -> None:
    """Prophet's Birthday (Fri 28 Aug 2026) moves PSP_A payouts - and they are on time."""
    session = demo_rules
    import_file(session, (DEMO_DIR / "payments.csv").read_bytes(), "payments.csv", "payments")
    import_file(
        session, (DEMO_DIR / "settlements.csv").read_bytes(), "settlements.csv", "settlements"
    )
    rows = {row.payment_id: row for row in build_rows(session, AS_OF)}
    adjusted = [row for row in rows.values() if row.holiday_adjusted]

    assert len(adjusted) == 13
    assert {row.provider for row in adjusted} == {"PSP_A"}
    assert all(row.actual_settlement_date is not None for row in adjusted)

    showcase = rows["pay_0021"]
    assert showcase.payment_date == date(2026, 8, 26)
    assert showcase.expected_settlement_date == date(2026, 8, 31)
    assert showcase.status is SettlementStatus.SETTLED
    assert showcase.explanation_text == (
        "26 Aug + T+2 → 31 Aug; 28 Aug skipped: bank holiday (Prophet's Birthday); "
        "29 Aug, 30 Aug skipped: weekend"
    )


def test_without_the_calendar_the_holiday_payouts_look_late(db_session: Session) -> None:
    """The reason the feature exists: weekends-only would raise 12 false alarms."""
    from app.services.reconciliation import upsert_rule

    session = db_session
    upsert_rule(session, "PSP_A", 2, RuleType.BUSINESS_DAYS)  # no calendar
    upsert_rule(session, "PSP_B", 1, RuleType.BUSINESS_DAYS)
    upsert_rule(session, "PSP_C", 3, RuleType.CALENDAR_DAYS)
    import_file(session, (DEMO_DIR / "payments.csv").read_bytes(), "payments.csv", "payments")
    import_file(
        session, (DEMO_DIR / "settlements.csv").read_bytes(), "settlements.csv", "settlements"
    )
    counts = status_counts(build_rows(session, AS_OF))
    assert counts["SETTLED_LATE"] == TARGET_SETTLED_LATE + 12


def test_as_of_is_a_thursday() -> None:
    assert date(2026, 9, 17) == AS_OF
    assert AS_OF.strftime("%A") == "Thursday"
