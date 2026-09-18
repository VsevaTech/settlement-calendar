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
        if row.provider == "PSP_A" and row.expected_settlement_date is not None
    ]
    assert all(
        row.expected_settlement_date.isoweekday() == 2 for row in friday_business
    ), "Friday + T+2 business days must land on Tuesday"


def test_as_of_is_a_thursday() -> None:
    assert date(2026, 9, 17) == AS_OF
    assert AS_OF.strftime("%A") == "Thursday"
