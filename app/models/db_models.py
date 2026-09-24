"""SQLAlchemy 2.0 models.

Money is stored as text and converted to :class:`decimal.Decimal` on the way in
and out, because SQLite has no exact numeric type and float must never be used
for financial values.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.services.settlement_calendar import MONEY_EXPONENT, RuleType


class Base(DeclarativeBase):
    pass


class DecimalText(TypeDecorator):
    """Exact decimal storage on top of TEXT."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None:
            return None
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
        return str(value.quantize(MONEY_EXPONENT))

    def process_result_value(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None:
            return None
        return Decimal(value).quantize(MONEY_EXPONENT)


class ProviderRule(Base):
    """Settlement rule for one provider, e.g. ``PSP_A -> T+2 business days``."""

    __tablename__ = "provider_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    offset_days: Mapped[int] = mapped_column(Integer, default=0)
    rule_type: Mapped[str] = mapped_column(String(16), default=RuleType.BUSINESS_DAYS.value)
    # Optional banking calendar (HolidayCalendarRecord.code). NULL = weekends only.
    calendar_code: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class HolidayCalendarRecord(Base):
    """A banking calendar that provider rules can reference by ``code``."""

    __tablename__ = "holiday_calendars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    # "6,7" or "" - empty means "inherit SC_WEEKEND_DAYS".
    weekend_days: Mapped[str] = mapped_column(String(32), default="")
    source: Mapped[str] = mapped_column(Text, default="")
    bundled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class HolidayRecord(Base):
    """One non-business date in a calendar."""

    __tablename__ = "holidays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    calendar_id: Mapped[int] = mapped_column(
        ForeignKey("holiday_calendars.id", ondelete="CASCADE"), index=True
    )
    holiday_date: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String(128), default="")

    __table_args__ = (UniqueConstraint("calendar_id", "holiday_date", name="uq_calendar_day"),)


class Payment(Base):
    """A successful payment that the PSP still owes us."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    payment_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(DecimalText)
    currency: Mapped[str] = mapped_column(String(8), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Settlement(Base):
    """An actual payout received from the PSP."""

    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    settlement_id: Mapped[str] = mapped_column(String(128), index=True)
    payment_id: Mapped[str] = mapped_column(String(128), index=True)
    settlement_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(DecimalText)
    currency: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (UniqueConstraint("settlement_id", name="uq_settlement_id"),)
