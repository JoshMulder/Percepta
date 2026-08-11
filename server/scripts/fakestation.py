"""Push a real fragmented MP4 into /media/ingest as a station would.

Splits the file at `moof` boundaries: everything before the first one is the
initialisation segment, and each `moof`+`mdat` after it is a media fragment -
which is exactly the framing the contract asks a station for.
"""
from _env import platform_host
import asyncio, os, ssl, sys, websockets

HOST = platform_host()
CA = os.environ.get("PERCEPTA_CA", "/certs/ca.crt")
SECRET = os.environ["STATION_SECRET"]
PATH = os.environ.get("MP4", "/out/test.mp4")

def split(data: bytes):
    """Init segment, then one chunk per moof box."""
    offsets = []
    i = 0
    while True:
        i = data.find(b"moof", i + 1)
        if i < 0:
            break
        offsets.append(i - 4)          # box size precedes the type
    if not offsets:
        return data, []
    return data[: offsets[0]], [
        data[a : (offsets[k + 1] if k + 1 < len(offsets) else len(data))]
        for k, a in enumerate(offsets)
    ]

async def main():
    ctx = ssl.create_default_context(cafile=CA)
    raw = open(PATH, "rb").read()
    init, frags = split(raw)
    print(f"  init {len(init)}B, {len(frags)} fragments")
    # extra_headers, not additional_headers: the legacy asyncio client in
    # websockets 13 uses the older name, and the failure surfaces at connect
    # time rather than at construction.
    async with websockets.connect(
        f"wss://{HOST}/media/ingest", ssl=ctx,
        extra_headers={"Authorization": f"Bearer {SECRET}"},
    ) as ws:
        await ws.send('{"codec": "avc1.42C01E"}')
        await ws.send(init)
        import time
        deadline = time.time() + float(os.environ.get("SECONDS", "60"))
        sent = 0
        while time.time() < deadline:
            for f in frags:
                await ws.send(f)
                sent += 1
                await asyncio.sleep(0.25)
            print(f"  {sent} fragments sent, {deadline - time.time():.0f}s left", flush=True)
    print("  station finished")

asyncio.run(main())
