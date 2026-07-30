"""The platform's half of station-owned position."""
import asyncio, uuid
from backend.services.station_ingest import StationIngest

async def main():
    ok = []
    def check(label, passed):
        ok.append(passed); print(f"  {'PASS' if passed else 'FAIL'}  {label}")

    ing = StationIngest.__new__(StationIngest)
    ing._simulated, ing._position, ing._seen = {}, {}, {}
    writes = []
    ing._write_position = lambda sid, fix: writes.append(fix)

    sid = uuid.uuid4()
    # A station that says nothing must not clear a stored position.
    await ing._reconcile_position(sid, {"kind": "health"})
    check("absent position writes nothing", writes == [])

    await ing._reconcile_position(sid, {"position": {"latitude": -43.53, "longitude": 172.62}})
    check("a reported fix is written", writes == [(-43.53, 172.62)])

    await ing._reconcile_position(sid, {"position": {"latitude": -43.53, "longitude": 172.62}})
    check("the same fix is not rewritten", len(writes) == 1)

    await ing._reconcile_position(sid, {"position": None})
    check("an explicit null clears it", writes[-1] is None)

    await ing._reconcile_position(sid, {"position": {"latitude": 999, "longitude": 0}})
    check("an out-of-range fix is refused", len(writes) == 2)

    await ing._reconcile_position(sid, {"position": {"latitude": "north", "longitude": 0}})
    check("garbage is refused", len(writes) == 2)

    print("\n" + ("ALL PASS" if all(ok) else "FAILURES"))
    return 0 if all(ok) else 1

raise SystemExit(asyncio.run(main()))
