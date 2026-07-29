"""Watch the live stream in a real browser and prove it decoded.

readyState and videoWidth come from the media element itself, so a pass here
means the browser actually decoded H.264 that arrived through the relay - not
that bytes moved.
"""
import asyncio, os
from playwright.async_api import async_playwright
BASE = os.environ.get("PERCEPTA_URL", "https://192.168.2.49:8000")
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        c = await b.new_context(viewport={"width":1920,"height":1080}, ignore_https_errors=True)
        pg = await c.new_page()
        errs = []
        frames = {"text": 0, "binary": 0, "first_text": None}
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" else None)

        def on_ws(ws):
            if "/media/view" not in ws.url:
                return
            def got(payload):
                if isinstance(payload, str):
                    frames["text"] += 1
                    if frames["first_text"] is None:
                        frames["first_text"] = payload[:80]
                else:
                    frames["binary"] += 1
            ws.on("framereceived", got)
        pg.on("websocket", on_ws)
        await pg.goto(BASE, wait_until="domcontentloaded")
        try:
            await pg.wait_for_selector("input[type=password]", timeout=5000)
            i = pg.locator("form input"); await i.nth(0).fill("admin@percepta.local"); await i.nth(1).fill("percepta-dev-2026")
            await pg.click("button[type=submit]"); await pg.wait_for_selector("header.topbar", timeout=15000)
        except Exception: pass
        await pg.wait_for_timeout(9000)
        # Pick the station the test is actually publishing to. The console
        # defaults to the first alphabetically, which is a different one - and
        # attaching to a station nobody is streaming gives exactly the same
        # empty video element as a broken player.
        want = os.environ.get("STATION_NAME", "Station Agent Bench")
        try:
            await pg.click(".station-trigger", timeout=5000)
            await pg.click(f".station-option:has-text('{want}')", timeout=5000)
            await pg.wait_for_timeout(6000)
            print(f"  selected station: {want}")
        except Exception as e:
            print("  could not select station:", e)
        # Swap the camera into the main view - that is what asks for the stream.
        try:
            await pg.click(".swap-preview", timeout=5000)
        except Exception as e:
            print("  could not swap to camera:", e)
        await pg.wait_for_timeout(12000)
        info = await pg.evaluate("""() => {
            const v = document.querySelector('video.video-live-el');
            if (!v) return {found:false};
            return {found:true, readyState:v.readyState, w:v.videoWidth, h:v.videoHeight,
                    t:Number(v.currentTime.toFixed(2)), paused:v.paused,
                    buffered:v.buffered.length ? Number(v.buffered.end(v.buffered.length-1).toFixed(2)) : 0};
        }""")
        print("  video element:", info)
        print("  frames to browser:", frames)
        print("  page errors:", errs[:3] if errs else "none")
        await pg.screenshot(path="/out/video.png")
        await b.close()
asyncio.run(main())
