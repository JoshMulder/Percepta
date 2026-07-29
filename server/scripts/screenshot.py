#!/usr/bin/env python3
"""Take a screenshot of the console, so UI work can be looked at rather than
reasoned about.

    .venv/bin/python scripts/screenshot.py                     # whole console
    .venv/bin/python scripts/screenshot.py --clip topbar       # just the header
    .venv/bin/python scripts/screenshot.py --open settings     # a panel

Why this exists: the browser extension normally used for this waits for
`document_idle`, and the console never reaches it - telemetry arrives several
times a second, the map redraws, video frames land. The page is never quiet, so
injection times out every time. That is not a fault in the extension; it is a
property of a live dashboard, and it means a whole class of layout mistakes were
being fixed by reading the stylesheet and guessing.

Playwright drives its own Chromium and does not wait for idle, so it works on
exactly the page the extension cannot reach.

Development only. It signs in with the seeded development credentials and trusts
the platform's self-signed certificate, neither of which belongs anywhere near a
real deployment.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    sys.exit(
        "playwright is not installed. Run:\n"
        "  .venv/bin/python -m pip install playwright\n"
        "  .venv/bin/python -m playwright install chromium"
    )

BASE = os.environ.get("PERCEPTA_URL", "https://192.168.2.49:8000")
EMAIL = os.environ.get("PERCEPTA_EMAIL", "admin@percepta.local")
PASSWORD = os.environ.get("PERCEPTA_PASSWORD", "percepta-dev-2026")

#: Regions worth looking at on their own. Full-page shots of a 2000px console
#: make a ten-pixel alignment fault invisible, which is how three of them
#: survived being "fixed".
CLIPS = {
    "topbar": {"x": 0, "y": 0, "width": 1920, "height": 60},
    "sidebar": {"x": 1450, "y": 0, "width": 470, "height": 1080},
    "radio": {"x": 1450, "y": 260, "width": 470, "height": 240},
    "weather": {"x": 1450, "y": 470, "width": 470, "height": 260},
}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="/tmp/percepta.png")
    ap.add_argument("--clip", choices=sorted(CLIPS), help="capture one region")
    ap.add_argument("--open", choices=["settings"], help="open a panel first")
    ap.add_argument("--click", help="CSS selector to click before capturing")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--settle", type=float, default=6.0,
                    help="seconds to let telemetry arrive and the fit settle")
    args = ap.parse_args()

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(
            viewport={"width": args.width, "height": args.height},
            # The platform serves a private-CA certificate. Accepting it here is
            # right for a development screenshot and would be wrong anywhere a
            # station is enrolled.
            ignore_https_errors=True,
        )
        page = await context.new_page()
        await page.goto(BASE, wait_until="domcontentloaded")

        # Sign in if the login form is showing.
        try:
            await page.wait_for_selector("input[type=password]", timeout=5000)
            # By position, not by type. The email field is a plain text input -
            # the API takes a string rather than an EmailStr on purpose, so that
            # a malformed address fails the same way a wrong one does - and
            # selecting on input[type=email] silently matched nothing.
            inputs = page.locator("form input")
            await inputs.nth(0).fill(EMAIL)
            await inputs.nth(1).fill(PASSWORD)
            await page.click("button[type=submit]")
            await page.wait_for_selector("header.topbar", timeout=15000)
        except Exception as exc:
            print(f"  (login step: {exc})", file=sys.stderr)

        # The console scales itself once telemetry has arrived and panels know
        # their height, so a shot taken too early is of a layout that no longer
        # exists a second later.
        await page.wait_for_timeout(int(args.settle * 1000))

        if args.open == "settings":
            await page.click("button.settings-toggle")
            await page.wait_for_timeout(700)
        if args.click:
            await page.click(args.click)
            await page.wait_for_timeout(500)

        out = Path(args.out)
        await page.screenshot(
            path=str(out), clip=CLIPS[args.clip] if args.clip else None
        )
        print(f"wrote {out} ({out.stat().st_size // 1024} kB)")
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
