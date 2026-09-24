"""Generate the synthetic demo dataset.

The dataset is fully synthetic and deterministic: the same ``--as-of`` date and
seed always produce the same files and therefore the same dashboard numbers.

Usage::

    python -m app.demo.generate --as-of 2026-09-17 --out demo-data
"""

from __future__ import annotations

import argparse
import csv
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.services.holiday_calendars import bundled_calendars
from app.services.settlement_calendar import (
    RuleType,
    SettlementRule,
    expected_settlement_date,
)

DEFAULT_AS_OF = date(2026, 9, 17)
SEED = 20260917

# The counts the live demo is expected to show.
TARGET_OVERDUE = 12
TARGET_DUE_TODAY = 7
TARGET_SETTLED_LATE = 5

# PSP_A is a UAE acquirer: T+2 business days on the bundled UAE banking
# calendar, so the Prophet's Birthday holiday (Friday 28 Aug 2026) pushes its
# payouts. PSP_B and PSP_C keep "weekends only" to show the contrast.
_CALENDARS = bundled_calendars()

DEMO_RULES: tuple[SettlementRule, ...] = (
    SettlementRule("PSP_A", 2, RuleType.BUSINESS_DAYS, calendar=_CALENDARS["AE"]),
    SettlementRule("PSP_B", 1, RuleType.BUSINESS_DAYS),
    SettlementRule("PSP_C", 3, RuleType.CALENDAR_DAYS),
)

PROVIDER_CURRENCIES: dict[str, tuple[str, ...]] = {
    "PSP_A": ("EUR", "EUR", "EUR", "USD"),
    "PSP_B": ("ILS", "ILS", "USD"),
    "PSP_C": ("USD", "EUR", "ILS"),
}

AMOUNT_RANGES: dict[str, tuple[int, int]] = {
    "EUR": (12000, 480000),
    "USD": (15000, 520000),
    "ILS": (40000, 1900000),
}


@dataclass
class DemoPayment:
    payment_id: str
    provider: str
    payment_date: date
    amount: Decimal
    currency: str
    expected: date
    settlement_date: date | None = None
    settlement_id: str | None = None


def _rule_map() -> dict[str, SettlementRule]:
    return {rule.provider: rule for rule in DEMO_RULES}


def _payment_dates(as_of: date) -> list[date]:
    """Payment dates from ~4 weeks back to ~1 week ahead, weekends included."""
    days: list[date] = []
    for offset in range(-26, 8):
        days.append(as_of + timedelta(days=offset))
    return days


def build_payments(as_of: date = DEFAULT_AS_OF, seed: int = SEED) -> list[DemoPayment]:
    """Build the synthetic payment population with deterministic status counts."""
    rng = random.Random(seed)
    rules = _rule_map()
    payments: list[DemoPayment] = []
    counter = 0

    for day in _payment_dates(as_of):
        weekday = day.isoweekday()
        # Fewer payments at the weekend, but never zero - weekend cases matter.
        if weekday in (6, 7):
            volume = rng.randint(2, 4)
        elif weekday == 5:  # Friday: the interesting business-day case
            volume = rng.randint(8, 11)
        else:
            volume = rng.randint(6, 9)

        for _ in range(volume):
            counter += 1
            provider = rng.choice(["PSP_A", "PSP_A", "PSP_B", "PSP_C"])
            currency = rng.choice(PROVIDER_CURRENCIES[provider])
            low, high = AMOUNT_RANGES[currency]
            cents = rng.randint(low, high)
            amount = (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))
            payments.append(
                DemoPayment(
                    payment_id=f"pay_{counter:04d}",
                    provider=provider,
                    payment_date=day,
                    amount=amount,
                    currency=currency,
                    expected=expected_settlement_date(day, rules[provider]),
                )
            )

    # A guaranteed cohort whose expected settlement date is exactly ``as_of``.
    # Without it the "due today" bucket would depend on the random draw.
    for provider in ("PSP_A", "PSP_B", "PSP_C"):
        rule = rules[provider]
        payment_day = _date_expecting(as_of, rule)
        for _ in range(6):
            counter += 1
            currency = rng.choice(PROVIDER_CURRENCIES[provider])
            low, high = AMOUNT_RANGES[currency]
            cents = rng.randint(low, high)
            payments.append(
                DemoPayment(
                    payment_id=f"pay_{counter:04d}",
                    provider=provider,
                    payment_date=payment_day,
                    amount=(Decimal(cents) / Decimal(100)).quantize(Decimal("0.01")),
                    currency=currency,
                    expected=expected_settlement_date(payment_day, rule),
                )
            )
    return payments


def _date_expecting(target: date, rule: SettlementRule) -> date:
    """The latest payment date whose expected settlement falls on ``target``."""
    for back in range(0, 15):
        candidate = target - timedelta(days=back)
        if expected_settlement_date(candidate, rule) == target:
            return candidate
    raise RuntimeError(f"no payment date settles on {target} under {rule.label}")


def assign_settlements(
    payments: list[DemoPayment], as_of: date = DEFAULT_AS_OF, seed: int = SEED
) -> list[DemoPayment]:
    """Attach actual settlements so the status counts are exactly on target."""
    rng = random.Random(seed + 1)

    past = sorted([p for p in payments if p.expected < as_of], key=lambda p: p.payment_id)
    today = sorted([p for p in payments if p.expected == as_of], key=lambda p: p.payment_id)
    future = [p for p in payments if p.expected > as_of]

    if len(past) < TARGET_OVERDUE + TARGET_SETTLED_LATE + 20:
        raise RuntimeError("not enough historical payments to build the demo dataset")
    if len(today) < TARGET_DUE_TODAY:
        raise RuntimeError("not enough payments expected today to build the demo dataset")

    # Spread the overdue items across providers and currencies so the report
    # looks like a real problem list, not one broken provider.
    by_bucket: dict[tuple[str, str], list[DemoPayment]] = {}
    for payment in past:
        by_bucket.setdefault((payment.provider, payment.currency), []).append(payment)

    overdue: list[str] = []
    buckets = sorted(by_bucket)
    index = 0
    while len(overdue) < TARGET_OVERDUE:
        bucket = buckets[index % len(buckets)]
        candidates = [p.payment_id for p in by_bucket[bucket] if p.payment_id not in overdue]
        if candidates:
            overdue.append(rng.choice(candidates))
        index += 1
        if index > 500:  # pragma: no cover - defensive
            raise RuntimeError("cannot pick overdue payments")
    overdue_ids = set(overdue)

    late_candidates = [
        p.payment_id
        for p in past
        if p.payment_id not in overdue_ids and (as_of - p.expected).days >= 2
    ]
    late_ids = set(rng.sample(late_candidates, TARGET_SETTLED_LATE))

    due_today_ids = {p.payment_id for p in today[:TARGET_DUE_TODAY]}
    future_ids = {p.payment_id for p in future}
    unsettled = overdue_ids | due_today_ids | future_ids

    sequence = 0
    for payment in sorted(payments, key=lambda p: p.payment_id):
        if payment.payment_id in unsettled:
            continue
        sequence += 1
        if payment.payment_id in late_ids:
            max_delay = min((as_of - payment.expected).days, 6)
            payment.settlement_date = payment.expected + timedelta(days=rng.randint(1, max_delay))
        else:
            payment.settlement_date = payment.expected
        payment.settlement_id = f"stl_{sequence:04d}"
    return payments


def status_summary(payments: list[DemoPayment], as_of: date) -> dict[str, int]:
    from app.services.settlement_calendar import resolve_status

    counts: dict[str, int] = {}
    for payment in payments:
        status = resolve_status(payment.expected, payment.settlement_date, as_of).value
        counts[status] = counts.get(status, 0) + 1
    return counts


def write_dataset(out_dir: Path, payments: list[DemoPayment]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    payments_path = out_dir / "payments.csv"
    settlements_path = out_dir / "settlements.csv"

    with payments_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["payment_id", "provider", "payment_date", "amount", "currency"])
        for payment in payments:
            writer.writerow(
                [
                    payment.payment_id,
                    payment.provider,
                    payment.payment_date.isoformat(),
                    f"{payment.amount:.2f}",
                    payment.currency,
                ]
            )

    with settlements_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            ["settlement_id", "payment_id", "settlement_date", "amount", "currency"]
        )
        for payment in payments:
            if payment.settlement_date is None:
                continue
            writer.writerow(
                [
                    payment.settlement_id,
                    payment.payment_id,
                    payment.settlement_date.isoformat(),
                    f"{payment.amount:.2f}",
                    payment.currency,
                ]
            )
    return payments_path, settlements_path


def generate(as_of: date = DEFAULT_AS_OF, seed: int = SEED) -> list[DemoPayment]:
    payments = build_payments(as_of, seed)
    return assign_settlements(payments, as_of, seed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic demo dataset")
    parser.add_argument("--as-of", default=DEFAULT_AS_OF.isoformat())
    parser.add_argument("--out", default="demo-data")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    as_of = date.fromisoformat(args.as_of)
    payments = generate(as_of, args.seed)
    counts = status_summary(payments, as_of)

    assert counts.get("OVERDUE") == TARGET_OVERDUE, counts
    assert counts.get("DUE_TODAY") == TARGET_DUE_TODAY, counts
    assert counts.get("SETTLED_LATE") == TARGET_SETTLED_LATE, counts

    payments_path, settlements_path = write_dataset(Path(args.out), payments)
    settled = sum(1 for p in payments if p.settlement_date is not None)
    print(f"as of        : {as_of}")
    print(f"payments     : {len(payments)} -> {payments_path}")
    print(f"settlements  : {settled} -> {settlements_path}")
    for status, count in sorted(counts.items()):
        print(f"  {status:<13}: {count}")


if __name__ == "__main__":
    main()
