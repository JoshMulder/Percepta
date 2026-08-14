"""Is a station online, offline, or dark — asked in one place.

Three modules derived this independently, from three constants that happened to
agree:

  - `api/stations.py` had OFFLINE_AFTER_SECONDS = 120 and its own `_online()`,
    for the per-organisation station list.
  - `api/platform.py` had ONLINE_WITHIN = timedelta(seconds=120), with a comment
    saying it was "reproduced here rather than importing across API modules".
  - `services/station_watch.py` had DARK_AFTER_SECONDS = 15 * 60, for the alarm.

Odin is the fourth consumer, and it puts the three side by side on one screen:
the tile, the map marker and the alert rail all describing the same station at
the same instant. Disagreement between them stops being a tidiness question and
becomes an operator asking which of the three to believe — at which point the
answer is none of them.

The windows are deliberately generous. A Starlink obstruction dropout is normal
at these sites and must not flap the status of every station on the map, and
`dark` is for a box that is not coming back on its own rather than one that is
between frames.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

#: Heard within this window and the station is online. The onboard computer
#: reports far more often; the slack absorbs a satellite dropout.
ONLINE_WITHIN = timedelta(seconds=120)

#: Silent for longer than this and it is not merely offline. Well clear of the
#: window above, so an obstruction never trips it.
DARK_AFTER = timedelta(minutes=15)

#: Deliberately three values with `dark` as a separate flag, rather than four
#: with dark as a status of its own. That is the shape already on the wire
#: (api/platform.py's FleetStation) and already switched on by the console, and a
#: shared derivation is the wrong place to redefine a shipping contract. Dark is
#: a modifier on offline: every dark station is also offline, and a reader that
#: only understands "offline" is still right about it.
StationStatus = Literal["online", "offline", "never"]


def status_for(
    last_seen_at: datetime | None, *, now: datetime | None = None
) -> tuple[StationStatus, bool]:
    """The station's liveness, and whether it counts as dark.

    Returns ("never", False) for a station that has never reported at all — an
    enrolled box that has not yet dialled home is a different thing from one that
    has gone quiet, and an alarm about it would be an alarm about a deployment in
    progress.

    `now` is injectable so a caller sweeping hundreds of stations can stamp one
    instant across the batch rather than letting the clock move underneath the
    loop, which is how a sort by status ends up unstable.
    """
    if last_seen_at is None:
        return "never", False
    moment = now or datetime.now(UTC)
    silence = moment - last_seen_at
    if silence < ONLINE_WITHIN:
        return "online", False
    return "offline", silence >= DARK_AFTER


def is_online(last_seen_at: datetime | None, *, now: datetime | None = None) -> bool:
    """The plain question, for callers that only have a boolean to fill."""
    return status_for(last_seen_at, now=now)[0] == "online"
