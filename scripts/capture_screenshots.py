"""Seed a running Settlement Calendar instance with the demo data and screenshot it.

Used by the ``assets`` GitHub Actions workflow to keep ``docs/screenshots`` in
sync with the UI, and usable locally:

    uvicorn app.main:app --port 8000 &
    python scripts/capture_screenshots.py --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx
from PIL import Image
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = REPO_ROOT / "demo-data"
OUT_DIR = REPO_ROOT / "docs" / "screenshots"

RULES = (
    ("PSP_A", 2, "BUSINESS_DAYS", "AE"),
    ("PSP_B", 1, "BUSINESS_DAYS", ""),
    ("PSP_C", 3, "CALENDAR_DAYS", ""),
)

PAGES = (
    ("/", "dashboard.png", 1360, 900),
    ("/?status=overdue", "overdue.png", 1360, 1000),
    ("/calendar", "calendar.png", 1360, 900),
    ("/rules", "rules.png", 1360, 620),
    ("/payments/pay_0178", "payment-detail.png", 1360, 900),
    ("/payments/pay_0021", "holiday-explanation.png", 1360, 900),
    ("/calendars/AE", "holiday-calendar.png", 1360, 1000),
)


def seed(base_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=60.0, follow_redirects=True) as client:
        client.post("/reset", data={"include_rules": "1"})
        for provider, offset, rule_type, calendar_code in RULES:
            client.post(
                "/rules",
                data={
                    "provider": provider,
                    "offset_days": offset,
                    "rule_type": rule_type,
                    "calendar_code": calendar_code,
                },
            )
        for kind, filename in (("payments", "payments.csv"), ("settlements", "settlements.csv")):
            path = DEMO_DIR / filename
            client.post(
                f"/upload/{kind}",
                files={"file": (filename, path.read_bytes(), "text/csv")},
            )
        summary = client.get("/api/summary").json()
    print("seeded:", summary["status_counts"])


def capture(base_url: str, executable_path: str | None) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        launch_kwargs = {"executable_path": executable_path} if executable_path else {}
        browser = playwright.chromium.launch(**launch_kwargs)
        for path, name, width, height in PAGES:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(base_url + path, wait_until="networkidle")
            target = OUT_DIR / name
            page.screenshot(path=target)
            # Palette-compress so the repository stays small.
            Image.open(target).convert("P", palette=Image.ADAPTIVE, colors=128).save(
                target, optimize=True
            )
            print(f"{name}: {target.stat().st_size // 1024} KB")
            page.close()
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--chromium", default=None, help="explicit chromium executable path")
    args = parser.parse_args()
    seed(args.base_url)
    capture(args.base_url.rstrip("/"), args.chromium)


if __name__ == "__main__":
    main()
