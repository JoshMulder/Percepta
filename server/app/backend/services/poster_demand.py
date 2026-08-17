"""A station takes a picture for the wall only while a wall is showing it.

WHY STILLS EXIST AT ALL. ODIN's grid shows every station at once, and a tile
with no picture is a row of numbers about a place nobody can see. Live video
cannot answer that at fleet scale: one stream is ~2.6 Mbit/s through the
in-process relay in `api/media.py`, which is the thing that binds this
deployment to a single worker, and two dozen of them is not a feature but a
different system. A scaled JPEG a minute is ~2.75 kbit/s and lives in Redis,
where any worker can serve it.

WHY A LEASE AND NOT A SWITCH. The same reason audio and video have one, and
`services/audio_demand.py` argues it at length: **most consumers never say
goodbye.** A closed laptop, a dropped link, a revoked session, a crashed
browser — none of those produces an unsubscribe, and every one of them would
otherwise leave a field station opening its camera once a minute, for ever, for
nobody. So the platform re-asserts and silence is the stop signal. That is the
only version of this that survives the platform crashing.

AND THE STATION MAY STILL SAY NO. Unlike audio, this command is one a station
is expected to refuse on its own authority: a capture is an RTSP handshake, a
decode and a JPEG encode, and standing load is what browns out a solar site.
The station declines below its own state-of-charge floor and reports the
refusal in `health.video.poster.reason`. A refusing station is not a broken
one, and nothing here should treat it as one — the platform asks; the site
decides.

The renewal reads which stations a wall is showing *on this worker*, exactly as
audio's does. Per-worker is correct rather than a limitation: a wall on another
worker renews from there, and the station takes the latest lease it is told
about.
"""

import asyncio
import logging
import uuid

from backend.realtime.bus import command_channel, publish_sync
from backend.realtime.hub import POSTER_STREAM, hub

log = logging.getLogger(__name__)

#: How often a still is asked for. Sixty seconds is the wall's own rhythm: long
#: enough that the picture costs a fiftieth of what squelch-gated Opus does,
#: short enough that a tile is never showing something that has stopped being
#: true. The station floors it at its own minimum and may serve faster if a
#: technician on the setup page is already driving the camera harder — one
#: capture serves every watcher.
INTERVAL_SECONDS = 60

#: What each request is worth. THREE INTERVALS, not one: a lease that expired
#: between captures would make every single capture a cold start against a
#: camera that had just been let go, and a wall would spend its life watching
#: cameras reconnect. Matches the station's own fallback
#: (`gsu/poster.DEFAULT_LEASE_S`) so the two agree when a lease is ever dropped
#: from a command.
LEASE_SECONDS = 180

#: How often it is re-asserted. A third of the lease, so two renewals can be
#: lost — a Redis blip, a worker stalling on a slow query — before a tile goes
#: stale. Audio and video use the same ratio.
RENEW_SECONDS = 60


def request(station_id: uuid.UUID, *, on: bool = True) -> None:
    """Ask one station to start or stop sending stills.

    Called when a wall first shows a station, so its tile is not blank for the
    first renewal period, and by the loop below to keep it going. Idempotent at
    the station: a second wall renews the lease rather than starting a second
    anything, because there is one camera and its picture is one picture.
    """
    command: dict = (
        {
            "kind": "video.poster",
            "lease_seconds": LEASE_SECONDS,
            "interval_seconds": INTERVAL_SECONDS,
        }
        if on
        else {"kind": "video.poster_stop"}
    )
    if not publish_sync(command_channel(station_id), command):
        # Not worth failing a wall render over: the renewal loop tries again in
        # RENEW_SECONDS, and a station that never hears us simply sends no
        # pictures, which is the safe direction to fail in.
        log.warning("Could not reach station %s to ask for posters.", station_id)


async def renew() -> None:
    """Keep telling stations that a wall is still showing them."""
    while True:
        await asyncio.sleep(RENEW_SECONDS)
        try:
            for station_id in hub.groups.stations_subscribed_to(POSTER_STREAM):
                request(station_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One bad iteration must not end the loop: that would leave every
            # tile on every wall expiring three minutes later with nothing to
            # say why, which is far worse than a logged blip.
            log.exception("Poster lease renewal failed; continuing.")
