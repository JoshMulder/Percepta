#!/usr/bin/env python3
"""Assert where the enrolment code section appears, and that panes are one size.

Reads the DOM rather than a screenshot, because "is that heading present" and
"are these two panels the same height" are questions a picture answers badly and
a measurement answers exactly. Same reason `measure.py` exists.

Development only - it signs in with the seeded development credentials and
trusts the platform's self-signed certificate.
"""

import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE = os.environ.get("PERCEPTA_URL", "https://192.168.2.49:8000")
EMAIL = os.environ.get("PERCEPTA_EMAIL", "admin@percepta.local")
PASSWORD = os.environ.get("PERCEPTA_PASSWORD", "percepta-dev-2026")

TABS = ["My account", "Radio", "Stations", "Organisation"]


async def headings(page) -> list[str]:
    return await page.eval_on_selector_all(
        ".settings-pane h3", "els => els.map(e => e.textContent.trim())"
    )


async def main() -> int:
    failures: list[str] = []

    def check(ok: bool, label: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            failures.append(label)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080}, ignore_https_errors=True
        )
        page = await context.new_page()
        await page.goto(BASE, wait_until="domcontentloaded")

        await page.wait_for_selector("input[type=password]", timeout=10000)
        inputs = page.locator("form input")
        await inputs.nth(0).fill(EMAIL)
        await inputs.nth(1).fill(PASSWORD)
        await page.click("button[type=submit]")
        await page.wait_for_selector("header.topbar", timeout=15000)
        await page.wait_for_timeout(5000)

        await page.click("button.settings-toggle")
        await page.wait_for_selector(".settings", timeout=5000)

        # --- every pane the same size -------------------------------------
        print("\nModal size across tabs")
        sizes = []
        for tab in TABS:
            await page.click(f'button.settings-tab:has-text("{tab}")')
            await page.wait_for_timeout(600)
            box = await page.locator(".settings").bounding_box()
            sizes.append((tab, round(box["width"], 1), round(box["height"], 1)))
            print(f"    {tab:<14} {box['width']:.1f} x {box['height']:.1f}")
        check(len({(w, h) for _, w, h in sizes}) == 1, "all panes identical size")

        # --- the enrolment code section -----------------------------------
        await page.click('button.settings-tab:has-text("Stations")')
        await page.wait_for_timeout(800)

        options = await page.eval_on_selector_all(
            ".station-picker select option",
            "els => els.map(e => ({v: e.value, t: e.textContent.trim()}))",
        )

        enrolled = [o for o in options if "Workshop Pi" not in o["t"]]
        workshop = [o for o in options if "Workshop Pi" in o["t"]]

        if enrolled:
            print(f"\nEnrolled station: {enrolled[0]['t']}")
            await page.select_option(".station-picker select", enrolled[0]["v"])
            await page.wait_for_timeout(1200)
            hs = await headings(page)
            print(f"    sections: {hs}")
            check("Enrolment code" not in hs, "no code section on an enrolled station")
            check("Cut this station off" in hs, "revoke offered on an enrolled station")

        if workshop:
            print(f"\nCreated, never enrolled: {workshop[0]['t']}")
            await page.select_option(".station-picker select", workshop[0]["v"])
            await page.wait_for_timeout(1200)
            hs = await headings(page)
            print(f"    sections: {hs}")
            check("Enrolment code" in hs, "code section on an unenrolled station")
            check(
                "Cut this station off" not in hs,
                "no revoke on a station that never enrolled",
            )
            check(
                await page.locator('button:has-text("Issue a code")').count() == 1,
                "issue button present",
            )

        # --- the add-new page ---------------------------------------------
        print("\nAdd new")
        await page.click('.station-picker button:has-text("Add new")')
        await page.wait_for_timeout(700)
        hs = await headings(page)
        print(f"    sections: {hs}")
        check("New station" in hs, "the create form is the first step")
        check(
            "Enrolment code" not in hs,
            "no code section before the station exists",
        )

        await browser.close()

    print(f"\n{'ALL PASS' if not failures else str(len(failures)) + ' FAILED'}")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
