"""Zoom the map hard, off-centre, and check the station does not move.

The station marker is a DOM element MapLibre positions from the map's own
projection, so its screen position is a direct readout of where the map thinks
the station is. If zoom is anchored on the centre, it stays put; if it is
anchored on the pointer, it walks away from the middle.
"""
from _env import platform_url
import asyncio, os
from playwright.async_api import async_playwright
BASE=platform_url()
async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(); c=await b.new_context(viewport={"width":1920,"height":1080},ignore_https_errors=True)
        pg=await c.new_page(); await pg.goto(BASE,wait_until="domcontentloaded")
        try:
            await pg.wait_for_selector("input[type=password]",timeout=5000)
            i=pg.locator("form input"); await i.nth(0).fill("admin@percepta.local"); await i.nth(1).fill("percepta-dev-2026")
            await pg.click("button[type=submit]"); await pg.wait_for_selector("header.topbar",timeout=15000)
        except Exception: pass
        await pg.wait_for_timeout(11000)
        box=await pg.locator(".map-canvas").first.bounding_box()
        cx=box["x"]+box["width"]/2; cy=box["y"]+box["height"]/2
        async def offset():
            m=await pg.locator(".map-station").first.bounding_box()
            return (m["x"]+m["width"]/2-cx, m["y"]+m["height"]/2-cy)
        def z_of(u):
            import re; m=re.search(r"/tiles/[^/]+/(\d+)/",u); return int(m.group(1)) if m else None
        zs=[]
        pg.on("request", lambda r: zs.append(z_of(r.url)) if "/tiles/" in r.url else None)
        print(f"  station offset before: dx={offset.__name__ and (await offset())[0]:.1f}")
        d0=await offset()
        # Pointer well off-centre, where a pointer-anchored zoom drags hardest.
        await pg.mouse.move(box["x"]+box["width"]*0.22, box["y"]+box["height"]*0.22)
        for _ in range(6):
            await pg.mouse.wheel(0,-120); await pg.wait_for_timeout(350)
        await pg.wait_for_timeout(2500)
        d1=await offset()
        seen=[z for z in zs if z]
        print(f"  station offset before zoom: dx={d0[0]:+.1f} dy={d0[1]:+.1f}")
        print(f"  station offset after  zoom: dx={d1[0]:+.1f} dy={d1[1]:+.1f}")
        print(f"  tile zoom levels requested: {sorted(set(seen))}")
        await b.close()
asyncio.run(main())
