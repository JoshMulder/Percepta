from _env import platform_url
import asyncio, os, sys
from playwright.async_api import async_playwright
BASE=platform_url()
import os as _os
SEL=_os.environ.get("SELECTORS","").split(",") if _os.environ.get("SELECTORS") else [
     "header.topbar",".station-select",".station-switch",".station-trigger",
     ".station-status",".link-state",".topbar-right",".logo","header.topbar > *"]
async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(); c=await b.new_context(viewport={"width":1920,"height":1080},ignore_https_errors=True)
        pg=await c.new_page(); await pg.goto(BASE,wait_until="domcontentloaded")
        try:
            await pg.wait_for_selector("input[type=password]",timeout=5000)
            i=pg.locator("form input"); await i.nth(0).fill("admin@percepta.local"); await i.nth(1).fill("percepta-dev-2026")
            await pg.click("button[type=submit]"); await pg.wait_for_selector("header.topbar",timeout=15000)
        except Exception: pass
        await pg.wait_for_timeout(7000)
        for sel in SEL:
            loc=pg.locator(sel); n=await loc.count()
            for k in range(min(n,6)):
                bb=await loc.nth(k).bounding_box()
                if not bb: continue
                cls=await loc.nth(k).get_attribute("class")
                print(f"{sel}[{k}] {str(cls)[:28]:28} x={bb['x']:7.1f} w={bb['width']:6.1f} "
                      f"cx={bb['x']+bb['width']/2:7.1f}  y={bb['y']:6.1f} h={bb['height']:5.1f} "
                      f"cy={bb['y']+bb['height']/2:6.1f}")
        await b.close()
asyncio.run(main())
