"""One projection of a station's frames into the vitals a wall shows.

TWO CALLERS, ONE FUNCTION, AND THAT IS THE ENTIRE POINT.

`api/platform.py::_vitals` reads the ingest's cached snapshot at request time;
`services/odin_digest.py::note` reads the live frame on the ingest hot path. Same
input shape, same output keys, two hand-written copies — and they had already
drifted by three fields before anybody noticed, because the wall PREFERS the
digest whenever its socket is up (`OdinWall.tsx`). The result was a preview
drawer whose "Worst condition", "Uplink offline" and "Running version" rows read
"—" precisely when the live feed was healthy, and showed real values only after
the socket dropped and the poll took over. A wall that is more informative when
it is degraded is worse than useless: it teaches the operator to trust the wrong
state.

So the projection lives here and both call it. A field added to one is added to
both, by construction, rather than by remembering.

WHAT THIS DOES NOT DO. It never re-judges what a station said. Severity ranking
uses the STATION'S OWN severity string, the worst condition is named with the
station's own identifier for it, and anything unrecognised ranks lowest rather
than being reinterpreted. The platform's job is to relay a station's account of
itself, not to second-guess it.

Fail-soft throughout: every field is optional, every read is type-checked, and a
malformed frame costs the fields it malformed rather than the station's row.
"""

from __future__ import annotations

#: How the station's own severity words rank against each other.
#:
#: Both vocabularies are here because both are used: the health module emits
#: critical/warning/info, and device status uses failing/degraded. Anything not
#: listed ranks 0 — an unknown severity must not sort ABOVE a known critical one
#: just because it is unfamiliar.
_SEVERITY_ORDER = {
    "critical": 3,
    "failing": 3,
    "warning": 2,
    "degraded": 2,
    "info": 1,
}


def worst_condition_of(conditions: list) -> tuple[str | None, int]:
    """The worst open condition's NAME, and how many there are.

    Returns the condition's `id` — which is what a station actually sends.

    This read `worst.get("code") or worst.get("name") or worst.get("message")`
    and a condition on the wire is `{id, severity, detail, since}`
    (`station/gsu/health.py`), so not one of those three keys has ever existed.
    `worst_condition` was therefore None on every station on the fleet view since
    the field was added — silently, because None renders as "—" and "no open
    conditions" is a perfectly plausible thing for a wall to say.

    `detail` is deliberately NOT the fallback. It is a human sentence, sized for
    a log line, and a tile has room for a name.
    """
    if not isinstance(conditions, list) or not conditions:
        return None, 0
    worst = max(
        conditions,
        key=lambda c: _SEVERITY_ORDER.get(str((c or {}).get("severity", "")), 0),
    )
    name = worst.get("id") if isinstance(worst, dict) else None
    return (name if isinstance(name, str) else None), len(conditions)


def project_health(frame: dict) -> dict:
    """The vitals a health frame carries. Keys match FleetStation exactly."""
    out: dict = {}
    if not isinstance(frame, dict):
        return out

    status = frame.get("status")
    out["health"] = status if isinstance(status, str) else None

    name, count = worst_condition_of(frame.get("conditions"))
    out["worst_condition"] = name
    out["condition_count"] = count

    uplink = frame.get("uplink")
    if isinstance(uplink, dict):
        connected = uplink.get("connected")
        out["uplink_connected"] = connected if isinstance(connected, bool) else None
        offline = uplink.get("offline_seconds")
        out["uplink_offline_seconds"] = (
            float(offline) if isinstance(offline, (int, float)) else None
        )

    devices = frame.get("devices")
    if isinstance(devices, list):
        slots: dict[str, str] = {}
        simulated: list[str] = []
        for d in devices:
            if not isinstance(d, dict):
                continue
            slot = d.get("slot")
            if not isinstance(slot, str):
                continue
            state = d.get("status")
            slots[slot] = state if isinstance(state, str) else "unknown"
            if d.get("simulated") is True:
                simulated.append(slot)
        out["slots"] = slots
        # Sorted, because this reaches a UI and an order that follows whatever
        # order the station happened to enumerate its hardware in makes a tile
        # appear to change when nothing has.
        out["simulated_slots"] = sorted(simulated)

    software = frame.get("software")
    running = software.get("running_version") if isinstance(software, dict) else None
    # `agent_version` is the fallback because a station that predates the
    # software block still reports one, and "unknown version" on a wall is a
    # question somebody has to go and answer by hand.
    out["running_version"] = running or frame.get("agent_version")
    return out


def project_power(frame: dict) -> dict:
    """The vitals a power frame carries. Keys match FleetStation exactly."""
    out: dict = {}
    if not isinstance(frame, dict):
        return out

    soc = frame.get("soc_pct")
    out["soc_pct"] = float(soc) if isinstance(soc, (int, float)) else None
    load = frame.get("load_w")
    out["load_w"] = float(load) if isinstance(load, (int, float)) else None

    mains, gen = frame.get("mains_w"), frame.get("generator_w")
    if isinstance(mains, (int, float)) or isinstance(gen, (int, float)):
        # Only decided when at least one source was actually reported. A station
        # that sends neither is not "on battery" — it is a station that does not
        # report where its power comes from, and those are different claims.
        out["on_battery"] = (mains or 0) <= 0 and (gen or 0) <= 0
    return out
