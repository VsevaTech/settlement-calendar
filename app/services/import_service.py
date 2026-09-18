"""CSV / XLSX import with column auto-detection and manual mapping fallback."""

from __future__ import annotations

import io
import re
import uuid
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.db_models import Payment, Settlement
from app.models.schemas import ImportResult
from app.services.settlement_calendar import (
    SettlementCalendarError,
    parse_date,
    parse_money,
)

MAX_REPORTED_ERRORS = 25

PAYMENT_FIELDS: tuple[str, ...] = (
    "payment_id",
    "provider",
    "payment_date",
    "amount",
    "currency",
)
SETTLEMENT_FIELDS: tuple[str, ...] = (
    "settlement_id",
    "payment_id",
    "settlement_date",
    "amount",
    "currency",
)

COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "payment_id": (
        "payment_id", "paymentid", "payment", "transaction_id", "txn_id", "trx_id",
        "order_id", "reference", "ref", "id",
    ),
    "settlement_id": (
        "settlement_id", "settlementid", "payout_id", "payoutid", "batch_id", "id",
    ),
    "provider": (
        "provider", "psp", "psp_name", "acquirer", "gateway", "processor", "source",
    ),
    "payment_date": (
        "payment_date", "paymentdate", "date", "transaction_date", "created_at",
        "captured_at", "paid_at", "authorised_at", "authorized_at",
    ),
    "settlement_date": (
        "settlement_date", "settlementdate", "payout_date", "paid_out_at", "value_date",
        "received_at", "date",
    ),
    "amount": (
        "amount", "gross_amount", "net_amount", "value", "total", "sum", "gross", "payout_amount",
    ),
    "currency": ("currency", "ccy", "curr", "currency_code", "iso_currency"),
}


class ImportError_(SettlementCalendarError):
    """Raised when a file cannot be read at all."""


@dataclass
class PendingUpload:
    """An uploaded file that still needs a manual column mapping."""

    kind: str
    filename: str
    frame: pd.DataFrame
    detected: dict[str, str] = field(default_factory=dict)


_PENDING: dict[str, PendingUpload] = {}


def stash_pending(upload: PendingUpload) -> str:
    token = uuid.uuid4().hex
    _PENDING[token] = upload
    # Keep the in-memory store small - this is a single-user MVP.
    if len(_PENDING) > 20:
        for stale in list(_PENDING)[:-20]:
            _PENDING.pop(stale, None)
    return token


def pop_pending(token: str) -> PendingUpload | None:
    return _PENDING.pop(token, None)


def peek_pending(token: str) -> PendingUpload | None:
    return _PENDING.get(token)


def normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def read_tabular(payload: bytes, filename: str) -> pd.DataFrame:
    """Read CSV or XLSX bytes into a string-typed DataFrame."""
    if not payload:
        raise ImportError_("uploaded file is empty")
    lowered = (filename or "").lower()
    try:
        if lowered.endswith((".xlsx", ".xlsm", ".xls")):
            frame = pd.read_excel(io.BytesIO(payload), dtype=str, engine="openpyxl")
        elif lowered.endswith((".csv", ".txt", ".tsv")) or not lowered:
            sep = "\t" if lowered.endswith(".tsv") else None
            frame = pd.read_csv(
                io.BytesIO(payload), dtype=str, sep=sep, engine="python", skipinitialspace=True
            )
        else:
            raise ImportError_(f"unsupported file type: {filename!r} (use .csv or .xlsx)")
    except ImportError_:
        raise
    except Exception as exc:  # pragma: no cover - pandas raises many error types
        raise ImportError_(f"cannot read {filename or 'file'}: {exc}") from exc

    if frame.empty or not len(frame.columns):
        raise ImportError_("file contains no rows")
    frame = frame.rename(columns=lambda c: str(c).strip())
    return frame.where(frame.notna(), None)


def detect_columns(columns: list[str], kind: str) -> dict[str, str]:
    """Map internal field -> source column name, best effort."""
    fields = PAYMENT_FIELDS if kind == "payments" else SETTLEMENT_FIELDS
    normalised = {normalise(col): col for col in columns}
    detected: dict[str, str] = {}
    taken: set[str] = set()

    for field_name in fields:
        for candidate in COLUMN_SYNONYMS.get(field_name, ()):
            source = normalised.get(candidate)
            if source is not None and source not in taken:
                detected[field_name] = source
                taken.add(source)
                break
    # Second pass: substring match for anything still missing.
    for field_name in fields:
        if field_name in detected:
            continue
        for norm, source in normalised.items():
            if source in taken:
                continue
            if any(candidate in norm for candidate in COLUMN_SYNONYMS.get(field_name, ())):
                detected[field_name] = source
                taken.add(source)
                break
    return detected


def missing_fields(detected: dict[str, str], kind: str) -> list[str]:
    fields = PAYMENT_FIELDS if kind == "payments" else SETTLEMENT_FIELDS
    return [f for f in fields if f not in detected or not detected[f]]


def _cell(row: pd.Series, mapping: dict[str, str], field_name: str) -> object:
    column = mapping.get(field_name)
    if column is None:
        return None
    value = row.get(column)
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return value


def import_payments(
    session: Session, frame: pd.DataFrame, mapping: dict[str, str]
) -> ImportResult:
    """Insert or update payments. Existing ``payment_id`` values are replaced."""
    result = ImportResult(kind="payments")
    existing = {p.payment_id: p for p in session.scalars(select(Payment)).all()}

    for index, row in frame.iterrows():
        line = int(index) + 2  # header + 1-based
        try:
            payment_id = str(_cell(row, mapping, "payment_id") or "").strip()
            if not payment_id:
                raise SettlementCalendarError("payment_id is empty")
            provider = str(_cell(row, mapping, "provider") or "").strip()
            if not provider:
                raise SettlementCalendarError("provider is empty")
            payment_date = parse_date(_cell(row, mapping, "payment_date"))
            amount = parse_money(_cell(row, mapping, "amount"))
            currency = str(_cell(row, mapping, "currency") or "").strip().upper()
            if not currency:
                raise SettlementCalendarError("currency is empty")
        except SettlementCalendarError as exc:
            result.skipped += 1
            if len(result.errors) < MAX_REPORTED_ERRORS:
                result.errors.append(f"row {line}: {exc}")
            continue

        record = existing.get(payment_id)
        if record is None:
            record = Payment(
                payment_id=payment_id,
                provider=provider,
                payment_date=payment_date,
                amount=amount,
                currency=currency,
            )
            session.add(record)
            existing[payment_id] = record
            result.imported += 1
        else:
            record.provider = provider
            record.payment_date = payment_date
            record.amount = amount
            record.currency = currency
            result.updated += 1

    session.commit()
    return result


def import_settlements(
    session: Session, frame: pd.DataFrame, mapping: dict[str, str]
) -> ImportResult:
    """Insert or update settlements, matched to payments by ``payment_id``."""
    result = ImportResult(kind="settlements")
    existing = {s.settlement_id: s for s in session.scalars(select(Settlement)).all()}

    for index, row in frame.iterrows():
        line = int(index) + 2
        try:
            settlement_id = str(_cell(row, mapping, "settlement_id") or "").strip()
            if not settlement_id:
                raise SettlementCalendarError("settlement_id is empty")
            payment_id = str(_cell(row, mapping, "payment_id") or "").strip()
            if not payment_id:
                raise SettlementCalendarError("payment_id is empty")
            settlement_date = parse_date(_cell(row, mapping, "settlement_date"))
            amount = parse_money(_cell(row, mapping, "amount"))
            currency = str(_cell(row, mapping, "currency") or "").strip().upper()
            if not currency:
                raise SettlementCalendarError("currency is empty")
        except SettlementCalendarError as exc:
            result.skipped += 1
            if len(result.errors) < MAX_REPORTED_ERRORS:
                result.errors.append(f"row {line}: {exc}")
            continue

        record = existing.get(settlement_id)
        if record is None:
            record = Settlement(
                settlement_id=settlement_id,
                payment_id=payment_id,
                settlement_date=settlement_date,
                amount=amount,
                currency=currency,
            )
            session.add(record)
            existing[settlement_id] = record
            result.imported += 1
        else:
            record.payment_id = payment_id
            record.settlement_date = settlement_date
            record.amount = amount
            record.currency = currency
            result.updated += 1

    session.commit()
    return result


def import_file(
    session: Session, payload: bytes, filename: str, kind: str
) -> ImportResult:
    """Read a file, auto-detect columns and import - or ask for a mapping."""
    frame = read_tabular(payload, filename)
    detected = detect_columns(list(frame.columns), kind)
    missing = missing_fields(detected, kind)
    if missing:
        token = stash_pending(
            PendingUpload(kind=kind, filename=filename, frame=frame, detected=detected)
        )
        return ImportResult(
            kind=kind,
            needs_mapping=True,
            mapping_token=token,
            detected_columns=detected,
            available_columns=[str(c) for c in frame.columns],
            errors=[f"could not detect column for: {', '.join(missing)}"],
        )
    if kind == "payments":
        return import_payments(session, frame, detected)
    return import_settlements(session, frame, detected)
