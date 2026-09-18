"""Pydantic models used by the API/UI layer."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from app.services.settlement_calendar import RuleType, SettlementStatus


class ProviderRuleInput(BaseModel):
    provider: str
    offset_days: int
    rule_type: RuleType

    @field_validator("provider")
    @classmethod
    def _strip_provider(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("provider must not be empty")
        return cleaned

    @field_validator("offset_days")
    @classmethod
    def _check_offset(cls, value: int) -> int:
        if value < 0 or value > 365:
            raise ValueError("offset_days must be between 0 and 365")
        return value


class PaymentRow(BaseModel):
    """One dashboard row: payment + rule + expectation + actual settlement."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    payment_id: str
    provider: str
    payment_date: date
    amount: Decimal
    currency: str
    rule_label: str
    expected_settlement_date: date | None
    settlement_id: str | None = None
    actual_settlement_date: date | None = None
    settled_amount: Decimal | None = None
    status: SettlementStatus | None = None
    days_overdue: int = 0
    has_rule: bool = True

    @property
    def amount_mismatch(self) -> bool:
        return self.settled_amount is not None and self.settled_amount != self.amount


class ImportResult(BaseModel):
    kind: str
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = []
    needs_mapping: bool = False
    mapping_token: str | None = None
    detected_columns: dict[str, str] = {}
    available_columns: list[str] = []
