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
    """Everything that arrives on the downward stream, dispatched by `kind`.

    There is no channel to check any more. Under contract 2.0 a station has one
    socket and one downward stream, and the platform already resolved whose
    commands these are from the credential before it sent them — so a command
    that arrives here is by construction this station's own.

    That deleted a whole class of fault rather than fixing it. The old router
    compared an inbound channel name against the one enrolment had handed out,
    and the interesting case was the near-miss: the platform's internal naming
    uses colons where the station boundary used slashes, and a station given
    the wrong form subscribed successfully, received everything, and dropped
    all of it — indistinguishable from a station ignoring its operator. There
    is now no name on the wire to get wrong.
    """

    def __init__(self, handlers: dict[str, Handler]) -> None:
        self.handlers = handlers
        self.applied = 0
        self.ignored = 0
        self.last: str | None = None

    def dispatch(self, payload: dict) -> bool:
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

def build_handlers(radio, light, on_config, stream=None,
                   events=None, updates=None, console_proxy=None,
                   host_shell=None, renew=None, poster=None) -> dict[str, Handler]:
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

    def events_ack(payload: dict) -> str:
        """`events.ack`: the platform has the batch; stop re-sending it.

        Registered unconditionally, unlike the device commands above, and the
        difference is deliberate. A missing device is a station that honestly
        cannot carry out an instruction, and falling through to the
        ignored-command path is the truthful answer. A missing event sender is
        this station not having finished starting up, and an acknowledgement
        dropped on that basis is a batch delivered for ever.
        """
        if events is None:
            return "no event sender yet"
        events.on_ack(payload.get("through_seq"))
        return f"through seq {payload.get('through_seq')}"

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

    def spectrum(payload: dict) -> str:
        """Ask for the spectrum, or stop asking.

        Demand-driven because 241 bins at 1 Hz is roughly 150 MB a day on a
        metered link, for a display that is open for minutes at commissioning.
        Re-requested periodically rather than held open, so a console that
        crashes stops the traffic without having to say goodbye.
        """
        on = bool(payload.get("on", True))
        radio.want_spectrum(on)
        return "spectrum on" if on else "spectrum off"

    def audio_start(payload: dict) -> str:
        """Somebody is listening. Leased, so silence stops it.

        Idempotent: a second listener replaces the lease rather than starting
        anything, because there is one receiver and its audio is one stream.
        Replaces, not extends — a shorter renewal shortens the lease, which is
        how the platform stops the audio sooner than the last one promised.
        """
        lease = payload.get("lease_seconds")
        radio.want_audio(True, None if lease is None else float(lease))
        return "audio on"

    def audio_stop(payload: dict) -> str:
        radio.want_audio(False)
        return "audio off"

    def ppm(payload: dict) -> str:
        radio.set_ppm(int(payload["ppm"]))
        return f"ppm {radio.ppm}"                        # -> radio.ppm

    def light_set(payload: dict) -> str:
        light.request(bool(payload["on"]))
        # Reported as light.on only once the hardware has actually done it.
        return f"requested {'on' if payload['on'] else 'off'}"

    def system_update(payload: dict) -> str:
        """`system.update`: record the target image the platform named, for the
        host-side updater to reconcile. The station never updates itself
        (DECISIONS.md item 48) — this only writes the request down, and reports
        progress through the running/desired version fields."""
        return updates.request(
            image=str(payload.get("image", "")),
            tag=str(payload.get("tag", "")),
            digest=str(payload.get("digest", "")),
            force=bool(payload.get("force", False)),
        )

    handlers: dict[str, Handler] = {"events.ack": events_ack}
    if radio is not None:
        handlers.update({
            "radio.tune": tune,
            "radio.squelch": squelch,
            "radio.auto_squelch": auto_squelch,
            "radio.monitor": monitor,
            "radio.gain": gain,
            "radio.ppm": ppm,
            "radio.spectrum": spectrum,
            "radio.audio": audio_start,
            "radio.audio_stop": audio_stop,
        })
    else:
        log.warning("No receiver fitted: radio commands will be ignored and logged.")
    if light is not None:
        handlers["light.set"] = light_set
    else:
        log.warning("No floodlight fitted: light.set will be ignored and logged.")

    def video_start(payload: dict) -> str:
        # Idempotent by construction: a second viewer replaces the lease rather
        # than starting a second encoder, because there is one camera and the
        # second `rpicam-vid` fails with a device-busy that reads like broken
        # hardware. Replaces, not extends: a shorter renewal shortens the lease.
        # -> health.video.stream.state
        return stream.start(payload)

    def video_stop(payload: dict) -> str:
        # on_platform_stop, not stop: on a box that keeps its encoder warm this
        # detaches the platform and leaves the encoder running, so the next
        # video.start re-attaches instantly. Elsewhere it is a full stop.
        return stream.on_platform_stop(
            str(payload.get("reason") or "stopped by the platform"))

    if stream is not None:
        # Both report their actual effect in `health.video.stream` rather than
        # being assumed to have worked: `video.start` on a station with no
        # camera is a state of `unavailable` and a reason, not silence.
        handlers["video.start"] = video_start
        handlers["video.stop"] = video_stop

    def poster_start(payload: dict) -> str:
        """`video.poster`: the wall would like a still every so often.

        Leased like audio and video, and REPLACING rather than extending for
        the same reason: a shorter renewal has to be able to shorten the lease,
        or the platform cannot change its mind faster than it once promised.

        This is the one leased command the station may refuse on its own
        authority — below `shed_poster_below_soc_pct` it stops capturing and
        says why in `health.video.poster.reason`. The platform asks; the site
        decides. Accepting here and refusing there is deliberate: the refusal
        is a live condition of the site, not a property of the request, and it
        can begin and end in the middle of a lease.
        """
        # -> health.video.poster.{leased,sent,refused,reason}
        return poster.request(
            payload.get("lease_seconds"), payload.get("interval_seconds"))

    def poster_stop(payload: dict) -> str:
        return poster.release(
            str(payload.get("reason") or "stopped by the platform"))

    if poster is not None:
        handlers["video.poster"] = poster_start
        handlers["video.poster_stop"] = poster_stop

    if on_config is not None:
        # Defined in command.schema.json and currently never sent — the
        # platform holds no site policy of its own (`contract/enrolment.md`
        # §7). Handled so a station is ready the day that changes.
        handlers["config.set"] = on_config

    if renew is not None:
        def config_refresh(payload: dict) -> str:
            # -> the credential renews (health.credential.expires_at moves) and the
            # box adopts the platform's current name and timezone from the fresh
            # enrolment record. Just wakes the renewer thread; the renewal runs
            # there, and the platform sees the effect in health, never here.
            return renew()

        handlers["config.refresh"] = config_refresh

    if updates is not None:
        # Recorded, never executed here: the host-side updater outside the
        # sandbox does the privileged work (DECISIONS.md item 48). Registered
        # only when an updater coordinator exists, like the device handlers.
        handlers["system.update"] = system_update

    def console_open(payload: dict) -> str:
        # -> the console socket opening, which the platform observes directly
        # (its /console/ingest sees the connection). Refused unless the box has
        # opted in — the proxy itself enforces that, so a station without
        # GSU_CONSOLE_PROXY answers here with a refusal rather than a socket.
        return console_proxy.open(payload.get("lease_seconds"))

    def console_close(payload: dict) -> str:
        return console_proxy.close(
            str(payload.get("reason") or "closed by the platform"))

    if console_proxy is not None:
        # Registered only when the proxy exists — like the device handlers, a
        # station that cannot carry this instruction lets it fall through to the
        # ignored-command path rather than accepting it and doing nothing.
        handlers["console.open"] = console_open
        handlers["console.close"] = console_close

    def host_open(payload: dict) -> str:
        # -> a request file the privileged helper acts on. Refused unless the box
        # has opted in (GSU_HOST_SHELL); the coordinator enforces that, so an
        # opted-out station answers with a refusal rather than a host session.
        return host_shell.request_open(payload.get("lease_seconds"))

    def host_close(payload: dict) -> str:
        return host_shell.request_close(
            str(payload.get("reason") or "closed by the platform"))

    if host_shell is not None:
        handlers["host.open"] = host_open
        handlers["host.close"] = host_close

    # radio.transmit is deliberately absent. It is ungrantable on the platform
    # and must not exist here until the fail-released design in
    # server/docs/05-radio-integration.md does. Arriving, it is ignored like any
    # other unknown command.
    return handlers
