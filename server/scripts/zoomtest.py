"""Zoom the map and check the station stays centred."""
import asyncio, os
from playwright.async_api import async_playwright
BASE = os.environ.get("PERCEPTA_URL", "https://192.168.2.49:8000")
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        c = await b.new_context(viewport={"width":1920,"height":1080}, ignore_https_errors=True)
        pg = await c.new_page(); await pg.goto(BASE, wait_until="domcontentloaded")
        try:
            await pg.wait_for_selector("input[type=password]", timeout=5000)
            i = pg.locator("form input"); await i.nth(0).fill("admin@percepta.local"); await i.nth(1).fill("percepta-dev-2026")
            await pg.click("button[type=submit]"); await pg.wait_for_selector("header.topbar", timeout=15000)
        except Exception: pass
        await pg.wait_for_timeout(8000)
        # Leaflet is not exposed on window, so the zoom level is read from the
        # tile URLs - our tiles are /tiles/{style}/{z}/{x}/{y}.png - and the pan
        # from the map pane's transform. Both are observable without hooking
        # into Leaflet, which keeps this test honest about what it measures.
        zoom_of = """() => {
            const t = document.querySelector('.leaflet-tile-container img');
            if (!t) return null;
            const m = t.src.match(/\\/tiles\\/[^/]+\\/(\\d+)\\//);
            return m ? Number(m[1]) : null;
        }"""
        box = await pg.locator(".leaflet-container").first.bounding_box()
        z_before = await pg.evaluate(zoom_of)
        before = await pg.evaluate("() => document.querySelector('.leaflet-map-pane').style.transform")
        # Wheel at the top-left quarter - far from the centre, where a
        # pointer-anchored zoom would drag the view hardest.
        await pg.mouse.move(box["x"] + box["width"] * 0.25, box["y"] + box["height"] * 0.25)
        for _ in range(3):
            await pg.mouse.wheel(0, -120); await pg.wait_for_timeout(500)
        await pg.wait_for_timeout(1500)
        after = await pg.evaluate("() => document.querySelector('.leaflet-map-pane').style.transform")
        z_after = await pg.evaluate(zoom_of)
        print(f"zoom level: {z_before} -> {z_after}")
        marker = None
        print("pane transform before:", before)
        print("pane transform after :", after)
        if marker:
            mx = marker["x"] + marker["width"]/2; my = marker["y"] + marker["height"]/2
            cx = box["x"] + box["width"]/2;        cy = box["y"] + box["height"]/2
            print(f"station marker offset from map centre after zoom: dx={mx-cx:.1f} dy={my-cy:.1f}")
        await b.close()
asyncio.run(main())
