"""Bank-holiday aware T+N arithmetic and the explanation shown to users."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from app.services.holiday_calendars import (
    bundled_calendars,
    calendar_from_mapping,
    holidays_to_csv,
    normalise_code,
    parse_holiday_csv,
)
from app.services.settlement_calendar import (
    HolidayCalendar,
    RuleType,
    SettlementCalendarError,
    SettlementRule,
    add_business_days,
    expected_settlement_date,
    explain_settlement,
    is_business_day,
)

AE = bundled_calendars()["AE"]
TARGET2 = bundled_calendars()["TARGET2"]


def cal(*days: date, name: str = "Test Day", weekend: frozenset[int] | None = None):
    return HolidayCalendar("TEST", "Test calendar", dict.fromkeys(days, name), weekend)


def rule(offset: int, rule_type=RuleType.BUSINESS_DAYS, calendar=None) -> SettlementRule:
    return SettlementRule("PSP", offset, rule_type, calendar=calendar)


# --- the example from the feature request ---------------------------------------


def test_t_plus_2_through_a_midweek_bank_holiday() -> None:
    holiday = cal(date(2026, 9, 16), name="Bank Holiday")
    explanation = explain_settlement(date(2026, 9, 14), rule(2, calendar=holiday))
    assert explanation.expected_date == date(2026, 9, 17)
    assert explanation.summary == (
        "14 Sep + T+2 → 17 Sep; 16 Sep skipped: bank holiday (Bank Holiday)"
    )
    assert explanation.holiday_adjusted
    assert [(s.day.day, s.label) for s in explanation.steps] == [
        (14, "T+0"),
        (15, "T+1"),
        (16, "skipped"),
        (17, "T+2"),
    ]


def test_same_payment_without_a_calendar_is_unchanged() -> None:
    explanation = explain_settlement(date(2026, 9, 14), rule(2))
    assert explanation.expected_date == date(2026, 9, 16)
    assert explanation.summary == "14 Sep + T+2 → 16 Sep"
    assert not explanation.holiday_adjusted
    assert explanation.warnings == ()


# --- T+N across holidays ------------------------------------------------------------


def test_t_plus_1_friday_with_a_monday_holiday_lands_on_tuesday() -> None:
    holiday = cal(date(2026, 9, 21))
    assert expected_settlement_date(date(2026, 9, 18), rule(1, calendar=holiday)) == date(
        2026, 9, 22
    )


def test_real_uae_holiday_prophets_birthday_2026() -> None:
    # Wed 26 Aug + T+2: Thu 27 = T+1, Fri 28 holiday, Sat/Sun weekend, Mon 31 = T+2.
    explanation = explain_settlement(date(2026, 8, 26), rule(2, calendar=AE))
    assert explanation.expected_date == date(2026, 8, 31)
    assert explanation.summary == (
        "26 Aug + T+2 → 31 Aug; 28 Aug skipped: bank holiday (Prophet's Birthday); "
        "29 Aug, 30 Aug skipped: weekend"
    )


def test_payment_on_a_bank_holiday_starts_counting_on_the_next_business_day() -> None:
    explanation = explain_settlement(date(2026, 8, 28), rule(2, calendar=AE))
    assert explanation.expected_date == date(2026, 9, 2)
    first = explanation.steps[0]
    assert (first.day, first.label) == (date(2026, 8, 28), "skipped")
    t0 = next(step for step in explanation.steps if step.label == "T+0")
    assert t0.day == date(2026, 8, 31)
    assert "counting starts" in t0.note


def test_t_plus_0_on_a_holiday_moves_to_the_next_business_day() -> None:
    assert expected_settlement_date(date(2026, 6, 15), rule(0, calendar=AE)) == date(2026, 6, 16)
    assert expected_settlement_date(date(2026, 6, 15), rule(0)) == date(2026, 6, 15)


def test_a_multi_day_eid_break_is_skipped_as_a_block() -> None:
    # Mon 25 May + T+2: Arafat + Eid Al Adha Tue 26 - Fri 29, then the weekend.
    explanation = explain_settlement(date(2026, 5, 25), rule(2, calendar=AE))
    assert explanation.expected_date == date(2026, 6, 2)
    assert len(explanation.skipped) == 6
    assert explanation.summary.startswith("25 May + T+2 → 2 Jun; 26 May skipped: bank holiday")


def test_a_holiday_on_a_weekend_is_not_counted_twice() -> None:
    # Eid Al Fitr 2026: Thu 19 - Sun 22 Mar; Sat/Sun are both weekend and holiday.
    assert expected_settlement_date(date(2026, 3, 18), rule(2, calendar=AE)) == date(2026, 3, 24)
    explanation = explain_settlement(date(2026, 3, 18), rule(2, calendar=AE))
    assert [s.day.day for s in explanation.skipped] == [19, 20, 21, 22]
    assert all(s.reason.startswith("bank holiday") for s in explanation.skipped)


def test_target2_easter_closing_days() -> None:
    # Thu 2 Apr 2026 + T+1: Good Friday, weekend, Easter Monday -> Tue 7 Apr.
    assert expected_settlement_date(date(2026, 4, 2), rule(1, calendar=TARGET2)) == date(
        2026, 4, 7
    )


def test_holidays_are_per_calendar() -> None:
    day = date(2026, 8, 28)
    assert not is_business_day(day, calendar=AE)
    assert is_business_day(day, calendar=TARGET2)
    assert is_business_day(day)


# --- calendar-day rules --------------------------------------------------------------


def test_calendar_days_without_a_calendar_may_land_on_a_weekend() -> None:
    # Unchanged v0.1 behaviour: no calendar, no roll-forward.
    assert expected_settlement_date(
        date(2026, 9, 18), rule(1, RuleType.CALENDAR_DAYS)
    ) == date(2026, 9, 19)


def test_calendar_days_with_a_calendar_roll_forward_past_holiday_and_weekend() -> None:
    explanation = explain_settlement(date(2026, 8, 25), rule(3, RuleType.CALENDAR_DAYS, AE))
    assert explanation.expected_date == date(2026, 8, 31)
    assert explanation.summary == (
        "25 Aug + T+3 → 31 Aug; 28 Aug skipped: bank holiday (Prophet's Birthday); "
        "29 Aug, 30 Aug skipped: weekend"
    )
    assert [s.label for s in explanation.steps] == ["T+0", "T+3", "skipped", "skipped", "rolled"]


def test_calendar_days_landing_on_a_business_day_are_not_moved() -> None:
    assert expected_settlement_date(
        date(2026, 9, 14), rule(3, RuleType.CALENDAR_DAYS, AE)
    ) == date(2026, 9, 17)


# --- weekend override, coverage and validation -------------------------------------


def test_a_calendar_can_define_its_own_weekend() -> None:
    friday_saturday = cal(weekend=frozenset({5, 6}))
    # Thu + T+1 with a Fri/Sat weekend -> Sunday, regardless of the rule's weekend.
    result = expected_settlement_date(date(2026, 9, 17), rule(1, calendar=friday_saturday))
    assert result == date(2026, 9, 20)


def test_calendar_inherits_the_rule_weekend_when_it_has_none() -> None:
    inherit = cal(date(2026, 9, 21))
    custom = SettlementRule(
        "PSP", 1, RuleType.BUSINESS_DAYS, weekend_days=frozenset({5, 6}), calendar=inherit
    )
    # Thu 17 + T+1: Fri/Sat weekend, Sun 20 is a business day.
    assert expected_settlement_date(date(2026, 9, 17), custom) == date(2026, 9, 20)


def test_missing_year_is_flagged_not_silently_ignored() -> None:
    explanation = explain_settlement(date(2027, 1, 4), rule(2, calendar=AE))
    assert explanation.warnings == (
        "UAE banking calendar has no holiday data for 2027 - only weekends were skipped",
    )
    assert explain_settlement(date(2026, 9, 14), rule(2, calendar=AE)).warnings == ()


def test_year_boundary_checks_both_years() -> None:
    explanation = explain_settlement(date(2026, 12, 31), rule(2, calendar=AE))
    assert explanation.expected_date == date(2027, 1, 4)
    assert len(explanation.warnings) == 1 and "2027" in explanation.warnings[0]


def test_a_calendar_that_never_opens_is_an_error_not_a_hang() -> None:
    start = date(2026, 1, 1)
    closed = cal(*(start + timedelta(days=i) for i in range(60)))
    with pytest.raises(SettlementCalendarError):
        expected_settlement_date(start, rule(1, calendar=closed))


def test_invalid_calendars_are_rejected() -> None:
    with pytest.raises(SettlementCalendarError):
        HolidayCalendar("", "x")
    with pytest.raises(SettlementCalendarError):
        HolidayCalendar("X", "x", weekend_days=frozenset(range(1, 8)))
    with pytest.raises(SettlementCalendarError):
        normalise_code("has space")
    assert normalise_code(" ae ") == "AE"


def test_rule_full_label() -> None:
    assert rule(2, calendar=AE).full_label == "T+2 business days + UAE banking calendar"
    assert rule(2).full_label == "T+2 business days"


# --- reference implementation cross-check -------------------------------------------


def _reference_business_days(start: date, days: int, closed) -> date:
    """Deliberately naive: step one day at a time."""
    cursor = start
    while closed(cursor):
        cursor += timedelta(days=1)
    counted = 0
    while counted < days:
        cursor += timedelta(days=1)
        if not closed(cursor):
            counted += 1
    return cursor


def test_business_day_walk_matches_a_naive_reference() -> None:
    rng = random.Random(7)
    holidays = {date(2026, 1, 1) + timedelta(days=rng.randint(0, 364)) for _ in range(25)}
    calendar = cal(*holidays)

    def closed(day: date) -> bool:
        return day.isoweekday() in (6, 7) or day in holidays

    for _ in range(500):
        start = date(2026, 1, 1) + timedelta(days=rng.randint(0, 330))
        offset = rng.randint(0, 10)
        expected = _reference_business_days(start, offset, closed)
        assert add_business_days(start, offset, calendar=calendar) == expected
        explanation = explain_settlement(start, rule(offset, calendar=calendar))
        assert explanation.expected_date == expected
        counted = [step for step in explanation.steps if step.label.startswith("T+")]
        assert len(counted) == offset + 1
        assert all(not closed(step.day) for step in counted)


# --- bundled config and CSV ------------------------------------------------------------


def test_bundled_calendars_load_and_are_sane() -> None:
    calendars = bundled_calendars()
    assert set(calendars) == {"AE", "TARGET2"}
    assert AE.weekend_days == frozenset({6, 7})
    assert AE.holiday_name(date(2026, 8, 28)) == "Prophet's Birthday"
    assert date(2026, 8, 28).strftime("%A") == "Friday"
    assert AE.covered_years == frozenset({2026})
    assert TARGET2.covered_years == frozenset({2026, 2027})
    # Good Friday / Easter Monday really are Friday / Monday.
    for day, name in TARGET2.holidays.items():
        if name == "Good Friday":
            assert day.isoweekday() == 5
        if name == "Easter Monday":
            assert day.isoweekday() == 1


def test_calendar_config_rejects_duplicates() -> None:
    with pytest.raises(SettlementCalendarError):
        calendar_from_mapping(
            {
                "code": "X",
                "name": "X",
                "holidays": [
                    {"date": date(2026, 1, 1), "name": "a"},
                    {"date": date(2026, 1, 1), "name": "b"},
                ],
            }
        )


def test_parse_holiday_csv_with_header_and_mixed_formats() -> None:
    payload = b"date,name\n2026-09-16,Test Day\n17.09.2026,Other\nnot-a-date,Broken\n"
    holidays, errors = parse_holiday_csv(payload)
    assert holidays == {date(2026, 9, 16): "Test Day", date(2026, 9, 17): "Other"}
    assert len(errors) == 1 and errors[0].startswith("line 4")


def test_parse_holiday_csv_without_header_and_semicolons() -> None:
    holidays, errors = parse_holiday_csv("2026-09-16;Día festivo\n".encode())
    assert holidays == {date(2026, 9, 16): "Día festivo"}
    assert errors == []


def test_parse_holiday_csv_empty_file() -> None:
    with pytest.raises(SettlementCalendarError):
        parse_holiday_csv(b"  \n")


def test_holidays_csv_round_trip() -> None:
    holidays, errors = parse_holiday_csv(holidays_to_csv(AE).encode())
    assert errors == []
    assert holidays == dict(AE.holidays)
