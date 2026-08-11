#!/usr/bin/env python3
"""What a platform admin can actually reach, in each organisation.

The interesting case is not the platform org - it is a platform admin who has
switched into a customer's organisation, where they are a guest with synthesised
admin roles. A tab that appears and then fails is worse than one that is absent,
so this checks the tabs on screen against what the API will serve.

Development only: seeded credentials, self-signed certificate.
"""

from _env import platform_url
import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE = platform_url()
EMAIL = os.environ["PERCEPTA_EMAIL"]
PASSWORD = os.environ["PERCEPTA_PASSWORD"]


async def main() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080}, ignore_https_errors=True
        )
        page = await context.new_page()

        failed: list[str] = []
        page.on(
            "response",
            lambda r: failed.append(f"{r.status} {r.url.split(BASE)[-1]}")
            if r.status >= 400 and "/api/" in r.url
            else None,
        )

        await page.goto(BASE, wait_until="domcontentloaded")
        await page.wait_for_selector("input[type=password]", timeout=10000)
        inputs = page.locator("form input")
        await inputs.nth(0).fill(EMAIL)
        await inputs.nth(1).fill(PASSWORD)
        await page.click("button[type=submit]")
        await page.wait_for_selector("header.topbar", timeout=15000)
        await page.wait_for_timeout(5000)

        async def tabs_and_panes(label: str) -> None:
            failed.clear()
            await page.click("button.settings-toggle")
            await page.wait_for_selector(".settings", timeout=5000)
            names = await page.eval_on_selector_all(
                ".settings-tab", "els => els.map(e => e.textContent.trim())"
            )
            print(f"\n{label}")
            print(f"  tabs: {names}")
            for name in names:
                failed.clear()
                await page.click(f'button.settings-tab:has-text("{name}")')
                await page.wait_for_timeout(1500)
                errs = await page.eval_on_selector_all(
                    ".settings-pane .settings-error",
                    "els => els.map(e => e.textContent.trim())",
                )
                bad = " | ".join(failed) if failed else ""
                mark = "FAIL" if (errs or failed) else "ok  "
                print(f"    {mark} {name:<14} {'; '.join(errs)}  {bad}")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)

        await tabs_and_panes("Active org: Platform")

        # Switch into a customer organisation. Driven through the API rather
        # than the switcher widget: the question here is what the console does
        # once the session is in a customer org, not how the switcher is built.
        target = await page.evaluate(
            """async () => {
                const orgs = await (await fetch('/api/auth/organizations')).json();
                const customer = orgs.find(o => !o.is_platform);
                if (!customer) return null;
                await fetch('/api/auth/organization', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({organization_id: customer.id}),
                });
                return customer.name;
            }"""
        )
        if target:
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_selector("header.topbar", timeout=15000)
            await page.wait_for_timeout(5000)
            await tabs_and_panes(f"Active org: {target} (guest)")
        else:
            print("\n(no customer organisation to switch into)")

        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
