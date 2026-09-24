"""SQLite persistence for holiday calendars (the editable copy of the config)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.db_models import HolidayCalendarRecord, HolidayRecord, ProviderRule
from app.services.holiday_calendars import (
    MAX_HOLIDAY_NAME,
    bundled_calendars,
    normalise_code,
)
from app.services.settlement_calendar import (
    HolidayCalendar,
    SettlementCalendarError,
    parse_weekend_days,
)


def _weekend_text(weekend_days: frozenset[int] | None) -> str:
    return "" if weekend_days is None else ",".join(str(d) for d in sorted(weekend_days))


def _record(session: Session, code: str) -> HolidayCalendarRecord | None:
    return session.scalar(select(HolidayCalendarRecord).where(HolidayCalendarRecord.code == code))


def _require(session: Session, code: str) -> HolidayCalendarRecord:
    record = _record(session, code)
    if record is None:
        raise SettlementCalendarError(f"unknown calendar: {code}")
    return record


def seed_bundled_calendars(session: Session) -> list[str]:
    """Insert bundled calendars whose code is not in the database yet."""
    inserted: list[str] = []
    for code, calendar in bundled_calendars().items():
        if _record(session, code) is not None:
            continue
        record = HolidayCalendarRecord(
            code=code,
            name=calendar.name,
            weekend_days=_weekend_text(calendar.weekend_days),
            source=calendar.source,
            bundled=True,
        )
        session.add(record)
        session.flush()
        session.add_all(
            HolidayRecord(calendar_id=record.id, holiday_date=day, name=name)
            for day, name in calendar.holidays.items()
        )
        inserted.append(code)
    session.commit()
    return inserted


def _to_domain(record: HolidayCalendarRecord, holidays: dict[date, str]) -> HolidayCalendar:
    weekend = parse_weekend_days(record.weekend_days) if record.weekend_days.strip() else None
    return HolidayCalendar(
        code=record.code,
        name=record.name,
        holidays=holidays,
        weekend_days=weekend,
        source=record.source,
    )


def load_calendars(session: Session) -> dict[str, HolidayCalendar]:
    """Every stored calendar with its holidays, keyed by code (2 queries)."""
    records = session.scalars(
        select(HolidayCalendarRecord).order_by(HolidayCalendarRecord.code)
    ).all()
    by_id: dict[int, dict[date, str]] = {record.id: {} for record in records}
    for holiday in session.scalars(
        select(HolidayRecord).order_by(HolidayRecord.holiday_date)
    ).all():
        by_id.setdefault(holiday.calendar_id, {})[holiday.holiday_date] = holiday.name
    return {record.code: _to_domain(record, by_id[record.id]) for record in records}


def get_calendar(session: Session, code: str) -> HolidayCalendar | None:
    return load_calendars(session).get(code)


def calendar_usage(session: Session) -> dict[str, list[str]]:
    """calendar code -> providers whose rule uses it."""
    usage: dict[str, list[str]] = {}
    for provider, code in session.execute(
        select(ProviderRule.provider, ProviderRule.calendar_code)
        .where(ProviderRule.calendar_code.is_not(None))
        .order_by(ProviderRule.provider)
    ).all():
        usage.setdefault(code, []).append(provider)
    return usage


def is_bundled(session: Session, code: str) -> bool:
    record = _record(session, code)
    return bool(record and record.bundled)


def create_calendar(
    session: Session, code: str, name: str, weekend_days: str = ""
) -> HolidayCalendarRecord:
    code = normalise_code(code)
    name = (name or "").strip()
    if not name:
        raise SettlementCalendarError("calendar name must not be empty")
    if _record(session, code) is not None:
        raise SettlementCalendarError(f"calendar {code} already exists")
    weekend = _weekend_text(parse_weekend_days(weekend_days)) if weekend_days.strip() else ""
    record = HolidayCalendarRecord(code=code, name=name[:128], weekend_days=weekend, source="")
    session.add(record)
    session.commit()
    return record


def delete_calendar(session: Session, code: str) -> None:
    record = _require(session, code)
    used_by = calendar_usage(session).get(code)
    if used_by:
        raise SettlementCalendarError(
            f"calendar {code} is used by {', '.join(used_by)} - change those rules first"
        )
    session.execute(delete(HolidayRecord).where(HolidayRecord.calendar_id == record.id))
    session.delete(record)
    session.commit()


def upsert_holidays(session: Session, code: str, holidays: dict[date, str]) -> tuple[int, int]:
    """Add or rename holidays. Returns (added, updated)."""
    record = _require(session, code)
    existing = {
        row.holiday_date: row
        for row in session.scalars(
            select(HolidayRecord).where(HolidayRecord.calendar_id == record.id)
        ).all()
    }
    added = updated = 0
    for day, name in holidays.items():
        name = (name or "").strip()[:MAX_HOLIDAY_NAME]
        row = existing.get(day)
        if row is None:
            session.add(HolidayRecord(calendar_id=record.id, holiday_date=day, name=name))
            added += 1
        elif row.name != name:
            row.name = name
            updated += 1
    session.commit()
    return added, updated


def delete_holiday(session: Session, code: str, day: date) -> None:
    record = _require(session, code)
    session.execute(
        delete(HolidayRecord).where(
            HolidayRecord.calendar_id == record.id, HolidayRecord.holiday_date == day
        )
    )
    session.commit()
