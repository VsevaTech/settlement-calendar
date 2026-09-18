"""Matching payments to settlements and turning them into dashboard rows."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.db_models import Payment, Settlement
from app.services.reconciliation import (
    build_rows,
    calendar_view,
    currency_totals,
    filter_rows,
    load_rules,
    overdue_rows,
    providers_without_rules,
    status_counts,
    upsert_rule,
)
from app.services.settlement_calendar import RuleType, SettlementStatus
from tests.conftest import AS_OF


def add_payment(
    session: Session,
    payment_id: str,
    provider: str,
    payment_date: date,
    amount: str,
    currency: str = "EUR",
) -> None:
    session.add(
        Payment(
            payment_id=payment_id,
            provider=provider,
            payment_date=payment_date,
            amount=Decimal(amount),
            currency=currency,
        )
    )
    session.commit()


def add_settlement(
    session: Session,
    settlement_id: str,
    payment_id: str,
    settlement_date: date,
    amount: str,
    currency: str = "EUR",
) -> None:
    session.add(
        Settlement(
            settlement_id=settlement_id,
            payment_id=payment_id,
            settlement_date=settlement_date,
            amount=Decimal(amount),
            currency=currency,
        )
    )
    session.commit()


def test_rules_are_persisted_and_reloaded(db_session: Session) -> None:
    upsert_rule(db_session, "PSP_A", 2, RuleType.BUSINESS_DAYS)
    upsert_rule(db_session, "PSP_A", 3, RuleType.CALENDAR_DAYS)  # update, not duplicate
    rules = load_rules(db_session)
    assert list(rules) == ["PSP_A"]
    assert rules["PSP_A"].offset_days == 3
    assert rules["PSP_A"].rule_type is RuleType.CALENDAR_DAYS


def test_all_five_statuses_are_produced(demo_rules: Session) -> None:
    session = demo_rules
    # PSP_B is T+1 business day.
    add_payment(session, "p_expected", "PSP_B", date(2026, 9, 17), "10.00")  # -> 18 Sep
    add_payment(session, "p_due", "PSP_B", date(2026, 9, 16), "20.00")  # -> 17 Sep
    add_payment(session, "p_overdue", "PSP_B", date(2026, 9, 14), "30.00")  # -> 15 Sep
    add_payment(session, "p_settled", "PSP_B", date(2026, 9, 11), "40.00")  # -> 14 Sep
    add_payment(session, "p_late", "PSP_B", date(2026, 9, 10), "50.00")  # -> 11 Sep

    add_settlement(session, "s1", "p_settled", date(2026, 9, 14), "40.00")
    add_settlement(session, "s2", "p_late", date(2026, 9, 15), "50.00")

    rows = {row.payment_id: row for row in build_rows(session, AS_OF)}
    assert rows["p_expected"].status is SettlementStatus.EXPECTED
    assert rows["p_due"].status is SettlementStatus.DUE_TODAY
    assert rows["p_overdue"].status is SettlementStatus.OVERDUE
    assert rows["p_settled"].status is SettlementStatus.SETTLED
    assert rows["p_late"].status is SettlementStatus.SETTLED_LATE
    assert rows["p_late"].days_overdue == 4
    assert rows["p_overdue"].days_overdue == 2

    assert status_counts(build_rows(session, AS_OF)) == {
        "EXPECTED": 1,
        "DUE_TODAY": 1,
        "OVERDUE": 1,
        "SETTLED": 1,
        "SETTLED_LATE": 1,
        "NO_RULE": 0,
    }


def test_matching_is_done_on_payment_id(demo_rules: Session) -> None:
    session = demo_rules
    add_payment(session, "pay_match", "PSP_A", date(2026, 9, 10), "100.00")
    add_payment(session, "pay_missing", "PSP_A", date(2026, 9, 10), "200.00")
    add_settlement(session, "stl_1", "pay_match", date(2026, 9, 14), "100.00")
    add_settlement(session, "stl_orphan", "pay_does_not_exist", date(2026, 9, 14), "999.00")

    rows = {row.payment_id: row for row in build_rows(session, AS_OF)}
    assert len(rows) == 2
    assert rows["pay_match"].settlement_id == "stl_1"
    assert rows["pay_match"].status is SettlementStatus.SETTLED
    assert rows["pay_missing"].actual_settlement_date is None
    assert rows["pay_missing"].status is SettlementStatus.OVERDUE


def test_earliest_settlement_wins_when_duplicated(demo_rules: Session) -> None:
    session = demo_rules
    add_payment(session, "pay_dup", "PSP_A", date(2026, 9, 9), "100.00")
    add_settlement(session, "stl_late", "pay_dup", date(2026, 9, 15), "50.00")
    add_settlement(session, "stl_early", "pay_dup", date(2026, 9, 11), "50.00")
    row = build_rows(session, AS_OF)[0]
    assert row.actual_settlement_date == date(2026, 9, 11)
    assert row.status is SettlementStatus.SETTLED


def test_payment_without_a_rule_is_flagged_not_guessed(db_session: Session) -> None:
    add_payment(db_session, "pay_norule", "PSP_UNKNOWN", date(2026, 9, 10), "100.00")
    row = build_rows(db_session, AS_OF)[0]
    assert row.has_rule is False
    assert row.status is None
    assert row.expected_settlement_date is None
    assert providers_without_rules(db_session) == ["PSP_UNKNOWN"]


def test_currencies_are_never_mixed(demo_rules: Session) -> None:
    session = demo_rules
    add_payment(session, "eur_overdue", "PSP_A", date(2026, 9, 10), "100.00", "EUR")
    add_payment(session, "usd_overdue", "PSP_A", date(2026, 9, 10), "200.00", "USD")
    add_payment(session, "ils_overdue", "PSP_A", date(2026, 9, 10), "300.00", "ILS")
    add_payment(session, "eur_upcoming", "PSP_A", date(2026, 9, 17), "40.00", "EUR")

    totals = {total.currency: total for total in currency_totals(build_rows(session, AS_OF), AS_OF)}
    assert set(totals) == {"EUR", "USD", "ILS"}
    assert totals["EUR"].overdue == Decimal("100.00")
    assert totals["USD"].overdue == Decimal("200.00")
    assert totals["ILS"].overdue == Decimal("300.00")
    assert totals["EUR"].upcoming == Decimal("40.00")
    assert totals["USD"].upcoming == Decimal("0.00")


def test_expected_and_received_today_split(demo_rules: Session) -> None:
    session = demo_rules
    # Both expected on 17 Sep under PSP_B (T+1 business day).
    add_payment(session, "paid_today", "PSP_B", date(2026, 9, 16), "60.00", "EUR")
    add_payment(session, "missing_today", "PSP_B", date(2026, 9, 16), "40.00", "EUR")
    add_settlement(session, "s_today", "paid_today", date(2026, 9, 17), "60.00", "EUR")

    total = currency_totals(build_rows(session, AS_OF), AS_OF)[0]
    assert total.expected_today == Decimal("100.00")
    assert total.received_today == Decimal("60.00")
    assert total.due_today == Decimal("40.00")
    assert total.due_today_count == 1


def test_filters_select_the_right_rows(demo_rules: Session) -> None:
    session = demo_rules
    add_payment(session, "p_overdue", "PSP_B", date(2026, 9, 14), "30.00")
    add_payment(session, "p_due", "PSP_B", date(2026, 9, 16), "20.00")
    rows = build_rows(session, AS_OF)

    assert [r.payment_id for r in filter_rows(rows, "overdue")] == ["p_overdue"]
    assert [r.payment_id for r in filter_rows(rows, "due_today")] == ["p_due"]
    assert len(filter_rows(rows, "all")) == 2
    assert len(filter_rows(rows, "nonsense")) == 2


def test_overdue_rows_are_sorted_worst_first(demo_rules: Session) -> None:
    session = demo_rules
    add_payment(session, "p_old", "PSP_B", date(2026, 9, 7), "30.00")  # -> 8 Sep
    add_payment(session, "p_recent", "PSP_B", date(2026, 9, 15), "30.00")  # -> 16 Sep
    rows = overdue_rows(build_rows(session, AS_OF))
    assert [r.payment_id for r in rows] == ["p_old", "p_recent"]
    assert rows[0].days_overdue > rows[1].days_overdue


def test_calendar_groups_by_expected_date_and_provider(demo_rules: Session) -> None:
    session = demo_rules
    add_payment(session, "a1", "PSP_A", date(2026, 9, 15), "100.00", "EUR")  # -> 17 Sep
    add_payment(session, "a2", "PSP_A", date(2026, 9, 15), "50.00", "EUR")  # -> 17 Sep
    add_payment(session, "b1", "PSP_B", date(2026, 9, 16), "70.00", "EUR")  # -> 17 Sep
    add_settlement(session, "s1", "a1", date(2026, 9, 17), "100.00", "EUR")

    days = calendar_view(build_rows(session, AS_OF), AS_OF)
    today = next(day for day in days if day.day == AS_OF)
    assert today.is_today is True
    cells = {cell.provider: cell for cell in today.cells}
    assert cells["PSP_A"].expected == Decimal("150.00")
    assert cells["PSP_A"].received == Decimal("100.00")
    assert cells["PSP_A"].outstanding == Decimal("50.00")
    assert cells["PSP_B"].expected == Decimal("70.00")
    assert cells["PSP_B"].count == 1
