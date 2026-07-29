"""Commands in, effects out.

Two failures this file exists to prevent, both of which have actually happened
on this project:

**Parsing the channel wrongly.** The channel is `cmd/gsu/{station_id}` —
slash-separated. Getting that wrong produces a station that is subscribed,
receiving, and dropping everything, which from the outside is indistinguishable
from one that ignores its operator. So the topic is compared against the one
enrolment issued, and anything else is logged loudly rather than silently
skipped, including the specific case of a colon-separated channel, which is the
platform's *internal* naming and not this boundary's.

**Accepting a command and quietly doing nothing.** The platform confirms
nothing: it publishes and waits to see the change in the station's own telemetry
(`contract/transport.md`). Every handler here therefore changes state that the
next telemetry frame reports, and the dispatch logs what it applied — a station
whose logs say "applied light.set on" while its telemetry says `on: false` is a
hardware fault, and one that says neither is this bug.

Unknown commands are **ignored, not rejected**: an older station and a newer
platform have to coexist (`contract/schemas/command.schema.json`).
"""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger("gsu.commands")

Handler = Callable[[dict], str]


class CommandRouter:
    def __init__(self, command_topic: str, handlers: dict[str, Handler]) -> None:
        self.command_topic = command_topic
        self.handlers = handlers
        self.applied = 0
        self.ignored = 0
        self.last: str | None = None

    def dispatch(self, channel: str, payload: dict) -> bool:
        if not self.accepts(channel):
            return False
        kind = str(payload.get("kind", ""))
        handler = self.handlers.get(kind)
        if handler is None:
            # Not an error. A newer platform may know commands this station does
            # not, and the contract is explicit that it must not be rejected.
            self.ignored += 1
            log.info("Ignoring unknown command %r (station is older than the platform).", kind)
            return False
        try:
            effect = handler(payload)
        except Exception:  # noqa: BLE001 - one bad command must not stop the loop
            log.exception("Command %r failed to apply.", kind)
            return False
        self.applied += 1
        self.last = f"{kind}: {effect}"
        log.info("Applied %s -> %s", kind, effect)
        return True

    def accepts(self, channel: str) -> bool:
        if channel == self.command_topic:
            return True
        if ":" in channel and channel.replace(":", "/") == self.command_topic:
            log.error(
                "Command arrived on %r but this station's channel is %r. That is "
                "the platform's internal naming, not the station boundary; "
                "everything on it is being dropped.",
                channel, self.command_topic,
            )
            return False
        log.error(
            "Command arrived on an unrecognised channel %r; expected %r. Dropping it.",
            channel, self.command_topic,
        )
        return False


def build_handlers(radio, light, on_config, stream=None) -> dict[str, Handler]:
    """Wire the contract's commands to the things that carry them out.

    Every entry here has a matching field in a telemetry payload — that pairing
    is the contract's core promise, and the reason there is no command without
    an observable effect.

    A command for a device that is not fitted is **not** registered. It then
    falls through to the unknown-command path and is logged as ignored, which is
    the honest outcome: the platform sees no change in telemetry because nothing
    changed, and the station's log says why. Registering a handler that quietly
    does nothing would produce exactly the silence this file exists to prevent.
    """

    def tune(payload: dict) -> str:
        radio.tune(int(payload["freq_hz"]))
        return f"{radio.freq_hz / 1e6:.3f} MHz"          # -> radio.freq_hz

    def squelch(payload: dict) -> str:
        radio.set_squelch(float(payload["db"]))
        return f"threshold {radio.manual_threshold_db} dB, AUTO off"  # -> radio.threshold_db

    def auto_squelch(payload: dict) -> str:
        radio.set_auto_squelch(bool(payload["on"]))
        if radio.auto_squelch:
            return "AUTO on, riding the measured floor"   # -> radio.auto_squelch
        return f"AUTO off, frozen at {radio.manual_threshold_db:.1f} dB"

    def monitor(payload: dict) -> str:
        radio.set_monitor(bool(payload["on"]))
        return "gate held open" if radio.monitor else "gate released"  # -> radio.monitor

    def gain(payload: dict) -> str:
        radio.set_gain(payload["gain"])
        return f"gain {radio.gain}"                      # -> radio.gain

    def ppm(payload: dict) -> str:
        radio.set_ppm(int(payload["ppm"]))
        return f"ppm {radio.ppm}"                        # -> radio.ppm

    def light_set(payload: dict) -> str:
        light.request(bool(payload["on"]))
        # Reported as light.on only once the hardware has actually done it.
        return f"requested {'on' if payload['on'] else 'off'}"

    handlers: dict[str, Handler] = {}
    if radio is not None:
        handlers.update({
            "radio.tune": tune,
            "radio.squelch": squelch,
            "radio.auto_squelch": auto_squelch,
            "radio.monitor": monitor,
            "radio.gain": gain,
            "radio.ppm": ppm,
        })
    else:
        log.warning("No receiver fitted: radio commands will be ignored and logged.")
    if light is not None:
        handlers["light.set"] = light_set
    else:
        log.warning("No floodlight fitted: light.set will be ignored and logged.")

    def video_start(payload: dict) -> str:
        # Idempotent by construction: a second viewer extends the lease rather
        # than starting a second encoder, because there is one camera and the
        # second `rpicam-vid` fails with a device-busy that reads like broken
        # hardware. -> health.video.stream.state
        return stream.start(payload)

    def video_stop(payload: dict) -> str:
        return stream.stop(str(payload.get("reason") or "stopped by the platform"))

    if stream is not None:
        # Not in command.schema.json yet — proposed in CONTRACT-QUESTIONS.md
        # item 12, and the platform is building the other half now. Both are
        # handled here so the station is ready, and both report their actual
        # effect in `health.video.stream` rather than being assumed to have
        # worked: `video.start` on a station with no camera is a state of
        # `unavailable` and a reason, not silence.
        handlers["video.start"] = video_start
        handlers["video.stop"] = video_stop

    if on_config is not None:
        # Not in command.schema.json yet: `contract/enrolment.md` §7 describes
        # config.set and the platform lists it as still owed. Handled here so a
        # station is ready for it, and raised in CONTRACT-QUESTIONS.md rather
        # than being invented into the schema.
        handlers["config.set"] = on_config

    # radio.transmit is deliberately absent. It is ungrantable on the platform
    # and must not exist here until the fail-released design in
    # server/docs/05-radio-integration.md does. Arriving, it is ignored like any
    # other unknown command.
    return handlers
