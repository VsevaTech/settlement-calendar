"""CSV / XLSX import, column detection and invalid input handling."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.db_models import Payment, Settlement
from app.services.import_service import (
    ImportError_,
    detect_columns,
    import_file,
    peek_pending,
    read_tabular,
)

PAYMENTS_CSV = b"""payment_id,provider,payment_date,amount,currency
pay_001,PSP_A,2026-09-14,100.00,EUR
pay_002,PSP_A,2026-09-15,75.50,EUR
pay_003,PSP_B,2026-09-15,320.00,ILS
"""

SETTLEMENTS_CSV = b"""settlement_id,payment_id,settlement_date,amount,currency
stl_001,pay_001,2026-09-16,100.00,EUR
stl_002,pay_003,2026-09-16,320.00,ILS
"""


def xlsx_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_csv_payments_import(db_session: Session) -> None:
    result = import_file(db_session, PAYMENTS_CSV, "payments.csv", "payments")
    assert result.imported == 3
    assert result.skipped == 0
    assert result.errors == []

    stored = db_session.scalars(select(Payment).order_by(Payment.payment_id)).all()
    assert [p.payment_id for p in stored] == ["pay_001", "pay_002", "pay_003"]
    assert stored[0].amount == Decimal("100.00")
    assert isinstance(stored[0].amount, Decimal)
    assert stored[0].payment_date == date(2026, 9, 14)
    assert stored[2].currency == "ILS"


def test_xlsx_payments_import(db_session: Session) -> None:
    payload = xlsx_bytes(
        ["payment_id", "provider", "payment_date", "amount", "currency"],
        [
            ["pay_x1", "PSP_A", "2026-09-14", "100.00", "eur"],
            ["pay_x2", "PSP_C", "18.09.2026", "1 250,75", "USD"],
        ],
    )
    result = import_file(db_session, payload, "payments.xlsx", "payments")
    assert result.imported == 2
    stored = db_session.scalars(select(Payment).order_by(Payment.payment_id)).all()
    assert stored[0].currency == "EUR"
    assert stored[1].amount == Decimal("1250.75")
    assert stored[1].payment_date == date(2026, 9, 18)


def test_settlements_import(db_session: Session) -> None:
    import_file(db_session, PAYMENTS_CSV, "payments.csv", "payments")
    result = import_file(db_session, SETTLEMENTS_CSV, "settlements.csv", "settlements")
    assert result.imported == 2
    stored = db_session.scalars(select(Settlement)).all()
    assert {s.payment_id for s in stored} == {"pay_001", "pay_003"}


def test_reimport_updates_instead_of_duplicating(db_session: Session) -> None:
    import_file(db_session, PAYMENTS_CSV, "payments.csv", "payments")
    changed = PAYMENTS_CSV.replace(b"100.00,EUR", b"111.00,EUR")
    result = import_file(db_session, changed, "payments.csv", "payments")
    assert result.imported == 0
    assert result.updated == 3
    assert len(db_session.scalars(select(Payment)).all()) == 3
    payment = db_session.scalar(select(Payment).where(Payment.payment_id == "pay_001"))
    assert payment is not None and payment.amount == Decimal("111.00")


def test_column_auto_detection_for_unusual_headers() -> None:
    frame = read_tabular(
        b"Txn ID,PSP,Transaction Date,Gross Amount,CCY\npay_1,PSP_A,2026-09-14,10.00,EUR\n",
        "weird.csv",
    )
    detected = detect_columns(list(frame.columns), "payments")
    assert detected == {
        "payment_id": "Txn ID",
        "provider": "PSP",
        "payment_date": "Transaction Date",
        "amount": "Gross Amount",
        "currency": "CCY",
    }


def test_unknown_columns_trigger_the_mapping_flow(db_session: Session) -> None:
    payload = b"a,b,c,d,e\npay_1,PSP_A,2026-09-14,10.00,EUR\n"
    result = import_file(db_session, payload, "mystery.csv", "payments")
    assert result.needs_mapping is True
    assert result.mapping_token
    assert set(result.available_columns) == {"a", "b", "c", "d", "e"}
    assert peek_pending(result.mapping_token) is not None
    assert db_session.scalars(select(Payment)).all() == []


def test_invalid_rows_are_skipped_and_reported(db_session: Session) -> None:
    payload = b"""payment_id,provider,payment_date,amount,currency
pay_ok,PSP_A,2026-09-14,100.00,EUR
,PSP_A,2026-09-14,100.00,EUR
pay_bad_date,PSP_A,not-a-date,100.00,EUR
pay_bad_amount,PSP_A,2026-09-14,abc,EUR
pay_no_currency,PSP_A,2026-09-14,100.00,
"""
    result = import_file(db_session, payload, "payments.csv", "payments")
    assert result.imported == 1
    assert result.skipped == 4
    assert len(result.errors) == 4
    assert any("row 3" in error for error in result.errors)


def test_unsupported_and_empty_files_are_rejected(db_session: Session) -> None:
    with pytest.raises(ImportError_):
        import_file(db_session, b"", "payments.csv", "payments")
    with pytest.raises(ImportError_):
        import_file(db_session, b"whatever", "payments.pdf", "payments")
    with pytest.raises(ImportError_):
        import_file(db_session, b"payment_id,provider\n", "payments.csv", "payments")
