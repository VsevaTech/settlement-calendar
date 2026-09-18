"""HTTP layer: upload, rules, dashboard, calendar, exports."""

from __future__ import annotations

import csv
import io

from fastapi.testclient import TestClient

from tests.conftest import DEMO_DIR

PAYMENTS_CSV = (
    b"payment_id,provider,payment_date,amount,currency\n"
    b"pay_001,PSP_A,2026-09-14,100.00,EUR\n"  # -> 16 Sep, overdue
    b"pay_002,PSP_A,2026-09-15,75.50,EUR\n"  # -> 17 Sep, due today
    b"pay_003,PSP_B,2026-09-15,320.00,ILS\n"  # -> 16 Sep, settled
)
SETTLEMENTS_CSV = (
    b"settlement_id,payment_id,settlement_date,amount,currency\n"
    b"stl_001,pay_003,2026-09-16,320.00,ILS\n"
)


def configure_rules(client: TestClient) -> None:
    for provider, offset, rule_type in (
        ("PSP_A", 2, "BUSINESS_DAYS"),
        ("PSP_B", 1, "BUSINESS_DAYS"),
        ("PSP_C", 3, "CALENDAR_DAYS"),
    ):
        response = client.post(
            "/rules",
            data={"provider": provider, "offset_days": offset, "rule_type": rule_type},
            follow_redirects=False,
        )
        assert response.status_code == 303


def load_demo(client: TestClient) -> None:
    configure_rules(client)
    client.post(
        "/upload/payments",
        files={"file": ("payments.csv", PAYMENTS_CSV, "text/csv")},
    )
    client.post(
        "/upload/settlements",
        files={"file": ("settlements.csv", SETTLEMENTS_CSV, "text/csv")},
    )


def test_health(client: TestClient) -> None:
    payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["as_of"] == "2026-09-17"


def test_empty_dashboard_invites_an_upload(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Nothing to reconcile yet" in response.text


def test_rules_are_saved_and_listed(client: TestClient) -> None:
    configure_rules(client)
    page = client.get("/rules").text
    assert "PSP_A" in page
    assert "T+2 business days" in page
    assert "T+3 calendar days" in page


def test_invalid_rule_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/rules",
        data={"provider": "PSP_X", "offset_days": -5, "rule_type": "BUSINESS_DAYS"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_full_flow_dashboard_shows_statuses(client: TestClient) -> None:
    load_demo(client)
    page = client.get("/").text
    assert "OVERDUE" in page
    assert "DUE_TODAY" in page
    assert "SETTLED" in page
    assert "EUR" in page and "ILS" in page

    summary = client.get("/api/summary").json()
    assert summary["payments"] == 3
    assert summary["status_counts"]["OVERDUE"] == 1
    assert summary["status_counts"]["DUE_TODAY"] == 1
    assert summary["status_counts"]["SETTLED"] == 1
    currencies = {item["currency"]: item for item in summary["currencies"]}
    assert currencies["EUR"]["overdue"] == "100.00"
    assert currencies["EUR"]["due_today"] == "75.50"
    assert currencies["ILS"]["overdue"] == "0.00"


def test_filters(client: TestClient) -> None:
    load_demo(client)
    overdue_page = client.get("/?status=overdue").text
    assert "pay_001" in overdue_page
    assert "pay_002" not in overdue_page

    due_page = client.get("/?status=due_today").text
    assert "pay_002" in due_page
    assert "pay_001" not in due_page

    currency_page = client.get("/?status=all&currency=ILS").text
    assert "pay_003" in currency_page
    assert "pay_001" not in currency_page


def test_overdue_export_endpoint(client: TestClient) -> None:
    load_demo(client)
    response = client.get("/export/overdue.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "overdue.csv" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert [row["payment_id"] for row in rows] == ["pay_001"]
    assert rows[0]["days_overdue"] == "1"


def test_xlsx_export_endpoint(client: TestClient) -> None:
    load_demo(client)
    response = client.get("/export/all.xlsx")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"


def test_payment_detail_shows_the_friday_rule(client: TestClient) -> None:
    configure_rules(client)
    client.post(
        "/upload/payments",
        files={
            "file": (
                "payments.csv",
                b"payment_id,provider,payment_date,amount,currency\n"
                b"pay_friday,PSP_A,2026-09-18,500.00,EUR\n",
                "text/csv",
            )
        },
    )
    page = client.get("/payments/pay_friday").text
    assert "Friday" in page
    assert "2026-09-22" in page
    assert "Tuesday" in page
    assert client.get("/payments/does_not_exist").status_code == 404


def test_calendar_view(client: TestClient) -> None:
    load_demo(client)
    page = client.get("/calendar").text
    assert "PSP_A" in page
    assert "today" in page


def test_mapping_flow(client: TestClient) -> None:
    configure_rules(client)
    response = client.post(
        "/upload/payments",
        files={
            "file": (
                "mystery.csv",
                b"col_a,col_b,col_c,col_d,col_e\npay_m1,PSP_A,2026-09-14,100.00,EUR\n",
                "text/csv",
            )
        },
    )
    assert "Map your columns" in response.text
    token = response.text.split('action="/mapping/')[1].split('"')[0]

    applied = client.post(
        f"/mapping/{token}",
        data={
            "payment_id": "col_a",
            "provider": "col_b",
            "payment_date": "col_c",
            "amount": "col_d",
            "currency": "col_e",
        },
    )
    assert "imported 1" in applied.text
    assert client.get("/api/summary").json()["payments"] == 1
    assert client.post("/mapping/does-not-exist", data={}).status_code == 404


def test_bad_upload_is_reported_not_crashed(client: TestClient) -> None:
    response = client.post(
        "/upload/payments", files={"file": ("broken.pdf", b"%PDF-1.4", "application/pdf")}
    )
    assert response.status_code == 200
    assert "unsupported file type" in response.text
    assert client.post(
        "/upload/unknown", files={"file": ("a.csv", b"x", "text/csv")}
    ).status_code == 404


def test_reset_clears_data(client: TestClient) -> None:
    load_demo(client)
    assert client.get("/api/summary").json()["payments"] == 3
    client.post("/reset", data={}, follow_redirects=False)
    assert client.get("/api/summary").json()["payments"] == 0
    assert "PSP_A" in client.get("/rules").text  # rules survive by default


def test_demo_dataset_through_the_http_api(client: TestClient) -> None:
    configure_rules(client)
    client.post(
        "/upload/payments",
        files={"file": ("payments.csv", (DEMO_DIR / "payments.csv").read_bytes(), "text/csv")},
    )
    client.post(
        "/upload/settlements",
        files={
            "file": ("settlements.csv", (DEMO_DIR / "settlements.csv").read_bytes(), "text/csv")
        },
    )
    summary = client.get("/api/summary").json()
    assert summary["status_counts"]["OVERDUE"] == 12
    assert summary["status_counts"]["DUE_TODAY"] == 7
    assert summary["status_counts"]["SETTLED_LATE"] == 5

    overdue = list(csv.DictReader(io.StringIO(client.get("/export/overdue.csv").text)))
    assert len(overdue) == 12
    assert {row["status"] for row in overdue} == {"OVERDUE"}


def test_htmx_panel_partial(client: TestClient) -> None:
    load_demo(client)
    partial = client.get("/partials/panel?status=overdue")
    assert partial.status_code == 200
    assert "pay_001" in partial.text
    assert "<html" not in partial.text  # a fragment, not a full page
    assert 'hx-target="#panel"' in partial.text
