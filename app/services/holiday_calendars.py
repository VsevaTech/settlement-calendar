"""Holiday calendar configuration: bundled TOML files and holiday CSV uploads.

Pure module - no FastAPI, SQLAlchemy or pandas - so the demo generator and the
unit tests can load exactly the calendars the web app seeds into SQLite.

Bundled calendar file (``app/calendars/*.toml``)::

    code = "AE"
    name = "UAE banking calendar"
    weekend_days = [6, 7]          # optional; omitted = inherit SC_WEEKEND_DAYS
    source = "where the dates come from"

    [[holidays]]
    date = 2026-08-28
    name = "Prophet's Birthday"

Holiday CSV upload (one calendar per upload)::

    date,name
    2026-08-28,Prophet's Birthday
    28.08.2026,Some other day
"""

from __future__ import annotations

import csv
import io
import re
import tomllib
from datetime import date
from pathlib import Path

from app.services.settlement_calendar import (
    HolidayCalendar,
    SettlementCalendarError,
    parse_date,
)

BUNDLED_DIR = Path(__file__).resolve().parent.parent / "calendars"
CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_\-]{0,31}$")
MAX_HOLIDAY_NAME = 128


def normalise_code(raw: str) -> str:
    """``" ae "`` -> ``"AE"``; codes are short upper-case identifiers."""
    code = (raw or "").strip().upper()
    if not CODE_PATTERN.match(code):
        raise SettlementCalendarError(
            "calendar code must be 1-32 characters: A-Z, 0-9, '_' or '-'"
        )
    return code


def _weekend(raw: object) -> frozenset[int] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(d, int) for d in raw):
        raise SettlementCalendarError("weekend_days must be a list of ISO weekday numbers")
    return frozenset(raw)


def calendar_from_mapping(data: dict[str, object]) -> HolidayCalendar:
    """Build a :class:`HolidayCalendar` from parsed TOML."""
    holidays: dict[date, str] = {}
    for entry in data.get("holidays", []) or []:  # type: ignore[union-attr]
        if not isinstance(entry, dict) or "date" not in entry:
            raise SettlementCalendarError("every [[holidays]] entry needs a date")
        day = parse_date(entry["date"])
        if day in holidays:
            raise SettlementCalendarError(f"duplicate holiday date {day.isoformat()}")
        holidays[day] = str(entry.get("name", "")).strip()[:MAX_HOLIDAY_NAME]
    return HolidayCalendar(
        code=normalise_code(str(data.get("code", ""))),
        name=str(data.get("name", "")).strip(),
        holidays=dict(sorted(holidays.items())),
        weekend_days=_weekend(data.get("weekend_days")),
        source=str(data.get("source", "")).strip(),
    )


def load_calendar_file(path: Path) -> HolidayCalendar:
    with path.open("rb") as handle:
        return calendar_from_mapping(tomllib.load(handle))


def bundled_calendars(directory: Path = BUNDLED_DIR) -> dict[str, HolidayCalendar]:
    """Every calendar shipped with the application, keyed by code."""
    calendars: dict[str, HolidayCalendar] = {}
    for path in sorted(directory.glob("*.toml")):
        calendar = load_calendar_file(path)
        if calendar.code in calendars:
            raise SettlementCalendarError(f"calendar {calendar.code} is defined twice")
        calendars[calendar.code] = calendar
    return calendars


DATE_HEADERS = ("date", "holiday_date", "day")
NAME_HEADERS = ("name", "holiday", "description", "holiday_name")


def parse_holiday_csv(payload: bytes) -> tuple[dict[date, str], list[str]]:
    """Parse a ``date,name`` CSV. Returns (holidays, per-row errors).

    Invalid rows are reported and skipped rather than failing the whole file,
    the same way payment imports behave.
    """
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SettlementCalendarError("holiday file must be UTF-8 CSV") from exc
    if not text.strip():
        raise SettlementCalendarError("holiday file is empty")
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise SettlementCalendarError("holiday file is empty")

    header = [cell.strip().lower() for cell in rows[0]]
    date_col = next((header.index(h) for h in DATE_HEADERS if h in header), None)
    name_col = next((header.index(h) for h in NAME_HEADERS if h in header), None)
    if date_col is None:
        # No header: assume "date,name".
        date_col, name_col, body, first_line = 0, 1, rows, 1
    else:
        body, first_line = rows[1:], 2

    holidays: dict[date, str] = {}
    errors: list[str] = []
    for offset, row in enumerate(body):
        line = first_line + offset
        try:
            day = parse_date(row[date_col] if date_col < len(row) else "")
        except SettlementCalendarError as exc:
            errors.append(f"line {line}: {exc}")
            continue
        name = ""
        if name_col is not None and name_col < len(row):
            name = row[name_col].strip()[:MAX_HOLIDAY_NAME]
        holidays[day] = name
    return dict(sorted(holidays.items())), errors


def holidays_to_csv(calendar: HolidayCalendar) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["date", "name"])
    for day, name in sorted(calendar.holidays.items()):
        writer.writerow([day.isoformat(), name])
    return buffer.getvalue()
