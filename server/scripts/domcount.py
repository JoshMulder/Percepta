from _env import platform_url
import asyncio, os
from playwright.async_api import async_playwright
BASE=platform_url()
async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(); c=await b.new_context(viewport={"width":1920,"height":1080},ignore_https_errors=True)
        pg=await c.new_page(); errs=[]
        pg.on("console", lambda m: errs.append(m.text) if m.type=="error" else None)
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto(BASE,wait_until="domcontentloaded")
        try:
            await pg.wait_for_selector("input[type=password]",timeout=5000)
            i=pg.locator("form input"); await i.nth(0).fill("admin@percepta.local"); await i.nth(1).fill("percepta-dev-2026")
            await pg.click("button[type=submit]"); await pg.wait_for_selector("header.topbar",timeout=15000)
        except Exception: pass
        await pg.wait_for_timeout(12000)
        for sel in [".maplibregl-canvas",".map-contact",".map-ring-label",".map-station",".basemap-btn"]:
            print(f"  {sel}: {await pg.locator(sel).count()}")
        print("  console errors:", errs[:4] if errs else "none")
        await b.close()
asyncio.run(main())
