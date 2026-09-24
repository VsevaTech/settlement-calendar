"""Pure settlement-date and status logic.

This module is deliberately free of FastAPI, SQLAlchemy and I/O so that every
calendar rule can be unit tested without starting the web application.

All money values are :class:`decimal.Decimal`. Floats are never used for
financial values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum

# ISO weekday numbers: Monday == 1 ... Sunday == 7.
DEFAULT_WEEKEND_DAYS: frozenset[int] = frozenset({6, 7})

MONEY_EXPONENT = Decimal("0.01")


class RuleType(StrEnum):
    """How the T+N offset is counted."""

    BUSINESS_DAYS = "BUSINESS_DAYS"
    CALENDAR_DAYS = "CALENDAR_DAYS"


class SettlementStatus(StrEnum):
    """Lifecycle of the money owed for a single payment."""

    EXPECTED = "EXPECTED"
    DUE_TODAY = "DUE_TODAY"
    OVERDUE = "OVERDUE"
    SETTLED = "SETTLED"
    SETTLED_LATE = "SETTLED_LATE"


UNSETTLED_STATUSES = frozenset(
    {SettlementStatus.EXPECTED, SettlementStatus.DUE_TODAY, SettlementStatus.OVERDUE}
)


class SettlementCalendarError(ValueError):
    """Raised for invalid rules or unparsable input values."""


def _validate_weekend(weekend_days: frozenset[int]) -> None:
    if len(weekend_days) >= 7:
        raise SettlementCalendarError("at least one weekday must be a business day")
    for day in weekend_days:
        if day not in range(1, 8):
            raise SettlementCalendarError("weekend_days must contain ISO weekday numbers 1..7")


@dataclass(frozen=True, slots=True)
class HolidayCalendar:
    """A banking calendar: non-business dates plus, optionally, its own weekend.

    ``weekend_days=None`` means "inherit the weekend of the rule" (the global
    ``SC_WEEKEND_DAYS``). A calendar that sets it - e.g. the UAE banking week -
    stays correct even when the global weekend is configured differently.
    """

    code: str
    name: str
    holidays: Mapping[date, str] = field(default_factory=dict, hash=False)
    weekend_days: frozenset[int] | None = None
    source: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if not self.code or not self.code.strip():
            raise SettlementCalendarError("calendar code must be a non-empty string")
        if not self.name or not self.name.strip():
            raise SettlementCalendarError("calendar name must be a non-empty string")
        if self.weekend_days is not None:
            _validate_weekend(self.weekend_days)

    def holiday_name(self, day: date) -> str | None:
        return self.holidays.get(day)

    @property
    def covered_years(self) -> frozenset[int]:
        """Years for which the calendar actually carries holiday data."""
        return frozenset(day.year for day in self.holidays)


@dataclass(frozen=True, slots=True)
class SettlementRule:
    """A provider level settlement rule, e.g. ``PSP_A -> T+2 business days``."""

    provider: str
    offset_days: int
    rule_type: RuleType
    weekend_days: frozenset[int] = field(default=DEFAULT_WEEKEND_DAYS)
    calendar: HolidayCalendar | None = None

    def __post_init__(self) -> None:
        if not self.provider or not self.provider.strip():
            raise SettlementCalendarError("provider must be a non-empty string")
        if self.offset_days < 0:
            raise SettlementCalendarError("offset_days must be >= 0")
        if self.offset_days > 365:
            raise SettlementCalendarError("offset_days must be <= 365")
        if not isinstance(self.rule_type, RuleType):
            raise SettlementCalendarError(f"unknown rule_type: {self.rule_type!r}")
        if self.rule_type is RuleType.BUSINESS_DAYS and self.weekend_days:
            _validate_weekend(self.weekend_days)

    @property
    def effective_weekend_days(self) -> frozenset[int]:
        """The calendar's own weekend if it defines one, otherwise the rule's."""
        if self.calendar is not None and self.calendar.weekend_days is not None:
            return self.calendar.weekend_days
        return self.weekend_days

    @property
    def label(self) -> str:
        unit = "business day" if self.rule_type is RuleType.BUSINESS_DAYS else "calendar day"
        plural = "" if self.offset_days == 1 else "s"
        return f"T+{self.offset_days} {unit}{plural}"

    @property
    def full_label(self) -> str:
        """``T+2 business days + UAE banking calendar``."""
        if self.calendar is None:
            return self.label
        return f"{self.label} + {self.calendar.name}"


def non_business_reason(
    day: date,
    weekend_days: frozenset[int] = DEFAULT_WEEKEND_DAYS,
    calendar: HolidayCalendar | None = None,
) -> str | None:
    """Why ``day`` is not a business day, or ``None`` when it is one.

    A bank holiday that falls on a weekend is reported as the holiday, because
    that is what a person reading the explanation expects to see.
    """
    if calendar is not None:
        holiday = calendar.holiday_name(day)
        if holiday is not None:
            return f"bank holiday ({holiday})" if holiday else "bank holiday"
    if day.isoweekday() in weekend_days:
        return "weekend"
    return None


def is_business_day(
    day: date,
    weekend_days: frozenset[int] = DEFAULT_WEEKEND_DAYS,
    calendar: HolidayCalendar | None = None,
) -> bool:
    """True when ``day`` is a business day under the weekend and holiday calendar."""
    return non_business_reason(day, weekend_days, calendar) is None


# Longest run of consecutive non-business days we accept before assuming the
# configuration is broken (a long Eid break plus a weekend is ~9 days).
MAX_NON_BUSINESS_RUN = 31


@dataclass(frozen=True, slots=True)
class SkippedDay:
    day: date
    reason: str


def _skip_non_business(
    cursor: date,
    weekend_days: frozenset[int],
    calendar: HolidayCalendar | None,
    skipped: list[SkippedDay],
) -> date:
    """Advance ``cursor`` to the first business day on or after it."""
    for _ in range(MAX_NON_BUSINESS_RUN + 1):
        reason = non_business_reason(cursor, weekend_days, calendar)
        if reason is None:
            return cursor
        skipped.append(SkippedDay(cursor, reason))
        cursor += timedelta(days=1)
    raise SettlementCalendarError(
        f"no business day within {MAX_NON_BUSINESS_RUN} days of {cursor.isoformat()} - "
        "check the weekend and holiday configuration"
    )


def next_business_day(
    day: date,
    weekend_days: frozenset[int] = DEFAULT_WEEKEND_DAYS,
    calendar: HolidayCalendar | None = None,
) -> date:
    """The first business day on or after ``day``."""
    return _skip_non_business(day, weekend_days, calendar, [])


def _walk_business_days(
    start: date,
    days: int,
    weekend_days: frozenset[int],
    calendar: HolidayCalendar | None,
) -> tuple[date, list[SkippedDay], list[date]]:
    """Core T+N walk. Returns (result, skipped days, counted days T+0..T+N)."""
    if days < 0:
        raise SettlementCalendarError("days must be >= 0")
    skipped: list[SkippedDay] = []
    cursor = _skip_non_business(start, weekend_days, calendar, skipped)
    counted = [cursor]
    for _ in range(days):
        cursor = _skip_non_business(cursor + timedelta(days=1), weekend_days, calendar, skipped)
        counted.append(cursor)
    return cursor, skipped, counted


def add_business_days(
    start: date,
    days: int,
    weekend_days: frozenset[int] = DEFAULT_WEEKEND_DAYS,
    calendar: HolidayCalendar | None = None,
) -> date:
    """Add ``days`` business days to ``start``.

    ``days == 0`` returns the first business day on or after ``start``, so a
    payment taken on Saturday (or on a bank holiday) never settles that day.
    Weekends and - when a calendar is given - its bank holidays are skipped.
    """
    return _walk_business_days(start, days, weekend_days, calendar)[0]


def add_calendar_days(start: date, days: int) -> date:
    """Add ``days`` calendar days to ``start`` - weekends are counted."""
    if days < 0:
        raise SettlementCalendarError("days must be >= 0")
    return start + timedelta(days=days)


@dataclass(frozen=True, slots=True)
class ExplanationStep:
    """One line of the day-by-day calculation shown on the payment page."""

    day: date
    label: str  # "T+0", "T+1", ..., "skipped", "rolled"
    note: str = ""


@dataclass(frozen=True, slots=True)
class SettlementExplanation:
    """Why the expected settlement date is what it is."""

    payment_date: date
    expected_date: date
    rule_label: str
    calendar_name: str | None
    skipped: tuple[SkippedDay, ...]
    steps: tuple[ExplanationStep, ...]
    warnings: tuple[str, ...] = ()

    @property
    def holiday_adjusted(self) -> bool:
        """True when a bank holiday (not just a weekend) moved the date."""
        return any(item.reason.startswith("bank holiday") for item in self.skipped)

    @property
    def summary(self) -> str:
        """``14 Sep + T+2 → 17 Sep; 16 Sep skipped: bank holiday (…)``."""
        offset = self.rule_label.split(" ", 1)[0]  # "T+2"
        head = f"{_short(self.payment_date)} + {offset} → {_short(self.expected_date)}"
        parts = [head]
        for days, reason in _group_skipped(self.skipped):
            joined = ", ".join(_short(day) for day in days)
            parts.append(f"{joined} skipped: {reason}")
        return "; ".join(parts)


def _short(day: date) -> str:
    return f"{day.day} {day:%b}"


def _group_skipped(skipped: tuple[SkippedDay, ...]) -> list[tuple[list[date], str]]:
    """Group consecutive skipped days that share a reason."""
    groups: list[tuple[list[date], str]] = []
    for item in skipped:
        if (
            groups
            and groups[-1][1] == item.reason
            and (item.day - groups[-1][0][-1]).days == 1
        ):
            groups[-1][0].append(item.day)
        else:
            groups.append(([item.day], item.reason))
    return groups


def _coverage_warnings(
    calendar: HolidayCalendar | None, first: date, last: date
) -> tuple[str, ...]:
    if calendar is None:
        return ()
    covered = calendar.covered_years
    missing = [year for year in range(first.year, last.year + 1) if year not in covered]
    return tuple(
        f"{calendar.name} has no holiday data for {year} - only weekends were skipped"
        for year in missing
    )


def explain_settlement(payment_date: date, rule: SettlementRule) -> SettlementExplanation:
    """Compute the expected date *and* the reasoning behind it.

    This is the single source of truth: :func:`expected_settlement_date` is
    just ``explain_settlement(...).expected_date``.

    * ``BUSINESS_DAYS`` - counting starts on the first business day on or after
      the payment date (T+0) and each of the N steps skips weekends and, when a
      calendar is attached, its bank holidays.
    * ``CALENDAR_DAYS`` - every day counts. Without a calendar the result may be
      a weekend (unchanged MVP behaviour). With a calendar attached the money
      moves through a bank, so a landing day that is a weekend or bank holiday
      rolls forward to the next business day of that calendar.
    """
    if not isinstance(payment_date, date):
        raise SettlementCalendarError("payment_date must be a date")
    weekend = rule.effective_weekend_days
    calendar = rule.calendar
    calendar_name = calendar.name if calendar is not None else None
    steps: list[ExplanationStep] = []

    if rule.rule_type is RuleType.BUSINESS_DAYS:
        expected, skipped, counted = _walk_business_days(
            payment_date, rule.offset_days, weekend, calendar
        )
        labels = {day: f"T+{index}" for index, day in enumerate(counted)}
        reasons = {item.day: item.reason for item in skipped}
        cursor = payment_date
        while cursor <= expected:
            if cursor in reasons:
                steps.append(ExplanationStep(cursor, "skipped", reasons[cursor]))
            elif cursor in labels:
                note = "payment date" if cursor == payment_date else ""
                if labels[cursor] == "T+0" and cursor != payment_date:
                    note = "counting starts on the next business day"
                if cursor == expected:
                    note = (note + "; " if note else "") + "expected settlement"
                steps.append(ExplanationStep(cursor, labels[cursor], note))
            cursor += timedelta(days=1)
    else:
        raw = add_calendar_days(payment_date, rule.offset_days)
        skipped = []
        expected = raw
        if calendar is not None:
            expected = _skip_non_business(raw, weekend, calendar, skipped)
        steps.append(ExplanationStep(payment_date, "T+0", "payment date"))
        if expected == raw:
            if rule.offset_days:
                steps.append(ExplanationStep(raw, f"T+{rule.offset_days}", "expected settlement"))
            else:
                steps[0] = ExplanationStep(raw, "T+0", "payment date; expected settlement")
        else:
            landing = f"landing day - {skipped[0].reason}, rolled forward"
            if rule.offset_days:
                steps.append(ExplanationStep(raw, f"T+{rule.offset_days}", landing))
            else:
                steps[0] = ExplanationStep(raw, "T+0", f"payment date; {landing}")
            steps.extend(ExplanationStep(i.day, "skipped", i.reason) for i in skipped[1:])
            steps.append(
                ExplanationStep(expected, "rolled", "next business day; expected settlement")
            )

    return SettlementExplanation(
        payment_date=payment_date,
        expected_date=expected,
        rule_label=rule.label,
        calendar_name=calendar_name,
        skipped=tuple(skipped),
        steps=tuple(steps),
        warnings=_coverage_warnings(calendar, payment_date, expected),
    )


def expected_settlement_date(payment_date: date, rule: SettlementRule) -> date:
    """The date the money is expected to land, given the provider rule."""
    return explain_settlement(payment_date, rule).expected_date


def resolve_status(
    expected_date: date,
    actual_date: date | None,
    as_of_date: date,
) -> SettlementStatus:
    """Classify one payment.

    ``as_of_date`` is always explicit so the calculation stays deterministic in
    tests and reproducible in the bundled demo.
    """
    if actual_date is not None:
        return (
            SettlementStatus.SETTLED
            if actual_date <= expected_date
            else SettlementStatus.SETTLED_LATE
        )
    if expected_date > as_of_date:
        return SettlementStatus.EXPECTED
    if expected_date == as_of_date:
        return SettlementStatus.DUE_TODAY
    return SettlementStatus.OVERDUE


def days_overdue(
    expected_date: date, actual_date: date | None, as_of_date: date
) -> int:
    """Days the money is late.

    For unsettled payments this is measured against ``as_of_date``; for late
    settlements it is the gap between expected and actual. Never negative.
    """
    reference = actual_date if actual_date is not None else as_of_date
    return max((reference - expected_date).days, 0)


def parse_money(value: object) -> Decimal:
    """Parse a monetary value into a 2-decimal :class:`Decimal`.

    Accepts ``"1 234,56"``, ``"1,234.56"``, ``"€99.00"`` and plain numbers.
    Floats are rejected outright - they must never reach financial code.
    """
    if isinstance(value, Decimal):
        return value.quantize(MONEY_EXPONENT)
    if isinstance(value, bool | float):
        raise SettlementCalendarError(f"refusing to parse float money value: {value!r}")
    if isinstance(value, int):
        return Decimal(value).quantize(MONEY_EXPONENT)
    if value is None:
        raise SettlementCalendarError("amount is empty")

    text = str(value).strip()
    if not text:
        raise SettlementCalendarError("amount is empty")
    text = "".join(ch for ch in text if ch.isdigit() or ch in "-+.,")
    if text.count(",") and text.count("."):
        # The right-most separator is the decimal separator.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif text.count(",") == 1 and len(text.split(",")[1]) in (1, 2):
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        return Decimal(text).quantize(MONEY_EXPONENT)
    except (InvalidOperation, ArithmeticError) as exc:  # pragma: no cover - defensive
        raise SettlementCalendarError(f"cannot parse amount: {value!r}") from exc


DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%Y-%m-%d %H:%M:%S",
    "%d.%m.%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


def parse_date(value: object) -> date:
    """Parse a date from the supported explicit formats (no fuzzy guessing)."""
    from datetime import datetime

    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if value is None:
        raise SettlementCalendarError("date is empty")

    text = str(value).strip()
    if not text:
        raise SettlementCalendarError("date is empty")
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise SettlementCalendarError(f"cannot parse date: {value!r}")


def parse_weekend_days(raw: str | None) -> frozenset[int]:
    """Parse ``"6,7"`` into ``frozenset({6, 7})`` (ISO weekday numbers)."""
    if raw is None or not str(raw).strip():
        return DEFAULT_WEEKEND_DAYS
    days: set[int] = set()
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            day = int(chunk)
        except ValueError as exc:
            raise SettlementCalendarError(f"invalid weekend day: {chunk!r}") from exc
        if day not in range(1, 8):
            raise SettlementCalendarError("weekend days must be ISO weekday numbers 1..7")
        days.add(day)
    result = frozenset(days)
    _validate_weekend(result)
    return result
