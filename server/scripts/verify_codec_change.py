"""What the relay does when a station's codec changes mid-stream.

    docker exec -w /app -e PYTHONPATH=/app percepta-app \
        python /app/../scripts/verify_codec_change.py

A camera's encoder is a checkbox in its own web interface, so this is not an
exotic case: switching one from H.265 to H.264 while a console was watching is
what found the bug. The relay used to keep the old init segment and the old
fragments, and the viewer ignored the second codec entirely, so the picture
degraded silently rather than restarting.
"""
import asyncio, json, uuid
from backend.realtime.media import MediaRelay

async def main():
    ok = []
    def check(label, passed):
        ok.append(passed)
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")

    relay = MediaRelay()
    sid, org = uuid.uuid4(), uuid.uuid4()
    stream, queue = await relay.attach(sid, org)
    await relay.publisher_connected(sid, org)

    # First announcement: no reset, viewers told the codec.
    await relay.set_codec(sid, "avc1.640028")
    first = json.loads(queue.get_nowait())
    check("first codec announced", first["codec"] == "avc1.640028")
    check("first is not a reset", first["reset"] is False)

    await relay.publish(sid, b"INIT-H264", is_init=True)
    queue.get_nowait()                      # the init segment fans out
    await relay.publish(sid, b"frame-1")
    queue.get_nowait()
    check("init segment retained", stream.init_segment == b"INIT-H264")

    # The camera's encoder changes underneath the stream.
    await relay.set_codec(sid, "hvc1.1.6.L120.90")
    second = json.loads(queue.get_nowait())
    check("change announced", second["codec"] == "hvc1.1.6.L120.90")
    check("change is flagged as a reset", second["reset"] is True)
    check("stale init segment dropped", stream.init_segment is None)
    check("stale fragments dropped", stream.recent == [])

    # Same codec again must not thrash a working viewer.
    await relay.set_codec(sid, "hvc1.1.6.L120.90")
    check("repeat of the same codec says nothing", queue.empty())

    print("\n" + ("ALL PASS" if all(ok) else "FAILURES"))
    return 0 if all(ok) else 1

raise SystemExit(asyncio.run(main()))
