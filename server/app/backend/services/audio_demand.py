"""Airband audio goes up the link only while somebody is listening.

Audio used to be the largest thing a station sends. Raw, it was: 24 kHz of
16-bit mono is 384 kbit/s, and base64 inside a JSON envelope made it
**512 kbit/s** for as long as an over lasted — on a metered Starlink link
shared with video, per transmitting station. It went up whenever the squelch
opened, whether or not a console existed to hear it.

**It is Opus now, and the number is 21.7 kbit/s** — measured, 400 ms of speech
at the encoder's default rate (`station/gsu/radio/opus.py:126-140`), inside the
contract's stated 16-24. Roughly 15x cheaper than the figure this file was
written against, and the correction matters in one direction: 512 kbit/s is
large enough to make guarding several channels at once look plainly
unaffordable, and 21.7 is not. A capacity argument built on the old number
would reject a feature the link can comfortably carry.

None of which retires the lease. Demand-driven is still right — an unattended
site should send nothing at all, and "cheap" is not "free" on a metered link
shared with video.

The spectrum has been demand-driven since it was written, for a cost two
orders of magnitude smaller (`radio/receiver.want_spectrum`: 241 floats at
1 Hz, "roughly 150 MB a day"). Audio was the one that mattered and the one
left open.

WHY A LEASE AND NOT AN EVENT
----------------------------
The obvious design is to send `radio.audio {on: true}` when a console
subscribes and `{on: false}` when it unsubscribes. It is wrong for the same
reason it was wrong for video: **most subscribers never say goodbye.** A
closed laptop, a dropped link, a revoked session, a crashed browser, a tab
shut while nothing was being sent — none produces an unsubscribe, and each one
leaves a station transmitting to nobody. `api/media.watch_for_close` exists
because exactly that happened with video.

So the platform re-asserts, and **silence is the stop signal**. The station's
lease expires on its own, which means audio stops when the platform goes away
rather than when it remembers to say so. That is the only version of this that
survives the platform crashing.

The renewal reads which stations have a live audio subscriber *on this
worker*. Per-worker is correct rather than a limitation: a listener on another
worker renews from there, and the station simply takes the latest lease it is
told about.
"""

import asyncio
import logging
import uuid

from backend.realtime.bus import command_channel, publish_sync
from backend.realtime.hub import hub

log = logging.getLogger(__name__)

#: What the station is told each request is worth. Matches the station's own
#: fallback (`radio/receiver.AUDIO_WINDOW_S`) so the two agree when a lease is
#: ever dropped from a command.
LEASE_SECONDS = 30

#: How often it is re-asserted. A third of the lease, so two renewals can be
#: lost — a Redis blip, a worker stalling on a slow query — before a listener
#: hears a gap. Video uses the same ratio.
RENEW_SECONDS = 10

#: The stream name in `hub.STREAM_CAPABILITY`, and the suffix of the group a
#: listening console joins.
STREAM = "audio"


def request(station_id: uuid.UUID, *, on: bool = True) -> None:
    """Ask one station to start or stop sending audio.

    Called on subscribe so the first over is not missed waiting for the next
    renewal tick, and by the loop below to keep it going. Idempotent at the
    station: a second listener extends the lease rather than starting a second
    anything, because there is one receiver and its audio is one stream.
    """
    command: dict = {"kind": "radio.audio", "on": on}
    if on:
        command["lease_seconds"] = LEASE_SECONDS
    if not publish_sync(command_channel(station_id), command):
        # Not an error worth failing a subscribe over: the renewal loop will
        # try again in RENEW_SECONDS, and a station that never hears us simply
        # sends no audio, which is the safe direction to fail in.
        log.warning("Could not reach station %s to ask for audio.", station_id)


async def renew() -> None:
    """Keep telling stations somebody is still listening."""
    while True:
        await asyncio.sleep(RENEW_SECONDS)
        try:
            for station_id in hub.groups.stations_subscribed_to(STREAM):
                request(station_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One bad iteration must not end the loop: that would leave every
            # listener's audio expiring in thirty seconds with nothing to say
            # why, which is far worse than a logged blip.
            log.exception("Audio lease renewal failed; continuing.")
