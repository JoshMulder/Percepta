#!/usr/bin/env python3
"""Does the station selection row stay put when the settings pane scrolls?

Measured rather than eyeballed: "looks pinned" and "is pinned" differ by a few
pixels of the row sliding under the dialog header, which a screenshot of a dark
UI hides completely.
"""

import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE = os.environ.get("PERCEPTA_URL", "https://192.168.2.49:8000")
EMAIL = os.environ.get("PERCEPTA_EMAIL", "admin@percepta.local")
PASSWORD = os.environ.get("PERCEPTA_PASSWORD", "percepta-dev-2026")


async def main() -> int:
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
        await page.click('button.settings-tab:has-text("Stations")')
        await page.wait_for_timeout(1200)

        row = page.locator(".station-picker")
        pane = page.locator(".settings-pane")

        before = (await row.bounding_box())["y"]
        await pane.evaluate("el => el.scrollTop = el.scrollHeight")
        await page.wait_for_timeout(700)
        after = (await row.bounding_box())["y"]
        scrolled = await pane.evaluate("el => el.scrollTop")
        pane_top = (await pane.bounding_box())["y"]

        print(f"  pane scrolled by      {scrolled:.0f}px")
        print(f"  row y before / after  {before:.1f} / {after:.1f}")
        print(f"  pane top              {pane_top:.1f}")

        ok_still = abs(after - before) < 1.0
        ok_at_top = abs(after - pane_top) < 1.0
        print(f"  {'PASS' if scrolled > 50 else 'FAIL'}  the pane actually scrolled")
        print(f"  {'PASS' if ok_still else 'FAIL'}  the row did not move")
        print(f"  {'PASS' if ok_at_top else 'FAIL'}  the row is flush with the top of the pane")

        await browser.close()
        return 0 if (scrolled > 50 and ok_still and ok_at_top) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
