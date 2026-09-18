"""FastAPI application: upload, rules, dashboard, calendar and exports.

All business logic lives in :mod:`app.services`; this module only wires HTTP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import __version__
from app.config import get_settings
from app.db import get_db, init_db
from app.models.schemas import ImportResult
from app.services import reconciliation as recon
from app.services.export_service import all_rows_xlsx, overdue_csv
from app.services.import_service import (
    PAYMENT_FIELDS,
    SETTLEMENT_FIELDS,
    ImportError_,
    import_file,
    import_payments,
    import_settlements,
    peek_pending,
    pop_pending,
)
from app.services.settlement_calendar import RuleType, SettlementCalendarError, SettlementStatus

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Create the schema on start - the app works against an empty database."""
    init_db()
    yield


app = FastAPI(title="Settlement Calendar", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def money(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f}"


templates.env.filters["money"] = money
templates.env.globals["app_version"] = __version__


def current_as_of() -> date:
    return get_settings().resolved_as_of_date


def weekend_days() -> frozenset[int]:
    return get_settings().resolved_weekend_days


def _context(request: Request, **extra: object) -> dict[str, object]:
    settings = get_settings()
    base: dict[str, object] = {
        "request": request,
        "as_of": settings.resolved_as_of_date,
        "as_of_pinned": bool(settings.as_of_date.strip()),
        "filters": recon.FILTERS,
    }
    base.update(extra)
    return base


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "as_of": current_as_of().isoformat()}


def _dashboard_context(
    request: Request, db: Session, status: str, provider: str, currency: str
) -> dict[str, object]:
    as_of = current_as_of()
    rows = recon.build_rows(db, as_of, weekend_days())
    visible = recon.filter_rows(rows, status)
    if provider:
        visible = [row for row in visible if row.provider == provider]
    if currency:
        visible = [row for row in visible if row.currency == currency]
    visible = sorted(
        visible,
        key=lambda row: (row.expected_settlement_date or date.max, row.provider, row.payment_id),
    )
    return _context(
        request,
        rows=visible,
        total_rows=len(rows),
        totals=recon.currency_totals(rows, as_of),
        counts=recon.status_counts(rows),
        selected_status=status if status in recon.FILTERS else "all",
        selected_provider=provider,
        selected_currency=currency,
        providers=recon.known_providers(db),
        currencies=sorted({row.currency for row in rows}),
        missing_rules=recon.providers_without_rules(db),
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    status: str = "all",
    provider: str = "",
    currency: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _dashboard_context(request, db, status, provider, currency),
    )


@app.get("/partials/panel", response_class=HTMLResponse)
def dashboard_panel(
    request: Request,
    status: str = "all",
    provider: str = "",
    currency: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """The filter bar + table, swapped in by HTMX. Plain links work without it."""
    return templates.TemplateResponse(
        request,
        "_panel.html",
        _dashboard_context(request, db, status, provider, currency),
    )


@app.get("/calendar", response_class=HTMLResponse)
def calendar(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    as_of = current_as_of()
    rows = recon.build_rows(db, as_of, weekend_days())
    return templates.TemplateResponse(
        request,
        "calendar.html",
        _context(
            request,
            days=recon.calendar_view(rows, as_of),
            missing_rules=recon.providers_without_rules(db),
        ),
    )


@app.get("/rules", response_class=HTMLResponse)
def rules_page(
    request: Request, message: str = "", error: str = "", db: Session = Depends(get_db)
) -> HTMLResponse:
    stored = recon.load_rules(db, weekend_days())
    return templates.TemplateResponse(
        request,
        "rules.html",
        _context(
            request,
            rules=[stored[key] for key in sorted(stored)],
            providers=recon.known_providers(db),
            missing_rules=recon.providers_without_rules(db),
            rule_types=[rule_type.value for rule_type in RuleType],
            message=message,
            error=error,
        ),
    )


@app.post("/rules")
def save_rule(
    provider: str = Form(...),
    offset_days: int = Form(...),
    rule_type: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        parsed_type = RuleType(rule_type)
        if offset_days < 0 or offset_days > 365:
            raise SettlementCalendarError("offset must be between 0 and 365")
        if not provider.strip():
            raise SettlementCalendarError("provider must not be empty")
    except (ValueError, SettlementCalendarError) as exc:
        return RedirectResponse(f"/rules?error={exc}", status_code=303)
    recon.upsert_rule(db, provider, offset_days, parsed_type)
    return RedirectResponse(f"/rules?message=Rule+saved+for+{provider.strip()}", status_code=303)


@app.post("/rules/delete")
def remove_rule(provider: str = Form(...), db: Session = Depends(get_db)) -> RedirectResponse:
    recon.delete_rule(db, provider)
    return RedirectResponse("/rules?message=Rule+removed", status_code=303)


@app.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "upload.html",
        _context(request, result=None, missing_rules=recon.providers_without_rules(db)),
    )


def _render_import(request: Request, db: Session, result: ImportResult) -> HTMLResponse:
    if result.needs_mapping:
        fields = PAYMENT_FIELDS if result.kind == "payments" else SETTLEMENT_FIELDS
        return templates.TemplateResponse(
            request,
            "mapping.html",
            _context(
                request,
                result=result,
                fields=fields,
                missing_rules=recon.providers_without_rules(db),
            ),
        )
    return templates.TemplateResponse(
        request,
        "upload.html",
        _context(request, result=result, missing_rules=recon.providers_without_rules(db)),
    )


@app.post("/upload/{kind}", response_class=HTMLResponse)
async def upload(
    request: Request,
    kind: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if kind not in ("payments", "settlements"):
        raise HTTPException(status_code=404, detail="unknown upload kind")
    payload = await file.read()
    try:
        result = import_file(db, payload, file.filename or "", kind)
    except (ImportError_, SettlementCalendarError) as exc:
        result = ImportResult(kind=kind, errors=[str(exc)])
    return _render_import(request, db, result)


@app.post("/mapping/{token}", response_class=HTMLResponse)
async def apply_mapping(
    request: Request, token: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    pending = peek_pending(token)
    if pending is None:
        raise HTTPException(status_code=404, detail="upload expired - please upload again")
    form = await request.form()
    fields = PAYMENT_FIELDS if pending.kind == "payments" else SETTLEMENT_FIELDS
    mapping = {
        field: str(form.get(field) or "").strip()
        for field in fields
        if str(form.get(field) or "").strip()
    }
    missing = [field for field in fields if field not in mapping]
    if missing:
        result = ImportResult(
            kind=pending.kind,
            needs_mapping=True,
            mapping_token=token,
            detected_columns=mapping,
            available_columns=[str(c) for c in pending.frame.columns],
            errors=[f"still missing: {', '.join(missing)}"],
        )
        return _render_import(request, db, result)

    pop_pending(token)
    if pending.kind == "payments":
        result = import_payments(db, pending.frame, mapping)
    else:
        result = import_settlements(db, pending.frame, mapping)
    return _render_import(request, db, result)


@app.get("/payments/{payment_id}", response_class=HTMLResponse)
def payment_detail(
    request: Request, payment_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    as_of = current_as_of()
    rows = recon.build_rows(db, as_of, weekend_days())
    match = next((row for row in rows if row.payment_id == payment_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="payment not found")
    return templates.TemplateResponse(
        request,
        "payment_detail.html",
        _context(request, row=match, missing_rules=recon.providers_without_rules(db)),
    )


@app.get("/export/overdue.csv")
def export_overdue(db: Session = Depends(get_db)) -> Response:
    rows = recon.build_rows(db, current_as_of(), weekend_days())
    return Response(
        content=overdue_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="overdue.csv"'},
    )


@app.get("/export/all.xlsx")
def export_all(db: Session = Depends(get_db)) -> Response:
    rows = recon.build_rows(db, current_as_of(), weekend_days())
    return Response(
        content=all_rows_xlsx(rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="settlements.xlsx"'},
    )


@app.get("/api/summary")
def api_summary(db: Session = Depends(get_db)) -> dict[str, object]:
    """Machine readable summary - handy for smoke tests and CI."""
    as_of = current_as_of()
    rows = recon.build_rows(db, as_of, weekend_days())
    totals = recon.currency_totals(rows, as_of)
    return {
        "as_of": as_of.isoformat(),
        "payments": len(rows),
        "status_counts": recon.status_counts(rows),
        "currencies": [
            {
                "currency": total.currency,
                "expected_today": str(total.expected_today),
                "received_today": str(total.received_today),
                "due_today": str(total.due_today),
                "overdue": str(total.overdue),
                "upcoming": str(total.upcoming),
                "settled_late": str(total.settled_late),
            }
            for total in totals
        ],
    }


@app.post("/reset")
def reset(include_rules: str = Form(default=""), db: Session = Depends(get_db)) -> RedirectResponse:
    recon.clear_data(db, include_rules=bool(include_rules))
    return RedirectResponse("/upload", status_code=303)


__all__ = ["app", "SettlementStatus"]
