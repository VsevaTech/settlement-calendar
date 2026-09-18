"""Joins payments, rules and actual settlements into dashboard-ready views."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.db_models import Payment, ProviderRule, Settlement
from app.models.schemas import PaymentRow
from app.services.settlement_calendar import (
    DEFAULT_WEEKEND_DAYS,
    RuleType,
    SettlementRule,
    SettlementStatus,
    days_overdue,
    expected_settlement_date,
    resolve_status,
)

ZERO = Decimal("0.00")

FILTERS: dict[str, str] = {
    "all": "All",
    "due_today": "Due Today",
    "overdue": "Overdue",
    "upcoming": "Upcoming",
    "settled": "Settled",
    "settled_late": "Settled Late",
}

FILTER_STATUSES: dict[str, set[SettlementStatus]] = {
    "due_today": {SettlementStatus.DUE_TODAY},
    "overdue": {SettlementStatus.OVERDUE},
    "upcoming": {SettlementStatus.EXPECTED},
    "settled": {SettlementStatus.SETTLED},
    "settled_late": {SettlementStatus.SETTLED_LATE},
}


@dataclass(frozen=True)
class CurrencyTotals:
    """Headline numbers for a single currency - currencies are never mixed."""

    currency: str
    expected_today: Decimal = ZERO
    received_today: Decimal = ZERO
    due_today: Decimal = ZERO
    overdue: Decimal = ZERO
    upcoming: Decimal = ZERO
    settled_late: Decimal = ZERO
    overdue_count: int = 0
    due_today_count: int = 0
    settled_late_count: int = 0


@dataclass(frozen=True)
class CalendarProviderCell:
    provider: str
    currency: str
    expected: Decimal
    received: Decimal
    outstanding: Decimal
    count: int


@dataclass(frozen=True)
class CalendarDay:
    day: date
    is_today: bool
    is_past: bool
    cells: list[CalendarProviderCell]


def load_rules(session: Session, weekend_days: frozenset[int] = DEFAULT_WEEKEND_DAYS
               ) -> dict[str, SettlementRule]:
    """All stored provider rules, keyed by provider."""
    rules: dict[str, SettlementRule] = {}
    for row in session.scalars(select(ProviderRule).order_by(ProviderRule.provider)).all():
        rules[row.provider] = SettlementRule(
            provider=row.provider,
            offset_days=row.offset_days,
            rule_type=RuleType(row.rule_type),
            weekend_days=weekend_days,
        )
    return rules


def upsert_rule(
    session: Session, provider: str, offset_days: int, rule_type: RuleType
) -> ProviderRule:
    """Create or update the rule for one provider (rules survive restarts)."""
    provider = provider.strip()
    record = session.scalar(select(ProviderRule).where(ProviderRule.provider == provider))
    if record is None:
        record = ProviderRule(
            provider=provider, offset_days=offset_days, rule_type=rule_type.value
        )
        session.add(record)
    else:
        record.offset_days = offset_days
        record.rule_type = rule_type.value
    session.commit()
    return record


def delete_rule(session: Session, provider: str) -> None:
    session.execute(delete(ProviderRule).where(ProviderRule.provider == provider))
    session.commit()


def known_providers(session: Session) -> list[str]:
    """Providers seen in payments, plus providers that already have a rule."""
    from_payments = set(session.scalars(select(Payment.provider).distinct()).all())
    from_rules = set(session.scalars(select(ProviderRule.provider).distinct()).all())
    return sorted(from_payments | from_rules)


def providers_without_rules(session: Session) -> list[str]:
    from_payments = set(session.scalars(select(Payment.provider).distinct()).all())
    from_rules = set(session.scalars(select(ProviderRule.provider).distinct()).all())
    return sorted(from_payments - from_rules)


def build_rows(
    session: Session,
    as_of_date: date,
    weekend_days: frozenset[int] = DEFAULT_WEEKEND_DAYS,
) -> list[PaymentRow]:
    """The full reconciled view: payment + rule + expected + actual + status."""
    rules = load_rules(session, weekend_days)
    settlements: dict[str, Settlement] = {}
    for settlement in session.scalars(
        select(Settlement).order_by(Settlement.settlement_date)
    ).all():
        # If a payment is paid out more than once, the earliest payout wins.
        current = settlements.get(settlement.payment_id)
        if current is None or settlement.settlement_date < current.settlement_date:
            settlements[settlement.payment_id] = settlement

    rows: list[PaymentRow] = []
    payments = session.scalars(
        select(Payment).order_by(Payment.payment_date, Payment.payment_id)
    ).all()
    for payment in payments:
        rule = rules.get(payment.provider)
        settlement = settlements.get(payment.payment_id)
        if rule is None:
            rows.append(
                PaymentRow(
                    payment_id=payment.payment_id,
                    provider=payment.provider,
                    payment_date=payment.payment_date,
                    amount=payment.amount,
                    currency=payment.currency,
                    rule_label="no rule",
                    expected_settlement_date=None,
                    settlement_id=settlement.settlement_id if settlement else None,
                    actual_settlement_date=settlement.settlement_date if settlement else None,
                    settled_amount=settlement.amount if settlement else None,
                    status=None,
                    has_rule=False,
                )
            )
            continue

        expected = expected_settlement_date(payment.payment_date, rule)
        actual = settlement.settlement_date if settlement else None
        status = resolve_status(expected, actual, as_of_date)
        rows.append(
            PaymentRow(
                payment_id=payment.payment_id,
                provider=payment.provider,
                payment_date=payment.payment_date,
                amount=payment.amount,
                currency=payment.currency,
                rule_label=rule.label,
                expected_settlement_date=expected,
                settlement_id=settlement.settlement_id if settlement else None,
                actual_settlement_date=actual,
                settled_amount=settlement.amount if settlement else None,
                status=status,
                days_overdue=days_overdue(expected, actual, as_of_date),
                has_rule=True,
            )
        )
    return rows


def filter_rows(rows: list[PaymentRow], selected: str) -> list[PaymentRow]:
    """Apply one of the dashboard filters."""
    if selected == "all" or selected not in FILTER_STATUSES:
        return rows
    wanted = FILTER_STATUSES[selected]
    return [row for row in rows if row.status in wanted]


def currency_totals(rows: list[PaymentRow], as_of_date: date) -> list[CurrencyTotals]:
    """Headline totals per currency. Different currencies are never added up."""
    buckets: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "expected_today": ZERO,
            "received_today": ZERO,
            "due_today": ZERO,
            "overdue": ZERO,
            "upcoming": ZERO,
            "settled_late": ZERO,
            "overdue_count": 0,
            "due_today_count": 0,
            "settled_late_count": 0,
        }
    )
    for row in rows:
        if not row.has_rule or row.status is None or row.expected_settlement_date is None:
            continue
        bucket = buckets[row.currency]
        if row.expected_settlement_date == as_of_date:
            bucket["expected_today"] += row.amount
            if row.actual_settlement_date is not None:
                bucket["received_today"] += row.amount
        if row.status is SettlementStatus.DUE_TODAY:
            bucket["due_today"] += row.amount
            bucket["due_today_count"] += 1
        elif row.status is SettlementStatus.OVERDUE:
            bucket["overdue"] += row.amount
            bucket["overdue_count"] += 1
        elif row.status is SettlementStatus.EXPECTED:
            bucket["upcoming"] += row.amount
        elif row.status is SettlementStatus.SETTLED_LATE:
            bucket["settled_late"] += row.amount
            bucket["settled_late_count"] += 1

    return [
        CurrencyTotals(currency=currency, **values)  # type: ignore[arg-type]
        for currency, values in sorted(buckets.items())
    ]


def status_counts(rows: list[PaymentRow]) -> dict[str, int]:
    counts = {status.value: 0 for status in SettlementStatus}
    counts["NO_RULE"] = 0
    for row in rows:
        if row.status is None:
            counts["NO_RULE"] += 1
        else:
            counts[row.status.value] += 1
    return counts


def calendar_view(
    rows: list[PaymentRow],
    as_of_date: date,
    future_days: int = 21,
    recent_past_days: int = 5,
) -> list[CalendarDay]:
    """Group expected settlements by date and provider for the calendar page.

    Past days are only kept when they still carry outstanding money (or are
    within ``recent_past_days``), so the page opens on what actually matters.
    """
    grouped: dict[date, dict[tuple[str, str], list[PaymentRow]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        if row.expected_settlement_date is None or row.status is None:
            continue
        # Settled money stays on its expected day too, for cash-flow context.
        grouped[row.expected_settlement_date][(row.provider, row.currency)].append(row)

    days: list[CalendarDay] = []
    for day in sorted(grouped):
        cells: list[CalendarProviderCell] = []
        for (provider, currency), items in sorted(grouped[day].items()):
            expected = sum((item.amount for item in items), ZERO)
            received = sum(
                (item.amount for item in items if item.actual_settlement_date is not None), ZERO
            )
            cells.append(
                CalendarProviderCell(
                    provider=provider,
                    currency=currency,
                    expected=expected,
                    received=received,
                    outstanding=expected - received,
                    count=len(items),
                )
            )
        days.append(
            CalendarDay(
                day=day,
                is_today=day == as_of_date,
                is_past=day < as_of_date,
                cells=cells,
            )
        )

    past = [day for day in days if day.day < as_of_date]
    future = [day for day in days if day.day >= as_of_date]
    keep_past = {
        day.day for day in past if any(cell.outstanding > 0 for cell in day.cells)
    } | {day.day for day in past[-recent_past_days:]}
    return [day for day in past if day.day in keep_past] + future[:future_days]


def overdue_rows(rows: list[PaymentRow]) -> list[PaymentRow]:
    """Rows for the overdue report, worst first."""
    overdue = [row for row in rows if row.status is SettlementStatus.OVERDUE]
    return sorted(overdue, key=lambda r: (-r.days_overdue, r.currency, r.payment_id))


def clear_data(session: Session, include_rules: bool = False) -> None:
    session.execute(delete(Settlement))
    session.execute(delete(Payment))
    if include_rules:
        session.execute(delete(ProviderRule))
    session.commit()
