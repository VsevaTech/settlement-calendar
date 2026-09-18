"""Calendar arithmetic and status rules - the heart of the product."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.settlement_calendar import (
    RuleType,
    SettlementCalendarError,
    SettlementRule,
    SettlementStatus,
    add_business_days,
    add_calendar_days,
    days_overdue,
    expected_settlement_date,
    is_business_day,
    parse_date,
    parse_money,
    parse_weekend_days,
    resolve_status,
)

MONDAY = date(2026, 9, 14)
TUESDAY = date(2026, 9, 15)
WEDNESDAY = date(2026, 9, 16)
THURSDAY = date(2026, 9, 17)
FRIDAY = date(2026, 9, 18)
SATURDAY = date(2026, 9, 19)
SUNDAY = date(2026, 9, 20)
NEXT_MONDAY = date(2026, 9, 21)
NEXT_TUESDAY = date(2026, 9, 22)


def rule(offset: int, rule_type: RuleType) -> SettlementRule:
    return SettlementRule("PSP_TEST", offset, rule_type)


def test_weekday_names_are_what_the_tests_assume() -> None:
    assert FRIDAY.strftime("%A") == "Friday"
    assert NEXT_TUESDAY.strftime("%A") == "Tuesday"


# --- calendar days -----------------------------------------------------------


def test_t_plus_1_calendar_day() -> None:
    assert expected_settlement_date(MONDAY, rule(1, RuleType.CALENDAR_DAYS)) == TUESDAY


def test_t_plus_3_calendar_days() -> None:
    assert expected_settlement_date(MONDAY, rule(3, RuleType.CALENDAR_DAYS)) == THURSDAY


def test_t_plus_3_calendar_days_crosses_the_weekend_without_skipping_it() -> None:
    # Calendar mode counts Saturday and Sunday.
    assert expected_settlement_date(FRIDAY, rule(3, RuleType.CALENDAR_DAYS)) == NEXT_MONDAY


def test_add_calendar_days_rejects_negative() -> None:
    with pytest.raises(SettlementCalendarError):
        add_calendar_days(MONDAY, -1)


# --- business days -----------------------------------------------------------


def test_t_plus_1_business_day() -> None:
    assert expected_settlement_date(MONDAY, rule(1, RuleType.BUSINESS_DAYS)) == TUESDAY


def test_t_plus_2_business_days() -> None:
    assert expected_settlement_date(MONDAY, rule(2, RuleType.BUSINESS_DAYS)) == WEDNESDAY


def test_friday_plus_1_business_day_is_monday() -> None:
    assert expected_settlement_date(FRIDAY, rule(1, RuleType.BUSINESS_DAYS)) == NEXT_MONDAY


def test_critical_case_friday_plus_2_business_days_is_tuesday() -> None:
    """The headline rule: a Friday payment on T+2 business days lands on Tuesday."""
    expected = expected_settlement_date(FRIDAY, rule(2, RuleType.BUSINESS_DAYS))
    assert expected == NEXT_TUESDAY
    assert expected.strftime("%A") == "Tuesday"


def test_saturday_and_sunday_are_not_business_days() -> None:
    assert is_business_day(FRIDAY) is True
    assert is_business_day(SATURDAY) is False
    assert is_business_day(SUNDAY) is False


def test_saturday_payment_settles_from_the_next_business_day() -> None:
    assert expected_settlement_date(SATURDAY, rule(0, RuleType.BUSINESS_DAYS)) == NEXT_MONDAY
    assert expected_settlement_date(SATURDAY, rule(1, RuleType.BUSINESS_DAYS)) == NEXT_TUESDAY
    assert expected_settlement_date(SUNDAY, rule(1, RuleType.BUSINESS_DAYS)) == NEXT_TUESDAY


def test_business_days_span_a_full_week() -> None:
    assert add_business_days(MONDAY, 5) == NEXT_MONDAY
    assert add_business_days(MONDAY, 10) == date(2026, 9, 28)


def test_custom_weekend_days_are_supported() -> None:
    # Friday/Saturday weekend, as used in parts of the Middle East.
    middle_east = SettlementRule(
        "PSP_ME", 1, RuleType.BUSINESS_DAYS, weekend_days=frozenset({5, 6})
    )
    assert expected_settlement_date(THURSDAY, middle_east) == SUNDAY


def test_invalid_rules_are_rejected() -> None:
    with pytest.raises(SettlementCalendarError):
        SettlementRule("PSP", -1, RuleType.BUSINESS_DAYS)
    with pytest.raises(SettlementCalendarError):
        SettlementRule(" ", 1, RuleType.BUSINESS_DAYS)
    with pytest.raises(SettlementCalendarError):
        SettlementRule("PSP", 1, RuleType.BUSINESS_DAYS, weekend_days=frozenset(range(1, 8)))


def test_rule_label_is_human_readable() -> None:
    assert rule(1, RuleType.BUSINESS_DAYS).label == "T+1 business day"
    assert rule(2, RuleType.BUSINESS_DAYS).label == "T+2 business days"
    assert rule(3, RuleType.CALENDAR_DAYS).label == "T+3 calendar days"


# --- statuses ----------------------------------------------------------------


def test_status_expected_when_the_date_has_not_arrived() -> None:
    assert resolve_status(FRIDAY, None, THURSDAY) is SettlementStatus.EXPECTED


def test_status_due_today() -> None:
    assert resolve_status(THURSDAY, None, THURSDAY) is SettlementStatus.DUE_TODAY


def test_status_overdue() -> None:
    assert resolve_status(WEDNESDAY, None, THURSDAY) is SettlementStatus.OVERDUE


def test_status_settled_on_time_and_early() -> None:
    assert resolve_status(THURSDAY, THURSDAY, THURSDAY) is SettlementStatus.SETTLED
    assert resolve_status(THURSDAY, WEDNESDAY, THURSDAY) is SettlementStatus.SETTLED


def test_critical_case_expected_15_actual_17_is_settled_late() -> None:
    assert (
        resolve_status(date(2026, 9, 15), date(2026, 9, 17), date(2026, 9, 17))
        is SettlementStatus.SETTLED_LATE
    )


def test_settled_late_stays_late_even_when_time_passes() -> None:
    late = resolve_status(date(2026, 9, 15), date(2026, 9, 17), date(2026, 12, 1))
    assert late is SettlementStatus.SETTLED_LATE


def test_days_overdue() -> None:
    assert days_overdue(date(2026, 9, 15), None, date(2026, 9, 17)) == 2
    assert days_overdue(date(2026, 9, 15), date(2026, 9, 17), date(2026, 9, 20)) == 2
    assert days_overdue(date(2026, 9, 20), None, date(2026, 9, 17)) == 0


# --- parsing -----------------------------------------------------------------


def test_parse_money_returns_decimal_not_float() -> None:
    value = parse_money("100.10")
    assert isinstance(value, Decimal)
    assert value == Decimal("100.10")
    assert parse_money("0.1") + parse_money("0.2") == Decimal("0.30")


def test_parse_money_handles_separators_and_symbols() -> None:
    assert parse_money("1 234,56") == Decimal("1234.56")
    assert parse_money("1,234.56") == Decimal("1234.56")
    assert parse_money("1.234,56") == Decimal("1234.56")
    assert parse_money("€ 99.00") == Decimal("99.00")
    assert parse_money(75) == Decimal("75.00")
    assert parse_money(Decimal("12.5")) == Decimal("12.50")


def test_parse_money_refuses_floats_and_empty_values() -> None:
    with pytest.raises(SettlementCalendarError):
        parse_money(10.5)
    with pytest.raises(SettlementCalendarError):
        parse_money("")
    with pytest.raises(SettlementCalendarError):
        parse_money(None)
    with pytest.raises(SettlementCalendarError):
        parse_money("not-a-number")


def test_parse_date_supports_common_formats() -> None:
    assert parse_date("2026-09-14") == MONDAY
    assert parse_date("14.09.2026") == MONDAY
    assert parse_date("14/09/2026") == MONDAY
    assert parse_date("2026-09-14T10:30:00") == MONDAY
    assert parse_date(MONDAY) == MONDAY


def test_parse_date_rejects_garbage() -> None:
    with pytest.raises(SettlementCalendarError):
        parse_date("14 Sep last year")
    with pytest.raises(SettlementCalendarError):
        parse_date("")


def test_parse_weekend_days() -> None:
    assert parse_weekend_days("6,7") == frozenset({6, 7})
    assert parse_weekend_days("") == frozenset({6, 7})
    assert parse_weekend_days("5,6") == frozenset({5, 6})
    with pytest.raises(SettlementCalendarError):
        parse_weekend_days("9")
