"""Pure settlement-date and status logic.

This module is deliberately free of FastAPI, SQLAlchemy and I/O so that every
calendar rule can be unit tested without starting the web application.

All money values are :class:`decimal.Decimal`. Floats are never used for
financial values.
"""

from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class SettlementRule:
    """A provider level settlement rule, e.g. ``PSP_A -> T+2 business days``."""

    provider: str
    offset_days: int
    rule_type: RuleType
    weekend_days: frozenset[int] = field(default=DEFAULT_WEEKEND_DAYS)

    def __post_init__(self) -> None:
        if not self.provider or not self.provider.strip():
            raise SettlementCalendarError("provider must be a non-empty string")
        if self.offset_days < 0:
            raise SettlementCalendarError("offset_days must be >= 0")
        if self.offset_days > 365:
            raise SettlementCalendarError("offset_days must be <= 365")
        if not isinstance(self.rule_type, RuleType):
            raise SettlementCalendarError(f"unknown rule_type: {self.rule_type!r}")
        if self.rule_type is RuleType.BUSINESS_DAYS:
            if not self.weekend_days:
                return
            if len(self.weekend_days) >= 7:
                raise SettlementCalendarError("at least one weekday must be a business day")
            for day in self.weekend_days:
                if day not in range(1, 8):
                    raise SettlementCalendarError(
                        "weekend_days must contain ISO weekday numbers 1..7"
                    )

    @property
    def label(self) -> str:
        unit = "business day" if self.rule_type is RuleType.BUSINESS_DAYS else "calendar day"
        plural = "" if self.offset_days == 1 else "s"
        return f"T+{self.offset_days} {unit}{plural}"


def is_business_day(day: date, weekend_days: frozenset[int] = DEFAULT_WEEKEND_DAYS) -> bool:
    """True when ``day`` is a business day under the configured weekend."""
    return day.isoweekday() not in weekend_days


def next_business_day(
    day: date, weekend_days: frozenset[int] = DEFAULT_WEEKEND_DAYS
) -> date:
    """The first business day on or after ``day``."""
    cursor = day
    for _ in range(14):
        if is_business_day(cursor, weekend_days):
            return cursor
        cursor += timedelta(days=1)
    raise SettlementCalendarError("no business day found - weekend configuration covers a week")


def add_business_days(
    start: date, days: int, weekend_days: frozenset[int] = DEFAULT_WEEKEND_DAYS
) -> date:
    """Add ``days`` business days to ``start``.

    ``days == 0`` returns the first business day on or after ``start``, so a
    payment taken on Saturday never settles on Saturday.
    """
    if days < 0:
        raise SettlementCalendarError("days must be >= 0")
    cursor = next_business_day(start, weekend_days)
    remaining = days
    while remaining > 0:
        cursor += timedelta(days=1)
        while not is_business_day(cursor, weekend_days):
            cursor += timedelta(days=1)
        remaining -= 1
    return cursor


def add_calendar_days(start: date, days: int) -> date:
    """Add ``days`` calendar days to ``start`` - weekends are counted."""
    if days < 0:
        raise SettlementCalendarError("days must be >= 0")
    return start + timedelta(days=days)


def expected_settlement_date(payment_date: date, rule: SettlementRule) -> date:
    """The date the money is expected to land, given the provider rule."""
    if not isinstance(payment_date, date):
        raise SettlementCalendarError("payment_date must be a date")
    if rule.rule_type is RuleType.BUSINESS_DAYS:
        return add_business_days(payment_date, rule.offset_days, rule.weekend_days)
    return add_calendar_days(payment_date, rule.offset_days)


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
    if len(days) >= 7:
        raise SettlementCalendarError("at least one weekday must be a business day")
    return frozenset(days)
