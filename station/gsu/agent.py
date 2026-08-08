"""The loop. Everything else in this package is something it drives.

The shape is dictated by one sentence in `station/README.md`: *nothing the
station needs to do correctly may require the platform to be reachable.* So the
loop runs whether or not the box is enrolled, whether or not the broker answers,
and whether or not anything it publishes is heard. Sensing, recording, local
alerting and duty cycling all happen above the transport and never ask it a
question. Publishing is the last thing each tick does, and its failure is a
counter, not an exception.

Cadence is `contract/transport.md`: adsb, power, radio and light at 1 Hz,
weather at 0.2 Hz, audio only while the squelch is open. Nothing is queued —
telemetry is current state, and a frame that missed its moment is worth less
than the one a second behind it.

**A stream with no working device is not published at all.** Not an empty
array, not a zero. An empty ADS-B frame means "clear airspace" and a weather
frame full of defaults means "measured"; both are lies a console cannot detect.
What is missing, and why, goes out in the health frame and shows on the local
console instead.
"""

from __future__ import annotations

import base64
import logging
import queue
import signal
import threading
import time
from datetime import UTC, datetime

from . import AGENT_VERSION, clock, tls
from .camera.ownership import SensorLease
from .commands import CommandRouter, build_handlers
from .config import AgentConfig, SiteConfig
from .events import EventSender
from .credentials import CredentialStore, Enrolment
from .devices.inventory import Inventory
from .enrolment import EnrolmentClient, Renewer
from .health import Health
from .radio.audio import AUDIO_RATE
from .radio.receiver import RadioController
from .radio.transcribe import Transcriber
from .store import LocalStore, TRANSCRIPT_KIND
from .update import UpdateCoordinator
from .stream import StreamSession
from .transport import (
    AUDIO, CONTRACT_VERSION, EVENTS, TELEMETRY,
    Transport, build_transport, redact_url,
)
from .video import CameraPreview

log = logging.getLogger("gsu.agent")

#: Hardware inventory sent at enrolment. Explicitly not trust — nothing here
#: influences what the station may do — but it is what an admin sees in the
#: fleet list, so it says plainly what this box is.
HARDWARE = {
    "model": "percepta-gsu-agent",
    "os": "linux",
    "agent_version": AGENT_VERSION,
}

PRUNE_EVERY_SECONDS = 300.0

#: Command kinds that run off the sensing thread, on a serial worker. They are
#: slow — a stream start probes the camera (up to ~15 s) and a stop reaps ffmpeg
#: — so running them inline on the sensing loop would stall the 125 ms audio
#: sub-tick, the same class of freeze the video-teardown fix removed. They do
#: not touch the radio front end, so they need no serialisation with the sensing
#: loop, only to be off the reader thread (a slow one must not stall the socket)
#: and serial among themselves (a start and a stop must not race one another).
#: Every other command runs on the sensing thread, where the front end has one
#: owner. See `_on_command`, `_drain_commands`, `_run_command_worker`.
SLOW_COMMANDS = frozenset({"video.start", "video.stop"})

#: How a stream with no source is described to an operator. Short, in their
#: terms, and never a parser's business — the structured version of the same
#: fact is in the health payload's device inventory.
NO_SOURCE = {
    "adsb": "no ADS-B receiver connected",
    "radio": "no airband receiver connected",
    "weather": "no weather station connected",
    "power": "no charge controller connected",
    "light": "no floodlight fitted",
    "video": "no camera fitted",
}

#: `unavailable_reason` is capped by the schema.
REASON_LIMIT = 200

#: How often the device set is rebuilt when something is missing. A USB-UART
#: that was unplugged at boot and plugged in afterwards should come good on its
#: own: nobody is there to restart anything.
REDISCOVER_SECONDS = 30.0

#: How often the receiver is read between full sensor sweeps.
#:
#: Audio is a stream and everything else on this loop is a reading, so the two
#: do not want the same cadence. At the sweep's one second the receiver was
#: handed a whole second to demodulate at once — a second of latency before a
#: syllable could leave the box, and the console's prebuffer then sizes itself
#: from the chunk it receives, so that one-second chunk cost another 1.25 on
#: top. Squelch opening and audio arriving two seconds later is not a radio.
#:
#: 125 ms rather than smaller: below about 100 ms the per-chunk overhead — a
#: payload, a base64 encode, a broker publish — starts to matter more than the
#: latency it saves, and 8 Hz is already inside what anybody hears as instant.
AUDIO_TICK_S = 0.125

#: How often the spectrum goes out *while somebody is watching it*. The levels
#: on the radio frame stay on the one-second sweep — a signal reading does not
#: need to move faster — but the spectrum is a live picture the operator is
#: watching change, and once a second it steps rather than moves. The front end
#: is already measured every AUDIO_TICK_S, so this only decides how often that
#: measurement is *published*; it is bounded by the demand window, so a station
#: nobody is watching still sends the spectrum only on the sweep, or not at all.
#: 200 ms (~5 Hz) reads as live without being the 8 Hz the audio needs.
SPECTRUM_TICK_S = 0.2

#: How long after the floodlight's commanded state changes before the measured
#: current is allowed to contradict it. A contactor takes a moment to move and
#: some lamps draw oddly while striking; judging inside that window turns
#: every ordinary switch into a fault. Longer than any plausible actuation,
#: shorter than anyone watching a dead lamp would wait.
LIGHT_SETTLE_SECONDS = 3.0


class Agent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        config.ensure_home()

        self.health = Health()
        self.site = SiteConfig.load(config.site_config_path)
        self.store = LocalStore(config.store_path, config.recordings_dir)
        self.updates = UpdateCoordinator(config.version, config.update_dir)
        # Airband transcription, off unless configured and the binary and model
        # are both present. Reads captured overs on a low-priority thread; live
        # audio always wins. See gsu/radio/transcribe.py.
        self.transcriber = Transcriber(
            self._record_transcript,
            binary=config.radio_whisper_bin,
            model=config.radio_whisper_model,
            prompt=config.radio_whisper_prompt,
            # The env override or the site toggle; the site one is the setup
            # page's switch and is re-read live in `_pump_radio`.
            enabled=config.radio_transcribe or self.site.radio_transcribe,
        )
        #: The over currently being spoken, accumulated while the squelch is open
        #: and handed to the transcriber when it closes. Only used when
        #: transcription is available, so a station without it buffers nothing.
        self._over_pcm = bytearray()
        self._over_open = False
        self._over_started_at: datetime | None = None
        self._over_freq_hz = 0
        self.credentials = CredentialStore(config.credential_path)
        self.ca = tls.CaStore(config.ca_path)
        # Two roots, deliberately. The broker is pinned to a private CA; the
        # API is verified against the system bundle unless told otherwise,
        # because it is expected behind a proxy with a public certificate.
        self.trust = self._resolve_broker_trust()
        self.api_trust = self._resolve_api_trust()
        self.client = EnrolmentClient(config.platform_url, trust=self.api_trust)
        self.inventory = Inventory(config.devices_path, demo=config.demo)

        self.enrolment: Enrolment | None = None
        self.transport: Transport | None = None
        self.router: CommandRouter | None = None
        self.events: EventSender | None = None
        self.renewer: Renewer | None = None

        # Devices exist before enrolment does. A box waiting for a technician to
        # type a code is still a box on a hillside with sensors on it.
        self.adsb = None
        self.weather = None
        self.power = None
        self.light = None
        self.camera = None
        self.radio: RadioController | None = None
        self._last_discovery = 0.0
        #: Set when a rebuild skipped the camera because the live stream held
        #: the sensor. Without it the deferral was permanent: the only other
        #: thing that rebuilds is rediscovery, which asks whether anything is
        #: *missing*, and the camera it declined to replace is present and
        #: working. See `build_devices` and the loop in `run`.
        self._camera_rebuild_owed = False
        #: The newest radio telemetry a pump produced, waiting for the next
        #: full sweep to publish it. See `_pump_radio`.
        self._radio_telemetry: dict | None = None
        #: Whether a sub-tick has read the receiver since the last sweep. The
        #: front end is a single device with a single reader, so exactly one of
        #: the two callers may read it in any given interval.
        self._radio_pumped = False
        #: The gate state the last published `radio` frame carried, as
        #: (squelch_open, monitor). None until one has gone out. Compared
        #: against on every sub-tick so an edge does not wait for the sweep —
        #: see `_pump_radio`.
        self._radio_gate: tuple[bool, bool] | None = None
        #: Monotonic time the spectrum last went out on a sub-tick, so a watched
        #: spectrum streams at SPECTRUM_TICK_S rather than on every 125 ms audio
        #: sub-tick. Zero until the first one. See `_sleep_pumping_radio`.
        self._spectrum_pub_at = 0.0
        #: Monotonic time of the last receiver read, held across sweeps so the
        #: audio timeline is the wall clock rather than the sum of the
        #: intervals we intended to sleep for. None until the first pump.
        #: See `_sleep_pumping_radio` for why the difference is audible.
        self._audio_clock: float | None = None
        # Who owns the camera, for the life of this process. Built before the
        # devices are, handed to every camera driver through device_context(),
        # and deliberately NOT rebuilt by rediscovery: an arbiter that is
        # replaced along with the thing it arbitrates cannot tell a successor
        # from a zombie, which is the bug it exists to make impossible. See
        # camera/ownership.py.
        self.sensor_lease = SensorLease("camera")
        self.build_devices()

        # Two consumers of the camera, and exactly one of them may hold the
        # sensor at a time — the lease above decides, not a convention:
        #
        #   video    the setup page's preview. Captures a single frame, on
        #            demand, only while somebody has the page open, and
        #            publishes nothing anywhere. It is called `video` because
        #            gsu/console.py reads it under that name.
        #   stream   H.264/HEVC, several Mbit/s, started only while somebody is
        #            actually watching, and the only consumer on an unattended
        #            box.
        #
        # The periodic snapshot channel that used to live here is gone: two
        # readers of one sensor was the camera wedge, and the platform has the
        # media channel for live video. See gsu/video.py.
        self.video = CameraPreview(self)
        self.stream = StreamSession(self)

        self._attach_lock = threading.Lock()
        self._stop = threading.Event()
        self._lock_handle = None
        #: Commands arrive on the relay's reader thread but must not run there.
        #: A radio retune racing the sensing thread's demodulate() corrupts the
        #: filter state, and a slow handler blocks the socket into a self-
        #: inflicted reconnect. So the reader only enqueues (`_on_command`):
        #: device commands drain on the sensing thread (`_drain_commands`, which
        #: gives the front end one owner), and slow ones on a worker thread
        #: (`_run_command_worker`). See SLOW_COMMANDS.
        self._tick_commands: queue.Queue[dict] = queue.Queue()
        self._slow_commands: queue.Queue[dict] = queue.Queue()
        self._command_worker: threading.Thread | None = None

        # Alert edge state. Alerts are edge-triggered because an operator wants
        # to know that something happened, not to be told 3600 times an hour
        # that it is still happening.
        self._alerting_icao: set[str] = set()
        self._battery_state = "ok"
        # Floodlight fault-check state: the commanded state last seen, how
        # long it has held (so a transition is judged only once settled), and
        # which fault is currently declared (so events record edges, not
        # every tick of a persisting fault).
        self._light_commanded: bool | None = None
        self._light_settled_s = 0.0
        self._light_fault: str | None = None
        self._link_up: bool | None = None
        self._offline_since: float | None = None
        self._last_prune = 0.0
        self._published = 0
        self._started = time.monotonic()
        self._credential_mtime: float | None = None

    # --- trust ----------------------------------------------------------

    def _resolve_broker_trust(self, stated_mode: str | None = None) -> tls.Trust:
        """What the broker is verified against: a pinned CA, or the public
        roots when the platform has said it is behind a public certificate.

        A trust root that cannot be read is a fault to *report*, never a reason
        to proceed without one: the fallback is "no CA", which refuses every
        TLS URL, and never "no verification". The station keeps sensing and
        recording either way — that is the whole design — it simply does not
        talk to anything it cannot identify.

        `stated_mode` is `broker.ca_mode` from the enrolment response and is
        None until one has been loaded, which is why this is re-resolved when
        an enrolment arrives rather than only at construction.
        """
        try:
            trust = tls.resolve_broker(
                self.ca,
                installed=self.config.ca_file,
                require_tls=self.config.require_tls,
                stated_mode=stated_mode,
            )
        except tls.Refusal as exc:
            self.health.raise_condition("tls.broker_trust_unusable", "critical", str(exc))
            log.error("%s", exc)
            return tls.Trust(require_tls=self.config.require_tls, purpose="broker")
        log.info("Broker TLS trust: %s.", trust.describe())
        return trust

    def _resolve_api_trust(self) -> tls.Trust:
        """What the platform API is verified against.

        The system CA bundle by default — the API is expected behind a
        TLS-terminating proxy with a public certificate, and the public trust
        store is the right tool for one. Pinned only when `GSU_API_CA_FILE`
        says so, which is the correct setting while the platform serves its own
        certificate.
        """
        try:
            trust = tls.resolve_api(
                installed=self.config.api_ca_file,
                require_tls=self.config.require_tls,
            )
        except tls.Refusal as exc:
            # Deliberately not a silent fall back to the system store: the
            # operator asked for pinning and got a broken file, and quietly
            # doing something weaker than they asked for is the whole failure
            # mode this module exists to prevent.
            self.health.raise_condition("tls.api_trust_unusable", "critical", str(exc))
            log.error("%s", exc)
            return tls.Trust(mode=tls.TRUST_PINNED, require_tls=self.config.require_tls,
                             purpose="api")
        log.info("Platform API TLS trust: %s.", trust.describe())
        return trust

    def _persist_ca(self, enrolment: Enrolment) -> None:
        """Keep the **broker's** CA from the enrolment response, and pin to it.

        `contract/enrolment.md` §4 calls `broker.ca_pem` pinned, which only
        means anything if it is stored: a CA re-fetched over an unverified
        channel every boot is pinned to nothing. A CA that *changes* is either a
        planned rotation or somebody else's certificate, and from here those
        look identical — so it is accepted (the response that carried it was
        itself verified) and said out loud.

        This never touches the API's trust root. That one is configured locally
        and is not something the platform gets to change by sending a field.
        """
        pem = enrolment.broker.ca_pem
        mode = enrolment.broker.ca_mode
        if mode == tls.TRUST_SYSTEM:
            # The platform is behind a publicly trusted certificate and has
            # said so. Any CA stored from an earlier enrolment is now a pin to
            # something that will not be presented again, so it is dropped
            # rather than left to win the precedence order and refuse every
            # connection — which is the failure this whole path exists to end.
            # An installed GSU_CA_FILE is untouched: that was put there
            # deliberately and out of band, and outranks anything said here.
            if self.ca.load() is not None:
                log.warning(
                    "The platform now serves a publicly trusted certificate, so "
                    "the CA pinned at %s no longer applies and has been "
                    "dropped. The broker is verified against the system trust "
                    "store from here.", self.config.ca_path,
                )
                self.ca.clear()
                # Recorded, not merely logged — the same as a CA rotation below.
                # Dropping a pin is a real reduction in the broker's trust
                # (from one CA to the whole public bundle), and the console now
                # shows the result as a plain green "public certificate" row, so
                # the transition itself needs a trace an operator can find.
                self.store.record_event(
                    "tls.ca_dropped", "warning",
                    "Broker CA pin dropped: the platform now states a public "
                    "certificate, so the broker is verified against the system "
                    "trust store from here.",
                )
            self.trust = self._resolve_broker_trust(stated_mode=mode)
            return
        if not pem:
            self.trust = self._resolve_broker_trust(stated_mode=mode)
            return
        # Persisted even when an installed CA is present and takes precedence:
        # if the installed file is ever removed, the box should still be pinned
        # to something rather than falling back to trusting anything.
        try:
            changed = self.ca.save(pem)
        except (ValueError, OSError) as exc:
            self.health.raise_condition(
                "tls.ca_unwritable", "critical",
                f"The platform sent a CA that could not be stored at "
                f"{self.config.ca_path}: {exc}",
            )
            return
        self.health.clear("tls.ca_unwritable")
        if changed:
            log.warning(
                "The platform's CA changed (now SHA-256 %s). Pinning to the new "
                "one; if this was not a planned rotation, it is worth asking why.",
                tls.fingerprint(pem),
            )
            self.store.record_event(
                "tls.ca_rotated", "warning",
                f"Pinned CA replaced; SHA-256 {tls.fingerprint(pem)}.",
            )
        # Re-resolve so the next transport uses it. The API client keeps its own
        # trust: one CA arriving in a response must not silently become the root
        # for the channel that delivered it.
        self.trust = self._resolve_broker_trust(stated_mode=mode)

    # --- devices --------------------------------------------------------

    def effective_elevation_m(self) -> float | None:
        """The station's elevation: local configuration first, then enrolment.

        Local first, to match `effective_position` — the two were inconsistent,
        position preferring the value set at the box and elevation preferring
        the one from enrolment, which meant setting a position locally moved the
        coordinates but not the height they were referenced against. The person
        at the mast is the authority on both, so both take the local value when
        there is one and fall back to what the platform issued when there is
        not. Recorded and reported to the platform, but not otherwise used.
        """
        if self.site.elevation_m is not None:
            return self.site.elevation_m
        site = self.enrolment.site if self.enrolment else None
        return site.elevation_m if site is not None else None

    def effective_position(self) -> tuple[float | None, float | None, str]:
        """Where this station is, and on whose word.

        **The station's own configuration wins, then the enrolment.** The
        position set on this box, by somebody standing at it, is preferred over
        whatever the platform issued — because the person at the mast is the one
        who knows, and because the setup page now lets them set it. The
        enrolment value is the fallback for a box that has never been told
        locally, which is the normal state right after a code is redeemed.

        This used to be the other way round in spirit — the position was frozen
        at enrolment and the setup page showed it read-only — on the reasoning
        that a box which has moved is recommissioned. That left an enrolled box
        with no way to be given a position at all, so the read-only rule is
        gone. Two editable copies of one fact is still a hazard, which is why
        the platform's copy is a fallback and reference rather than a second
        master.

        Returns `(None, None, "")` when nobody has said. Unset is a state, and
        it is reported as absent rather than as a plausible-looking default.
        """
        if self.site.latitude is not None and self.site.longitude is not None:
            return self.site.latitude, self.site.longitude, "station"
        site = self.enrolment.site if self.enrolment else None
        if site and site.latitude is not None and site.longitude is not None:
            return site.latitude, site.longitude, "enrolment"
        return None, None, ""

    def position_state(self) -> dict:
        """The position, its source, and the two inputs kept apart.

        Configured and effective are separate here for the same reason the
        device report keeps intent and detection separate: the setup page has
        to show what this station was told to be while also showing what it is
        actually using, and merging them is how a station ends up reporting a
        position nobody at the site ever confirmed.
        """
        latitude, longitude, source = self.effective_position()
        site = self.enrolment.site if self.enrolment else None
        return {
            "source": source,
            "latitude": latitude,
            "longitude": longitude,
            # The platform's words for the coordinates it issued. A pair of
            # decimals cannot be checked against the view out of the door;
            # "Timaru District, Canterbury" can.
            "locality": site.locality if site else None,
            "organization": site.organization if site else None,
            "elevation_m": self.effective_elevation_m(),
            "station": {
                "latitude": self.site.latitude,
                "longitude": self.site.longitude,
                "elevation_m": self.site.elevation_m,
                "radio_transcribe": self.site.radio_transcribe,
                "transcript_retention_days": self.site.transcript_retention_days,
                # So the setup page can show "installed / switched off" apart
                # from "not installed", and why.
                "transcribe_installed": self.transcriber.installed,
                "transcribe_reason": self.transcriber.install_reason,
            },
            "platform": {
                "latitude": site.latitude if site else None,
                "longitude": site.longitude if site else None,
            },
        }

    def reported_position(self) -> dict | None:
        """What goes up to the platform, or nothing.

        **Only ever this station's own configuration.** Echoing the platform's
        own value back at it as though the station had confirmed it would
        launder a guess into a measurement, and the platform would have no way
        to tell that nobody had ever been to the site.
        """
        if self.site.latitude is None or self.site.longitude is None:
            return None
        position = {
            "latitude": self.site.latitude,
            "longitude": self.site.longitude,
            # "configured" and not "gps": nothing on this box surveys itself
            # yet, and the platform must be able to tell a typed position from
            # a fixed one the day a GPS is fitted (CONTRACT-QUESTIONS.md 16).
            "source": "configured",
        }
        elevation = self.effective_elevation_m()
        if elevation is not None:
            position["elevation_m"] = elevation
        return position

    def apply_position(self) -> None:
        """Push the effective position into the drivers that compute from it.

        Called on attach and again whenever the setup page saves, so that an
        installer who corrects a position sees range and bearing change without
        restarting anything.
        """
        latitude, longitude, _ = self.effective_position()
        if latitude is None or longitude is None:
            return
        for driver in (self.adsb, self.weather):
            set_site = getattr(driver, "set_site", None)
            if set_site:
                set_site(latitude, longitude)

    def set_location(self, latitude, longitude, elevation_m) -> None:
        """Store what the setup page was given, and act on it immediately.

        Values are already parsed and range-checked by the caller
        (`config.parse_latitude` and friends); `None` for all three positions
        is a deliberate clear, which is how a station wrongly positioned during
        commissioning stops asserting a position it does not have.
        """
        self.site.latitude = latitude
        self.site.longitude = longitude
        self.site.elevation_m = elevation_m
        self.site.save(self.config.site_config_path)
        self.apply_position()

    def device_context(self) -> dict:
        site = self.enrolment.site if self.enrolment else None
        latitude, longitude, _ = self.effective_position()
        return {
            # The last-resort pair is a *simulation* origin, not a claim about
            # this station: it is what the synthetic ADS-B source needs to put
            # contacts somewhere before anyone has said where here is. Nothing
            # reported to the platform ever falls back to it — see
            # `reported_position`, which returns nothing instead.
            "latitude": latitude if latitude is not None else -43.5,
            "longitude": longitude if longitude is not None else 172.6,
            "timezone": site.timezone if site else "UTC",
            # The synthetic camera writes this on its test card, so that a
            # console showing two demo stations shows which is which.
            "station_name": site.name if site else "",
            # Handed to whichever camera driver is built. `_instantiate`
            # filters by constructor signature, so a driver that owns no local
            # sensor (the RTSP one) simply does not accept it and never asks
            # who owns what — which is the right answer for a camera that
            # encodes on the far end.
            "sensor_lease": self.sensor_lease,
            "alert_range_km": self.site.alert_range_km,
            "alert_altitude_m": self.site.alert_altitude_m,
            "traffic": self.config.airband_traffic,
        }

    def build_devices(self, force_camera: bool = False) -> None:
        """Construct whatever the inventory says is fitted, and record why
        anything else is missing. Never substitutes a simulation for hardware
        that did not answer.

        `force_camera` rebuilds the camera slot even while the live stream holds
        it, for the one caller that has decided the slot must change *now*: an
        operator saving a new camera on the setup page. Rediscovery never sets
        it — an automatic pass must not tear a working stream down — but a
        deliberate change must not be deferrable, because the thing that would
        discharge a deferral is the stream ending, and a platform viewer that
        keeps watching (its player reconnects within a second of any drop) keeps
        the stream up for ever. That is precisely the "watches the demo until
        somebody restarts the box" this used to warn about, arriving by way of
        the viewer rather than the deferral never being asked.

        Two rules here, both bought with a wedged station:

        **The outgoing driver is retired, not merely closed.** A retired driver
        never captures again. This used to be load-bearing against a much
        nastier failure — the old driver held a live picamera2 handle and
        reopened it lazily, so an abandoned instance could reacquire the sensor
        *after* its replacement was built and leak the acquisition for the life
        of the process. That driver is gone with the Pi 2B, and the
        sensor lease now refuses a stale holder's release outright, so retiring
        is no longer the only thing standing between the station and a wedge.
        It is kept because a capture already in flight on the outgoing instance
        should not spend a lease its successor is waiting for.

        **The camera slot is not touched while the live stream has the
        sensor.** A rebuild mid-stream is guaranteed contention — and worse,
        the trigger used to be circular: snapshots failed *because* the encoder
        held the sensor, the slot reported failed, and rediscovery then treated
        the one working consumer of the camera as a broken camera. The slot is
        rebuilt on the first pass after the stream ends — which needs
        `_camera_rebuild_owed` to be remembered, because nothing else would ever
        ask. Rediscovery only fires for a slot that is *missing*, and the camera
        this declined to replace is present and working: it is the demo one, and
        an operator who has just pointed the station at an RTSP URL watches it
        go on showing the demo until somebody restarts the box.
        """
        context = self.device_context()
        self._last_discovery = time.monotonic()
        keep_camera = self._stream_holds_camera() and not force_camera
        # Set when this pass leaves the camera alone, cleared when it builds
        # one. Assigned rather than or-ed: a rebuild that did the camera has
        # discharged the debt however it was incurred.
        self._camera_rebuild_owed = keep_camera

        if self.radio is not None:
            try:
                self.radio.shutdown()
            except Exception:  # noqa: BLE001
                pass
        for slot, driver in list(self.inventory.drivers.items()):
            if slot == "radio":
                continue  # its front end was just shut down through the controller
            if slot == "camera" and keep_camera:
                continue
            self._retire_driver(driver)

        self.adsb = self.inventory.build("adsb", context)
        self.weather = self.inventory.build("weather", context)
        self.power = self.inventory.build("power", context)
        self.light = self.inventory.build("light", context)
        front_end = self.inventory.build("radio", context)
        self.radio = (
            RadioController(front_end, state_path=self.config.receiver_state_path)
            if front_end is not None else None
        )
        # Constructed here and read from the preview thread. Constructing it
        # must stay cheap for that reason — a network camera opens nothing and
        # runs no subprocess until the first capture, which happens off this
        # loop. The context carries the sensor lease, so the new driver
        # contends with the outgoing one through the same arbiter rather than
        # through luck.
        if not keep_camera:
            self.camera = self.inventory.build("camera", context)
            # Forget the outgoing camera's last picture. The preview keeps a
            # stale frame on purpose - a picture with a stated age beats a
            # blank box while a camera is merely struggling - but that
            # reasoning stops at the device staying the same. After a swap it
            # shows the OLD camera's view under the NEW camera's name, which
            # is how somebody points a station at an RTSP URL, sees the
            # previous sensor's test card, and concludes it worked.
            preview = getattr(self, "video", None)
            if preview is not None:
                preview.last_frame = None
                # And the outgoing camera's excuse with it, for the same
                # reason one line up. "the camera did not deliver a frame
                # within 15s (rtsp://…)" is a true sentence about a driver
                # that no longer exists, and leaving it set attributes it to
                # whatever was just fitted — so a demo camera that is starting
                # up perfectly well is introduced by the failure of the RTSP
                # camera it replaced.
                preview.last_reason = ""

        self._log_unconfigured = True

        if self.router is not None:
            self.router.handlers = build_handlers(
                self.radio, self.light, self._apply_config,
                getattr(self, "stream", None), updates=self.updates,
            )

        self._report_capabilities()

    def _report_capabilities(self) -> None:
        missing = [
            report for report in self.inventory.report()
            if report.configured and report.status != "present"
        ]
        unconfigured = [
            report for report in self.inventory.report() if not report.configured
        ]
        if missing:
            self.health.raise_condition(
                "devices.absent", "warning",
                "; ".join(f"{report.slot}: {report.detail}" for report in missing),
            )
        else:
            self.health.clear("devices.absent")

        unsourced = self.inventory.unsourced_streams()
        if unsourced:
            self.health.raise_condition(
                "telemetry.unsourced", "warning",
                "No source for: " + ", ".join(sorted(unsourced))
                + ". Those streams are not published at all rather than "
                  "published empty.",
            )
        else:
            self.health.clear("telemetry.unsourced")

        conflicts = self.inventory.conflicts()
        if conflicts:
            self.health.raise_condition("devices.conflict", "critical", "; ".join(conflicts))
            for conflict in conflicts:
                log.error("Device allocation: %s", conflict)
        else:
            self.health.clear("devices.conflict")

        # Said once per rebuild, not once per caller: this method also runs on
        # every health frame and every status.json poll — which the Devices
        # page now makes every 2.5 seconds — and an empty slot is not news
        # that often.
        if getattr(self, "_log_unconfigured", False):
            self._log_unconfigured = False
            for report in unconfigured:
                log.info("Slot %s: nothing fitted.", report.slot)

    # --- enrolment ------------------------------------------------------

    def load_enrolment(self) -> Enrolment | None:
        try:
            enrolment = self.credentials.load()
        except ValueError as exc:
            self.health.raise_condition("enrolment.unreadable", "critical", str(exc))
            log.error("%s", exc)
            return None
        if enrolment is not None:
            self._attach(enrolment)
        return enrolment

    def enrol(self, token: str) -> Enrolment:
        """Claim a code. Raises with a message meant for a technician.

        Resumable by construction: this can be called again after a dropped
        connection, and the platform re-issues rather than refusing
        (`contract/enrolment.md` §4) — the failure that matters is a technician
        stuck on a hillside with a used code.
        """
        enrolment = self.client.claim(token, HARDWARE)
        self.credentials.save(enrolment)
        self.health.clear("enrolment.missing")
        self.health.clear("enrolment.unreadable")
        self.store.record_event(
            "enrolment.claimed", "info",
            f"Enrolled as {enrolment.site.name} ({enrolment.station_id}).",
        )
        self._attach(enrolment)
        return enrolment

    def _attach(self, enrolment: Enrolment) -> None:
        """Take up an identity: connect, subscribe, and start renewing."""
        with self._attach_lock:
            if self.transport is not None:
                self.transport.stop()
            self.enrolment = enrolment

            self._persist_ca(enrolment)

            # The platform states its own address, which on a development stack
            # is frequently only routable from inside it. The override exists
            # for that. Nothing else comes from enrolment any more: contract 2.0
            # took channel names and the broker username off the wire, so the
            # credential is the whole of the station's identity here.
            url = self.config.broker_url or enrolment.broker.url
            try:
                self.transport = build_transport(
                    url,
                    secret=enrolment.credential.secret,
                    trust=self.trust,
                )
            except (tls.Refusal, ValueError) as exc:
                # Refusing to publish is the correct outcome, and everything
                # below still happens: sensing, recording, local alerting and
                # credential renewal are unaffected, and `_publish` already
                # returns False when there is no transport. The one thing that
                # must not happen is connecting anyway.
                #
                # `ValueError` is here for a URL scheme no transport speaks —
                # a `rediss://` left in an environment file after 2.0, most
                # likely. That is a deployment mistake rather than a trust
                # decision, but the right answer is identical and the wrong
                # answer would be worse: an unhandled exception here takes down
                # a box that is otherwise sensing, recording and alerting
                # perfectly well, over a setting nobody can correct until
                # somebody drives out to it.
                self.transport = None
                self.health.raise_condition("uplink.refused", "critical", str(exc))
                self.store.record_event("uplink.refused", "critical", str(exc))
                log.error(
                    "NOT PUBLISHING. %s The station is still sensing, recording "
                    "and alerting locally.", exc,
                )
            else:
                self.health.clear("uplink.refused")

            # **Everything that receives is wired up before anything connects.**
            #
            # `start()` returns immediately and a background thread opens the
            # socket, and `transport.md` is explicit that "commands arrive from
            # the moment the socket opens, because the credential already
            # determines whose they are" — there is no subscribe handshake to
            # delay them. So starting first left a window, however short, in
            # which a command could arrive with no handler registered and be
            # dropped with nothing but a log line.
            #
            # The first thing a platform sends is the one that matters least
            # for being late and most for being lost: the conformance harness
            # opens with `radio.audio`, and a dropped audio lease is a station
            # that stays silent while looking perfectly healthy. A console
            # asking for audio the moment a box reconnects is the same shape,
            # and a station reconnects on every link blip.
            #
            # Ordering costs nothing. The handler is set on the transport
            # object, not on the socket, so registering it before the thread
            # exists is exactly as valid and closes the window entirely.
            if self.transport is not None:
                self.events = EventSender(self.store, self.transport.publish)
            handlers = build_handlers(
                self.radio, self.light, self._apply_config, self.stream,
                self.events, updates=self.updates,
            )
            self.router = CommandRouter(handlers)
            if self.transport is not None:
                self.transport.on_command(self._on_command)
                self.transport.start()

            # The site's own details are things the station needs while the
            # platform is unreachable, so they come from the stored enrolment
            # rather than from a live call. The position does not: it is this
            # station's own (`effective_position`), and enrolling must not
            # overwrite what somebody set on the setup page.
            self.apply_position()
            for driver in (self.adsb, self.weather):
                set_timezone = getattr(driver, "set_timezone", None)
                if set_timezone:
                    set_timezone(enrolment.site.timezone)
            if self.site.version == 0:
                self.site.version = enrolment.config_version
                self.site.save(self.config.site_config_path)

            self._credential_mtime = self.credentials.mtime()

            # Nothing to start here any more. The preview publishes nothing, so
            # enrolment is not what makes it useful — it is started once in
            # run(), before enrolment, which is when an installer is pointing
            # the camera at things and most needs to see the picture.

            if self.renewer is not None:
                self.renewer.stop()
            self.renewer = Renewer(
                self.client, self.credentials, enrolment, self.health,
                on_renewed=self._on_renewed,
            )
            self.renewer.start()

            log.info(
                "Station %s (%s) attached to %s. Contract %s: the credential "
                "is the whole identity — nothing names a channel on the wire.",
                enrolment.site.name, enrolment.station_id,
                redact_url(self.config.broker_url or enrolment.broker.url),
                CONTRACT_VERSION,
            )

    def factory_reset(self) -> list[str]:
        """Return this box to the state it shipped in, and say what went.

        **Everything.** Credential, pinned CA, device selections, site
        configuration, and the local store of events and recordings. Anything
        less leaves a box that looks reset and behaves like the site it came
        from: an old device list makes a new owner's slots wrong, a stale
        `site.version` makes the platform's first `config.set` a no-op, and
        kept events are one customer's data on another customer's hardware.

        The one thing not touched is the setup password, which lives in the
        environment file rather than the state directory. It is how somebody
        reaches this page at all, and a reset that locks the person doing it
        out of the box is a site visit rather than a reset.

        Order matters. Publishing stops first, so nothing is written back
        underneath the deletion — the renewer in particular would happily
        recreate a credential file moments after it was removed.
        """
        gone: list[str] = []
        with self._attach_lock:
            # Stop everything that writes before deleting anything.
            if self.renewer is not None:
                self.renewer.stop()
                self.renewer = None
            if self.transport is not None:
                try:
                    self.transport.close()
                except Exception:  # noqa: BLE001 - already on the way out
                    log.debug("Transport close failed during reset.", exc_info=True)
                self.transport = None
            self.router = None
            self.events = None
            self.enrolment = None
            self._credential_mtime = None

            self.credentials.clear()
            gone.append("credential")
            if self.config.ca_path.exists():
                self.config.ca_path.unlink(missing_ok=True)
                gone.append("pinned broker CA")

            for path, label in (
                (self.config.devices_path, "device selections"),
                (self.config.site_config_path, "site configuration"),
                (self.config.store_path, "events and recordings"),
            ):
                if path.exists():
                    path.unlink(missing_ok=True)
                    gone.append(label)

        # Rebuild in memory so the page the operator is looking at reflects the
        # reset immediately, rather than showing the old world until a restart.
        self.site = SiteConfig.load(self.config.site_config_path)
        self.store = LocalStore(self.config.store_path, self.config.recordings_dir)
        self.inventory = Inventory(self.config.devices_path,
                                   demo=self.config.demo)
        self.build_devices()
        self.health.clear_all()
        log.warning("Factory reset: cleared %s.", ", ".join(gone))
        return gone

    def reload_credential_if_changed(self) -> bool:
        """Pick up a credential this process did not issue itself.

        Re-enrolment can happen from three places: the local console (which
        reattaches directly), `gsu enrol` in another process, or an image
        rewriting the file. Only the first tells the running agent. Without
        this, a box that has been correctly re-enrolled over SSH sits with a
        dead secret and an `uplink.down` alarm until somebody restarts it —
        which on an unattended site means somebody who is hours away.

        Checked only while the uplink is down: a healthy station has no reason
        to re-read its own identity.
        """
        mtime = self.credentials.mtime()
        if mtime is None or mtime == self._credential_mtime:
            return False
        try:
            enrolment = self.credentials.load()
        except ValueError as exc:
            self.health.raise_condition("enrolment.unreadable", "critical", str(exc))
            return False
        if enrolment is None:
            return False
        log.info("The stored credential changed on disk; re-attaching.")
        self.store.record_event(
            "credential.reloaded", "info",
            "Picked up a credential issued by another process.",
        )
        self.health.clear("enrolment.missing")
        self.health.clear("credential.revoked")
        self._attach(enrolment)
        return True

    def _on_renewed(self, enrolment: Enrolment) -> None:
        self.enrolment = enrolment
        # A renewal returns the whole response, CA included, so this is where a
        # rotated CA arrives on a station that never re-enrols.
        self._persist_ca(enrolment)
        if self.transport is not None:
            self.transport.set_credential(enrolment.credential.secret)
        self.store.record_event(
            "credential.renewed", "info",
            f"Credential renewed; expires {enrolment.credential.expires_at.isoformat()}.",
        )

    def _pump_credential_refusal(self) -> None:
        """Turn a relay 4401 into a forced renewal attempt.

        The relay flags `credential_refused` when the platform closes 4401; the
        renewer is the only thing that can tell a revocation (raise the alarm)
        from an expiry-while-offline (renew and reconnect). Without this hand-off
        the box reconnect-loops on 4401 for ever, unable to tell a revoked
        credential from a bad network. Separated from `run` so it is testable.
        """
        refused = getattr(self.transport, "credential_refused", None)
        if refused is not None and refused.is_set():
            refused.clear()
            if self.renewer is not None:
                self.renewer.renew_now()

    # --- commands -------------------------------------------------------

    def _on_command(self, payload: dict) -> None:
        """Queue a command from the relay's reader thread; never run it here.

        Dispatching inline would run device work on the socket's reader thread —
        a radio retune racing the sensing loop's demodulate(), and a slow stream
        start stalling pongs into a reconnect. So this only enqueues and returns:
        slow, non-racing commands to the worker; everything else to the sensing
        loop, which owns the front end. See `_drain_commands` / `_run_command_worker`.
        """
        if self.router is None:
            return
        if str(payload.get("kind", "")) in SLOW_COMMANDS:
            self._slow_commands.put(payload)
        else:
            self._tick_commands.put(payload)

    def _drain_commands(self) -> None:
        """Apply queued device commands on the sensing thread.

        The reason the queue exists: radio.tune/gain/ppm mutate the front end's
        demodulator and buffers, and applying them on the reader thread while the
        sensing thread is inside demodulate() corrupts the filter state. Drained
        here they run on the one thread that reads the front end. Fast by
        construction — a hardware control transfer and a flush — so it never
        holds the loop up. Called at the top of every tick and audio sub-tick.
        """
        if self.router is None:
            return
        while True:
            try:
                payload = self._tick_commands.get_nowait()
            except queue.Empty:
                return
            self.router.dispatch(payload)

    def _run_command_worker(self) -> None:
        """Drain slow commands (video start/stop) off the sensing thread.

        Serial, so a start and a stop cannot race on the one StreamSession, and
        off the reader thread so a start that spends 15 s probing the camera
        cannot stall the socket. It never touches the radio front end, so it
        needs no lock against the sensing loop — StreamSession already handles
        the worker-vs-tick concurrency exactly as it did when these ran on the
        reader thread (see stream.py).
        """
        while not self._stop.is_set():
            try:
                payload = self._slow_commands.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.router is not None:
                self.router.dispatch(payload)


    def _apply_config(self, payload: dict) -> str:
        """`config.set`: apply, persist, and report the new version.

        The platform never assumes the change took — same rule as every other
        command — so the version goes out in the next health frame.
        """
        version = payload.get("version", payload.get("config_version"))
        changed = self.site.apply(payload.get("config") or payload, version)
        self.site.save(self.config.site_config_path)
        for driver in (self.adsb,):
            set_thresholds = getattr(driver, "set_thresholds", None)
            if set_thresholds:
                set_thresholds(self.site.alert_range_km, self.site.alert_altitude_m)
        return f"version {self.site.version}, changed {changed or 'nothing'}"

    # --- the loop -------------------------------------------------------

    def run(self) -> int:
        if not self._take_lock():
            return 1
        self._install_signals()

        reason = clock.implausible_reason()
        if reason is not None:
            # Not fatal to running: sensing and recording do not need a correct
            # clock. Fatal to enrolling, which is where it strands a site.
            self.health.raise_condition("clock.implausible", "critical", reason)
            log.error("Clock is implausible: %s", reason)

        if self.load_enrolment() is None:
            self.health.raise_condition(
                "enrolment.missing", "warning",
                "Not enrolled. Enter an enrolment code on the setup page.",
            )
            log.warning(
                "Not enrolled: sensing and recording locally, publishing nothing. "
                "Enter a code on the setup page or set GSU_ENROL_TOKEN."
            )
            if self.config.enrol_token:
                try:
                    self.enrol(self.config.enrol_token)
                except Exception as exc:  # noqa: BLE001 - shown, not raised
                    log.error("Enrolment with the supplied code failed: %s", exc)

        console = None
        if self.config.setup_enabled:
            from .console import Console

            console = Console.from_config(self, self.config)
            console.start()

        # The one place the preview is started, enrolled or not: it publishes
        # nothing, so there is nothing for an identity to gate. Starting the
        # thread costs nothing while nobody is watching — it takes a frame only
        # inside the demand window that `/status.json` opens, which is exactly
        # the moment an installer is pointing the box at things.
        self.video.start()

        tick = self.config.tick_seconds
        weather_due = 0.0
        health_due = 0.0
        next_tick = time.monotonic()
        log.info("Station agent %s running at %.1f Hz.", AGENT_VERSION, 1 / tick)
        # A no-op unless transcription is available; safe to call unconditionally.
        self.transcriber.start()

        # Commands run off the socket's reader thread: this worker takes the slow
        # ones (video start/stop) and the sensing loop drains the rest. See
        # _on_command / _run_command_worker.
        self._command_worker = threading.Thread(
            target=self._run_command_worker, name="gsu-commands", daemon=True)
        self._command_worker.start()

        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    self.step(tick, weather_due <= 0, health_due <= 0)
                except Exception:  # noqa: BLE001 - a loop that dies is a dead site
                    log.exception("Tick failed; continuing.")
                # Audio is a stream and everything else here is a reading, so
                # between full sweeps the radio is pumped on its own. At one
                # tick per second the receiver was handed a whole second to
                # demodulate at once, which is a whole second of latency before
                # a syllable can even leave the box — and the console's
                # prebuffer then sizes itself from the chunk it receives, so a
                # one-second chunk cost another 1.25 on top. Squelch opening
                # and audio arriving two seconds later is not a radio.
                #
                # Not a thread. The front end is a single device with a single
                # reader, and a second thread reading it is the bug that has
                # already cost this project a camera and a snapshot channel.
                # Sub-ticking on the same thread keeps one reader; the total
                # sample rate is unchanged, only the granularity.
                weather_due = (
                    self.site.weather_period_s if weather_due <= 0 else weather_due - tick
                )
                health_due = (
                    self.site.health_period_s if health_due <= 0 else health_due - tick
                )
                if started - self._last_prune > PRUNE_EVERY_SECONDS:
                    self._last_prune = started
                    self.store.prune(
                        self.site.audio_retention_hours,
                        self.site.audio_retention_mb,
                        self.site.event_retention_days,
                        self.site.transcript_retention_days,
                    )
                # Before the rediscovery/rebuild decision: keep the encoder warm
                # where configured, and — when a camera change is owed — get it
                # to let the sensor go so the rebuild below can run.
                self._maintain_warm_stream()
                if (
                    started - self._last_discovery > REDISCOVER_SECONDS
                    and self._anything_missing()
                ):
                    self.build_devices()
                elif self._camera_rebuild_owed and not self._stream_holds_camera():
                    # The stream has let go of the sensor, so the camera swap
                    # that was deferred can happen now. Not on the rediscovery
                    # interval and not behind `_anything_missing`: this is a
                    # configuration change somebody made and is waiting on, not
                    # a hunt for hardware that might have been plugged back in.
                    log.info("Applying the deferred camera change now the live "
                             "stream has released the sensor.")
                    self.build_devices()
                if self.transport is None or not self.transport.connected:
                    # Also checked when there is no transport at all: a refused
                    # uplink is exactly the case a technician fixes by
                    # re-enrolling, which brings a new CA with it.
                    self.reload_credential_if_changed()
                self._pump_credential_refusal()
                # Absolute schedule rather than sleep(tick): a slow tick must
                # not make the cadence drift away from 1 Hz for ever.
                next_tick += tick
                # Overran by more than a whole period: give up on catching up
                # rather than running a burst of sweeps back to back.
                if next_tick - time.monotonic() < -tick:
                    next_tick = time.monotonic()
                self._sleep_pumping_radio(next_tick)
        finally:
            if console is not None:
                console.stop()
            self.shutdown()
        return 0

    def _stream_holds_camera(self) -> bool:
        """Whether the live encoder owns the sensor right now.

        "starting" counts: the encoder is about to take the sensor, and a
        rebuild dispatched in that window kills the stream at birth.

        An in-flight teardown counts too. A detached stop flips the state to
        idle at once but reaps the encoder — and releases the sensor — on a
        worker thread; rebuilding the camera into that window hands the next
        reader a device the kernel still says is busy. This matters more now the
        keep-warm loop yields the camera by a detached stop, but it was always
        the honest answer for the lease-expiry path too.
        """
        stream = getattr(self, "stream", None)
        if stream is None:
            return False
        if stream.state in ("streaming", "starting"):
            return True
        teardown = getattr(stream, "_teardown_thread", None)
        return bool(teardown is not None and teardown.is_alive())

    def _maintain_warm_stream(self) -> None:
        """Keep the live encoder warm between viewers, where the box is set to.

        Runs on the sensing loop. A warm encoder holds the camera, so it must
        yield when a deferred camera change is waiting on the sensor — otherwise
        the change never lands. It yields only when the platform is not actually
        watching; an active viewer defers the change exactly as it did before
        keep-warm existed.
        """
        stream = getattr(self, "stream", None)
        if stream is None or not stream.keeps_warm():
            return
        if self._camera_rebuild_owed:
            # A configuration change is waiting for the sensor. Let it have it;
            # the warm start below brings the encoder back once the rebuild has
            # run and the flag clears.
            stream.yield_camera_if_warm()
            return
        if stream.wants_warm_start():
            # Onto the slow-command worker, never inline: start() can spend
            # fifteen seconds probing the camera, and this runs on the sensing
            # loop the radio's audio shares. note_warm_attempt starts the
            # backoff so ticks in the meantime do not queue a second start.
            stream.note_warm_attempt()
            self._slow_commands.put({"kind": "video.start", "_warm": True})

    @staticmethod
    def _retire_driver(driver) -> None:
        """Take the hardware off the outgoing driver, permanently.

        retire() where the driver has one (a retired driver never reopens —
        the race this closes is described in build_devices); close() where it
        does not, which is fine for drivers that hold nothing another instance
        would contend for.
        """
        if driver is None:
            return
        retire = getattr(driver, "retire", None) or getattr(driver, "close", None)
        if retire is None:
            return
        try:
            retire()
        except Exception:  # noqa: BLE001 - the replacement matters more
            log.exception("Retiring an outgoing driver failed; continuing.")

    def _anything_missing(self) -> bool:
        # The camera is excluded while the stream holds the sensor: snapshot
        # failures during a stream are contention, not a missing device, and
        # counting them here is what put rediscovery into a rebuild loop on
        # the first real station.
        skip_camera = self._stream_holds_camera()
        return any(
            report.configured and report.driver_available and report.status != "present"
            and not (report.slot == "camera" and skip_camera)
            for report in self.inventory.report()
        )

    def step(self, dt: float, weather_due: bool = False, health_due: bool = False) -> None:
        """One tick. Sensing first, publishing last, and no step in between
        cares whether the link is up."""
        # Apply any queued device commands before this tick senses, so a retune
        # or gain change lands on the front end on the one thread that reads it.
        self._drain_commands()
        light_load = getattr(self.light, "load_w", 0.0) if self.light else 0.0

        reading = None
        if self.power is not None:
            reading = self.power.read(dt, extra_load_w=light_load)
            # Duty cycling: the station sheds its own load rather than waiting
            # for a command that may never arrive.
            if (
                self.light is not None
                and reading.soc_pct < self.site.shed_light_below_soc_pct
                and self.light.on
            ):
                log.warning(
                    "Shedding the floodlight at %.0f%% state of charge.", reading.soc_pct
                )
                self.store.record_event(
                    "power.shed", "warning",
                    f"Floodlight shed at {reading.soc_pct:.0f}% state of charge.",
                )
                self.light.request(False)

        if self.light is not None:
            self.light.step(dt)
        self._evaluate_light(dt)

        contacts = self.adsb.poll(dt) if self.adsb is not None else None

        # Normally the sub-ticks between sweeps have already read the
        # receiver and left their telemetry here — the front end is a single
        # device with a single reader, so asking it for another second of
        # samples now would be asking for two seconds in every one.
        #
        # But `step` has to stand on its own: it is the unit the tests drive
        # and the unit a single-shot run executes, and a radio that only works
        # when the outer loop happens to be sleeping is a radio that works by
        # accident. So if nothing has pumped, this does.
        if not self._radio_pumped:
            self._pump_radio(dt)
        self._radio_pumped = False
        radio_payload, self._radio_telemetry = self._radio_telemetry, None

        # The fail-closed half of the live stream: an expired lease, the
        # ceiling, or an encoder that has died stops it here rather than needing
        # a command that may never arrive.
        self.stream.tick()

        self._evaluate_alerts(contacts, reading)

        # --- publishing, which is allowed to fail -----------------------
        # Every stream reports on its own cadence whether or not it has a
        # source. A stream with none says so explicitly; going quiet is what a
        # failed station looks like, and the console cannot tell the two apart.
        # Events go first, and on their own schedule rather than this tick's.
        # They are the only thing here that cannot be dropped, so they get the
        # link before the telemetry that can — and a station coming back from
        # an outage drains its backlog one acknowledged batch at a time rather
        # than arriving as a flood.
        if self.events is not None:
            self.events.pump()

        reports = self._reports()
        if contacts is not None:
            # An empty list here is a real statement: the receiver is alive and
            # the sky is clear. It is only ever sent when that is true.
            self._publish_telemetry(
                {"kind": "adsb", "aircraft": [c.to_payload() for c in contacts]}
            )
        else:
            self._publish_telemetry(self.unavailable_payload("adsb", reports))
        self._publish_telemetry(
            reading.to_payload() if reading is not None
            else self.unavailable_payload("power", reports)
        )
        # **Gated on the device being present, like every other slot.** A
        # `RadioController` exists whenever a radio is configured, and its
        # `tick` always returns a payload — so `radio_payload is not None` says
        # nothing about whether a dongle is plugged in. A disconnected receiver
        # went on publishing a dead noise floor as an available reading, and the
        # console, having no reason to doubt it, drew the panel green while the
        # station's own setup page showed the radio absent. The other streams
        # read `available: false` from their sensor returning nothing; the radio
        # never returns nothing, so its absence is read here from the slot
        # report instead — the same report that drives the health inventory, so
        # the two now agree.
        radio_report = reports.get("radio")
        radio_present = radio_report is not None and radio_report.status == "present"
        if radio_payload is not None and radio_present:
            self._publish_radio(radio_payload)
        else:
            # The console has been told the stream is unavailable, so whatever
            # the gate does next is news to it whichever way it lands. A
            # receiver that comes back must not have its first state swallowed
            # as "unchanged" against a reading from before it went away.
            self._radio_gate = None
            self._publish_telemetry(self.unavailable_payload("radio", reports))
        self._publish_telemetry(
            {"kind": "light", "on": self.light.on} if self.light is not None
            else self.unavailable_payload("light", reports)
        )
        if weather_due:
            weather = (
                self.weather.read(self.site.weather_period_s)
                if self.weather is not None else None
            )
            self._publish_telemetry(
                weather.to_payload() if weather is not None
                else self.unavailable_payload("weather", reports)
            )
        if health_due:
            self._publish_telemetry(self.health_payload())

        self._update_link_state()

    def _accumulate_over(self, open_now: bool, pcm: bytes, freq_hz: int) -> None:
        """Collect one transmission's audio while the squelch is open, and hand
        the whole over to the transcriber the tick the gate closes — a model
        reads a whole over far better than the 125 ms slices the loop runs in."""
        if open_now:
            if not self._over_open:
                self._over_open = True
                self._over_freq_hz = freq_hz
                self._over_started_at = datetime.now(UTC)
                self._over_pcm = bytearray()
            if pcm:
                self._over_pcm.extend(pcm)
        elif self._over_open:
            self._over_open = False
            self.transcriber.submit(
                bytes(self._over_pcm), AUDIO_RATE, self._over_freq_hz,
                self._over_started_at or datetime.now(UTC),
            )
            self._over_pcm = bytearray()

    def _record_transcript(
        self, freq_hz: int, started_at: datetime, duration_s: float, text: str
    ) -> None:
        """Write a transcript as an event, from the transcription worker thread.

        `store.record_event` is safe to call from here — the store's connection
        is `check_same_thread=False` behind a lock. The event syncs to the
        platform's stream like any other, so the transcript needs no transport of
        its own.
        """
        self.store.record_event(
            TRANSCRIPT_KIND,
            "info",
            f"{freq_hz / 1e6:.3f} MHz, {duration_s:.0f}s: {text}",
        )

    def _pump_radio(self, dt: float, publish_gate_change: bool = False,
                    publish_spectrum: bool = False) -> dict | None:
        """Read the receiver once, publish any audio, keep the telemetry.

        The single reader of the front end. Audio goes out the moment it
        exists, because it is a stream; the telemetry waits for the next sweep,
        because a signal level is a reading and eight a second is seven more
        than anybody needs.

        `publish_gate_change` is for the callers with no sweep behind them —
        see the gate note below. `publish_spectrum` is the same idea for the one
        reading that *is* watched moving: while a console has the spectrum open,
        the frame carrying it goes out on the sub-tick rather than waiting for
        the sweep. The sweep passes neither, because it is about to publish this
        very payload a few lines later.
        """
        if self.radio is None:
            return None
        telemetry, audio = self.radio.tick(dt)
        self._radio_telemetry = telemetry
        self._radio_pumped = True

        # **The gate is an edge, not a reading, and it does not wait.**
        #
        # Everything else on this payload is a level: rssi, the floor, the
        # threshold. Once a second is plenty for those and the reason the rest
        # of the frame waits for the sweep. `squelch_open` is not one of them —
        # it is the console's channel-open light, and a light that comes on
        # after the sound it is announcing is worse than no light, because the
        # operator reads it as a second, later event.
        #
        # Audio leaves on the sub-tick that produced it; the frame saying the
        # gate opened left on the next sweep, up to a second behind. So the
        # console lit the LED most of a second after it started playing, and
        # dropped it most of a second after the audio stopped.
        #
        # Publishing the edge does not replace the fixed cadence — the sweep
        # still sends this stream every second whether anything changed or not,
        # which is what `transport.md` requires of a droppable stream. It adds a
        # frame when the gate moves, and the receiver's hang time bounds how
        # often that can be: a signal sitting on the threshold cannot close more
        # than once per HANG_SECONDS, so this is a few hundred bytes on an over,
        # not a new cadence.
        gate_moved = publish_gate_change and (
            bool(telemetry.get("squelch_open")),
            bool(telemetry.get("monitor")),
        ) != self._radio_gate
        # `spectrum` is only on the frame inside the demand window, so this
        # publishes nothing extra when nobody is watching. One publish covers
        # both reasons — a gate edge that also happens to carry the spectrum
        # must not go out twice.
        spectrum_due = publish_spectrum and "spectrum" in telemetry
        if gate_moved or spectrum_due:
            self._publish_radio(telemetry)

        # The setup-page switch, read live so it takes effect without a restart.
        self.transcriber.enabled = (
            self.config.radio_transcribe or self.site.radio_transcribe
        )
        # Accumulate the current over for transcription and submit it when the
        # gate closes. Before the `audio is None` return below, because the gate
        # closing — which is exactly when an over ends — produces no audio.
        # Guarded on availability so a station without transcription buffers
        # nothing.
        if self.transcriber.available:
            self._accumulate_over(
                bool(telemetry.get("squelch_open")),
                self.radio.last_pcm if self.radio is not None else b"",
                int(telemetry.get("freq_hz", 0)),
            )

        # Recorded whether or not it can be sent, whether or not anybody is
        # listening, and whether or not there is an Opus encoder at all. A
        # transmission during an outage is not simply gone; one nobody had a
        # console open for is not gone; and one this box cannot encode is not
        # gone either — the recording is PCM and needs no codec, so it must not
        # hang on the audio payload existing. A box without libopus produces no
        # payload (audio is None) but still hears, squelches and records; gating
        # this on the payload silently made recording depend on the codec, which
        # is the one thing it was written to be independent of. So it keys off
        # `last_pcm`, set whenever the gate is open, not off `audio`. From
        # `last_pcm` and not the wire payload for the other half of the reason
        # too: the wire carries Opus, a WAV on disk can be opened by anything,
        # and decoding the frame we just encoded to get back to where we started
        # would be absurd.
        if self.radio.last_pcm:
            self.store.write_audio(
                self.radio.last_pcm, AUDIO_RATE,
                label=f"{self.radio.freq_hz // 1000}kHz",
            )

        if audio is None:
            return None
        # Published only while somebody is listening. 24 kHz of 16-bit mono is
        # 384 kbit/s, and base64 in a JSON envelope makes it 512 — the largest
        # thing this station sends, and it used to go up on every over whether
        # or not a console existed to hear it. The platform asks and renews;
        # silence stops it. See `RadioController.want_audio`.
        if self.radio.audio_wanted:
            self._publish(AUDIO, audio)
        return audio

    def _sleep_pumping_radio(self, until: float) -> None:
        """Wait for the next full sweep, running the radio at AUDIO_TICK_S.

        The radio's own readings are not published here — the levels stay on
        the one-second cadence with everything else, because a signal level is
        a reading and nobody needs it eight times a second. What does go out is
        the audio, only when the squelch is open, and a `radio` frame on the
        sub-tick where the gate itself moves, which is the one field on that
        payload nobody can wait a second for. See `_pump_radio`.
        """
        # Paced against a deadline, and demodulating the time that actually
        # passed — not the interval we meant to sleep for.
        #
        # This slept a whole AUDIO_TICK_S and then asked the receiver for one
        # AUDIO_TICK_S of audio, which ignores everything the tick itself
        # costs: a pure-Python demodulate over three thousand samples, an Opus
        # encode, a base64, a publish, and a WAV appended to the SD card. On a
        # Pi that is about 25 ms, so every cycle took 150 ms of wall clock and
        # produced 125 ms of audio.
        #
        # **A station that generates audio slower than real time cannot be
        # rescued downstream.** Measured on the bench at 0.82 seconds of audio
        # per second of wall clock: the console's buffer drains at 18% no
        # matter how deep it is, underruns, and the operator hears speech
        # chopping in and out for ever. Two playback fixes went in ahead of
        # this one and neither could have worked.
        # The clock is the agent's, not this call's. Resetting it here left the
        # sweep's own work — every sensor, the telemetry publish, the health
        # pass — outside the audio timeline, and that is another tenth of every
        # second the receiver is never asked for: 0.91 seconds of audio per
        # second, down from 0.82 but still draining the console's buffer.
        # Carried across sweeps, the gap between them is demodulated like any
        # other elapsed time.
        last = self._audio_clock or time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= until:
                self._audio_clock = last
                return
            if self.radio is None:
                self._audio_clock = last
                self._stop.wait(until - now)
                return
            # To the next boundary, not for a whole interval: the work above
            # has already spent part of this one.
            nap = min(last + AUDIO_TICK_S, until) - now
            if nap > 0 and self._stop.wait(nap):
                self._audio_clock = last
                return
            now = time.monotonic()
            elapsed, last = now - last, now
            self._audio_clock = last
            # Apply queued device commands before this sub-tick reads the front
            # end, so a retune/gain change is serialised with demodulate() on the
            # one thread that owns the receiver rather than racing it.
            self._drain_commands()
            # While a console is watching the spectrum, push it out between
            # sweeps too — throttled to SPECTRUM_TICK_S so it rides roughly
            # every other audio sub-tick rather than all eight a second. Bounded
            # by the receiver's own demand window, so a station nobody is
            # watching sends nothing here.
            spectrum_due = (
                self.radio is not None
                and self.radio.spectrum_wanted
                and now - self._spectrum_pub_at >= SPECTRUM_TICK_S
            )
            try:
                # Clamped, so a stalled box asks for a burst it cannot use
                # rather than an unbounded one. Falling behind after a stall is
                # right — stale airband audio is worth less than the gap.
                # The gate change goes out from here, because the next sweep is
                # up to a second away and the console's channel-open light must
                # not trail the audio it is announcing.
                self._pump_radio(min(elapsed, AUDIO_TICK_S * 4),
                                 publish_gate_change=True,
                                 publish_spectrum=spectrum_due)
            except Exception:  # noqa: BLE001 - a dead loop is a dead site
                log.exception("Radio sub-tick failed; continuing.")
            else:
                if spectrum_due:
                    self._spectrum_pub_at = now

    def video_state(self) -> dict:
        """What the camera is doing, in one place.

        "What is the camera costing me" used to have two answers — a snapshot
        channel running continuously at a low rate, and a live stream running
        only while somebody watched. It now has one: the stream. The snapshot
        channel is gone, and `snapshots: false` says so out loud rather than
        leaving a console to infer it from a bitrate of zero, which is also
        what a broken camera looks like.

        `sensor` is new and is the answer to the question the last three fixes
        were all circling: who is holding the camera right now.
        """
        state = self.video.stats()
        state["stream"] = self.stream.state_payload()
        state["camera"] = self._camera_backend()
        state["sensor"] = self.sensor_lease.state()
        return state

    def _camera_backend(self) -> dict:
        """Which capture path the camera is on, and why that one.

        Separated out of the driver's free-text `detail` because it answers a
        question people ask directly — "why is the camera slow" — and a setup
        page should be able to answer it without an SSH session. The reason is
        the driver's own, never inferred here: a station
        that guesses at this would send somebody to site for a camera fault
        that is really a virtual environment built without
        `--system-site-packages`.

        Two fields and not three: whether the camera is simulated is already
        reported per slot in `devices[]`, and a second copy computed a different
        way here is how two places start disagreeing about the same station.
        """
        driver = self.inventory.drivers.get("camera")
        return {
            "backend": getattr(driver, "backend", None),
            "backend_reason": getattr(driver, "backend_reason", ""),
        }

    def unavailable_payload(self, kind: str, reports: dict | None = None) -> dict:
        """Declare a stream the station has no source for.

        `available: false` says *there is nothing behind this stream*, which is
        a different statement from a field the instrument does not measure —
        that one is simply omitted (humidity on a weather head with no RH
        module). Reaching for this when the other is meant would tell an
        operator the weather station is missing when it is working.

        Sent on the stream's normal cadence, never in place of going quiet: a
        station that stops publishing has failed, and "I have no receiver" is
        something that has to keep being said.
        """
        report = (self._reports() if reports is None else reports).get(kind)
        if report is None or not report.configured:
            reason = NO_SOURCE.get(kind, f"no source for {kind}")
        elif report.status == "stalled":
            # Fitted and it has stopped, which is a fault rather than an
            # absence, and an operator acts differently on the two.
            reason = f"{report.label} stopped responding"
        elif report.detail.startswith(report.label):
            # The detail already names the device; repeating the label would
            # spend a third of the 200 characters saying it twice.
            reason = report.detail
        else:
            reason = f"{report.label} configured but not detected"
            if report.detail:
                reason = f"{reason}: {report.detail}"
        return {
            "kind": kind,
            "available": False,
            "unavailable_reason": reason[:REASON_LIMIT],
            # Why, in one machine-readable word, beside the sentence.
            #
            # "Nothing is selected for this slot" and "something is selected
            # and is not working" need opposite reactions — one is a complete
            # station, the other is a fault someone has to go and fix — and the
            # only place that distinction lived was inside an English sentence
            # the console would have had to parse.
            #
            # It rides the stream rather than the health frame because of when
            # it is needed. Health goes out every 30 seconds; these go out at
            # the stream's own cadence. A console that has just connected, or
            # just switched station, was waiting up to half a minute to find
            # out a slot was empty, and showing a red X or a skeleton in the
            # meantime. The answer is known the instant the first frame lands.
            "unavailable_cause": (
                "not_fitted"
                if report is None or not report.configured
                else "stopped" if report.status == "stalled"
                else "not_detected"
            ),
        }

    def _reports(self) -> dict:
        return {
            report.telemetry_kind: report
            for report in self.inventory.report()
            if report.telemetry_kind
        }

    def _publish_telemetry(self, payload: dict) -> bool:
        return self._publish(TELEMETRY, self._stamp_simulated(payload))

    def _publish_radio(self, payload: dict) -> bool:
        """Publish a `radio` frame, remembering the gate state it carried.

        Every route out — the sweep and the sub-tick edge — goes through here,
        so "what has the console been told" is one fact in one place. Recording
        it on the sweep's frame as well is what stops the next sub-tick sending
        a duplicate of something that has just gone.
        """
        self._radio_gate = (bool(payload.get("squelch_open")),
                            bool(payload.get("monitor")))
        return self._publish_telemetry(payload)

    def _stamp_simulated(self, payload: dict) -> dict:
        """Mark a stream whose source is a demo sensor.

        **Per stream, not per station.** A station is routinely part real: a
        bench box with a live camera and a demo weather head is the normal way
        to develop against one, and the old station-wide `is_simulated` flag had
        to be wrong about one half of it. Whether a reading is synthetic is a
        property of the sensor that produced it, and this is the only place that
        knows — the slot report says `simulated` because the driver said so.

        Stamped here rather than in each payload builder so that no stream can
        be added later and quietly forget. The flag is only ever *added*: a
        payload that already carries one (an ADS-B contact's own `simulated`,
        which means something different — a test target injected by a real
        receiver) is left exactly as it is.
        """
        kind = payload.get("kind")
        if not kind or "simulated" in payload:
            return payload
        report = self._reports().get(kind)
        if report is None or not report.simulated:
            return payload
        return {**payload, "simulated": True}

    def _publish(self, stream: str, payload: dict) -> bool:
        if self.transport is None:
            return False
        sent = self.transport.publish(stream, payload)
        if sent:
            self._published += 1
        return sent

    # --- alerting, which happens with or without a link -----------------

    def _evaluate_alerts(self, contacts, power) -> None:
        if contacts is not None:
            alerting = {c.icao for c in contacts if c.alert}
            for contact in contacts:
                if contact.alert and contact.icao not in self._alerting_icao:
                    self.store.record_event(
                        "adsb.proximity", "warning",
                        f"{contact.callsign or contact.icao} at {contact.range_km:.1f} km, "
                        f"{(contact.altitude or 0):.0f} m.",
                    )
            self._alerting_icao = alerting

        if power is None:
            return
        # Hysteresis on the way back up, or a battery sitting on the threshold
        # writes an event a second.
        state = "ok"
        if power.soc_pct < self.site.critical_battery_pct:
            state = "critical"
        elif power.soc_pct < self.site.low_battery_pct:
            state = "low"
        if state != self._battery_state:
            recovering = state == "ok" and power.soc_pct < self.site.low_battery_pct + 2
            if not recovering:
                if state == "ok":
                    self.health.clear("power.battery")
                    self.store.record_event(
                        "power.recovered", "info",
                        f"Battery recovered to {power.soc_pct:.0f}%.",
                    )
                else:
                    self.health.raise_condition(
                        "power.battery",
                        "critical" if state == "critical" else "warning",
                        f"Battery at {power.soc_pct:.0f}%.",
                    )
                    self.store.record_event(
                        "power.battery", "critical" if state == "critical" else "warning",
                        f"Battery {state} at {power.soc_pct:.0f}%.",
                    )
                self._battery_state = state

    def _evaluate_light(self, dt: float) -> None:
        """Fault-check the floodlight against its measured current draw.

        Only when a sensor is configured (`measured_a` is None otherwise —
        the driver's declaration that there is nothing to disagree with).
        Two faults, deliberately at different volumes:

        - commanded on, no draw → **warning**. The lamp, its fuse or its
          wiring: the site is dark when it was asked not to be, which matters
          and does not compound.
        - commanded off, still drawing → **critical**. A relay welded closed
          is a light burning a battery at an unattended site; every hour it
          goes unnoticed is runtime nobody gets back.

        Judged only after `LIGHT_SETTLE_SECONDS` in the same commanded state.
        Within the window nothing is raised *or cleared*: a fault that was
        true before the switch stays declared until the new state has had its
        chance to be measured.

        The measured amps stay off the telemetry wire: `light` in
        `contract/schemas/telemetry.schema.json` carries no such field, and
        this station does not invent schema (CONTRACT-QUESTIONS.md item 15
        proposes it). The amps reach people through the setup page's light
        tab and the device detail in the health frame; the faults travel as
        health conditions, which the contract already carries.
        """
        light = self.light
        measured = getattr(light, "measured_a", None) if light is not None else None
        if measured is None:
            self._declare_light_fault(None, 0.0, 0.0)
            self._light_commanded = None
            return
        commanded = bool(getattr(light, "commanded", light.on))
        if commanded != self._light_commanded:
            self._light_commanded = commanded
            self._light_settled_s = 0.0
        self._light_settled_s += dt
        if self._light_settled_s < LIGHT_SETTLE_SECONDS:
            return
        threshold = float(getattr(light, "sense_threshold_a", 0.0) or 0.0)
        if threshold <= 0:
            # A zero threshold cannot distinguish anything from anything.
            self._declare_light_fault(None, 0.0, 0.0)
            return
        drawing = float(measured) >= threshold
        if commanded and not drawing:
            self._declare_light_fault("no_draw", float(measured), threshold)
        elif not commanded and drawing:
            self._declare_light_fault("stuck_on", float(measured), threshold)
        else:
            self._declare_light_fault(None, 0.0, 0.0)

    def _declare_light_fault(self, fault: str | None, measured: float,
                             threshold: float) -> None:
        """Raise/clear the two light conditions, and record edges as events."""
        if fault != "no_draw":
            self.health.clear("light.no_draw")
        if fault != "stuck_on":
            self.health.clear("light.stuck_on")
        if fault == "no_draw":
            detail = (
                f"The floodlight is commanded on but drawing {measured:.2f} A "
                f"(threshold {threshold:g} A). Lamp, fuse or wiring."
            )
            self.health.raise_condition("light.no_draw", "warning", detail)
        elif fault == "stuck_on":
            detail = (
                f"The floodlight is commanded off but drawing {measured:.2f} A "
                f"(threshold {threshold:g} A). The relay may be welded closed "
                "— that is a light burning the battery until someone opens "
                "the circuit."
            )
            self.health.raise_condition("light.stuck_on", "critical", detail)
        if fault != self._light_fault:
            self._light_fault = fault
            if fault is not None:
                self.store.record_event(
                    f"light.{fault}",
                    "critical" if fault == "stuck_on" else "warning",
                    detail,
                )
            else:
                self.store.record_event(
                    "light.recovered", "info",
                    "The floodlight's measured draw agrees with its commanded "
                    "state again.",
                )

    def security(self) -> dict:
        """How this station's link is protected, as a fact rather than a hope.

        Rendered on the local console and carried in the health frame, because
        "am I actually on TLS, and against which CA" is not a question anyone
        should have to answer by reading source or a packet capture.
        """
        url = None
        if self.transport is not None:
            url = self.transport.url
        elif self.enrolment is not None:
            url = self.config.broker_url or self.enrolment.broker.url
        return {
            # Redacted: the local console has no authentication and this frame
            # goes over the wire. Neither is a place for a pasted password.
            "broker_url": redact_url(url),
            "broker_tls": tls.is_tls(url) if url else None,
            "platform_tls": tls.is_tls(self.config.platform_url),
            # Two roots, reported separately. "Which CA is this box trusting"
            # has two answers and merging them into one is what produced the
            # arrangement this replaced.
            "trust": self.trust.to_dict(),
            "api_trust": self.api_trust.to_dict(),
            "publishing": self.transport is not None,
            "tls_failed": bool(getattr(self.transport, "tls_failed", False)),
        }

    def _update_link_state(self) -> None:
        up = bool(self.transport and self.transport.connected)
        # A certificate the station will not accept is a different fault from a
        # link that is down, and an operator acts differently on the two.
        if getattr(self.transport, "tls_failed", False):
            self.health.raise_condition(
                "uplink.tls_failed", "critical",
                f"The broker's certificate did not verify against "
                f"{self.trust.describe()}. Nothing is being published, and this "
                "station will not connect without verifying. "
                f"Last error: {self.transport.last_error}",
            )
        elif up:
            self.health.clear("uplink.tls_failed")
        if self._link_up is None:
            self._link_up = up
            return
        if up == self._link_up:
            return
        self._link_up = up
        if up:
            offline = time.monotonic() - (self._offline_since or time.monotonic())
            self._offline_since = None
            self.health.clear("uplink.down")
            self.store.record_event(
                "uplink.up", "info", f"Uplink restored after {offline:.0f}s.",
            )
        else:
            self._offline_since = time.monotonic()
            self.health.raise_condition(
                "uplink.down", "warning",
                "No route to the broker; telemetry is being dropped and events "
                "are being recorded locally.",
            )
            self.store.record_event("uplink.down", "warning", "Uplink lost.")

    # --- health ---------------------------------------------------------

    def health_payload(self) -> dict:
        """The `health` telemetry kind — in the contract, and consumed.

        Proposed from this side and since adopted: it is in
        `contract/schemas/telemetry.schema.json` `$defs/health` and in the
        platform ingest's `KNOWN_KINDS`, and `devices[].simulated` is what
        drives the console's DEMO badge. **Validate changes here against the
        schema** — `tests/test_station.py` does, in both the enrolled and
        unenrolled states, because this payload is the one whose shape varies
        most with what is wrong at the time.

        It carries the things there is otherwise no way to say: the config
        version the station is running (`contract/enrolment.md` §7 requires it
        be reported in telemetry), the devices it actually found against the
        ones it was told to expect, **which telemetry streams have no source at
        all**, and whether the credential is renewing.

        `security`, `clock`, `resources` and `software` are not in the schema
        yet. The schema allows additional properties, so they are valid rather
        than merely tolerated — they are proposed properly in CONTRACT-QUESTIONS.
        """
        # Re-evaluated here rather than only at build time: a device that was
        # absent at boot and has since started talking must stop being reported
        # as missing without anyone restarting anything.
        self._report_capabilities()
        self._check_clock()
        credential = self.enrolment.credential if self.enrolment else None
        transport = self.transport
        payload = {
            "kind": "health",
            "agent_version": AGENT_VERSION,
            # Declared, never negotiated. The platform reads this to know what
            # this station is capable of emitting — it is the only lever that
            # makes any non-advisory addition safe to send to a fleet, because
            # a 2.0 box ignores a field it does not know and says nothing.
            "contract_version": CONTRACT_VERSION,
            "config_version": self.site.version,
            # The contract's summary vocabulary (ok | degraded | failing), which
            # is deliberately not the per-condition severity vocabulary
            # (info | warning | critical) carried in `conditions` below. They
            # answer different questions; see health.Health.SUMMARY.
            "status": self.health.summary(),
            "conditions": self.health.to_list(),
            "uplink": {
                "connected": bool(transport and transport.connected),
                "dropped_frames": transport.dropped if transport else 0,
                "offline_seconds": round(
                    time.monotonic() - self._offline_since, 1
                ) if self._offline_since else 0.0,
            },
            # Rising `events_pending` is the one delivery fault this contract
            # calls out by name: telemetry dropping is normal and expected,
            # events accumulating is a channel that has stopped draining.
            "events_pending": self.events.pending if self.events else 0,
            "events_dropped": self.events.dropped if self.events else 0,
            # Two things a remote box cannot be asked in person: whether its
            # link is verified, and whether its clock is disciplined by
            # anything. Both are cheap to state and expensive to guess.
            "security": self.security(),
            # How often this station actually publishes each stream, so a
            # console can work out what "late" means instead of assuming.
            #
            # The contract states these cadences and the console derived its
            # staleness thresholds from them as bare literals — 3x for the 1 Hz
            # streams, 6x for weather. But `weather_period_s` is a site setting
            # and is settable at runtime over the command channel, so raising
            # it above 30s on a metered link — an entirely reasonable thing to
            # do — put a permanent red X on a perfectly healthy station, with
            # nothing on either side to say why. The station is the only party
            # that knows its own cadence; it says so here.
            "cadence": {
                "adsb": 1.0,
                "power": 1.0,
                "radio": 1.0,
                "light": 1.0,
                "weather": float(self.site.weather_period_s),
                "health": float(self.site.health_period_s),
            },
            "clock": clock.discipline().to_dict(),
            "devices": [report.to_dict() for report in self.inventory.report()],
            # The console's reason to render "no receiver" rather than an empty
            # panel that looks like quiet airspace.
            "unsourced_streams": sorted(self.inventory.unsourced_streams()),
            "unsourced_fields": self._unsourced_fields(),
            "resources": [resource.to_dict() for resource in self.inventory.resources()],
            "storage": self.store.stats(),
            # What video is costing, measured. It is the largest single consumer
            # on the link when it is running, and the only honest way to know
            # what a given camera and setting cost is for the box to say — see
            # gsu/video.py and HARDWARE.md §8.
            "video": self.video_state(),
            # The running software version, and — while a remote update is in
            # flight — the desired version and the host updater's last result.
            # DECISIONS item 49: without this the platform watches `agent_version`
            # (a build constant) sit unchanged across every release and can see
            # neither an update landing nor a rollback, nor tell a revoked box
            # from an offline one. Sourced from gsu/update.py's UpdateCoordinator
            # — the same state the local /status.json shows. Not yet named in
            # `$defs/health`; valid because that object is deliberately
            # permissive, and proposed in CONTRACT-QUESTIONS for the platform to
            # formalise and consume.
            "software": self.updates.state(),
            "uptime_s": round(time.monotonic() - self._started, 1),
        }
        # Renewal health, and only when there is a credential to have any. The
        # schema types `expires_at` as a string; a null would be this station
        # breaking its own rule that an unsourced value is omitted rather than
        # defaulted (DECISIONS.md item 16). A station with no credential has no
        # renewal health — that fact is `enrolment.missing` in `conditions`.
        if credential is not None:
            payload["credential"] = {
                "expires_at": credential.expires_at.isoformat(),
                "renewal_failures": self.renewer.failures if self.renewer else 0,
            }
        # Where this station is, when it has been told. The station is the only
        # place a position is entered (owner's decision; the platform's field
        # stops being editable), so this is how the platform learns it — on the
        # health cadence rather than at enrolment, because a position corrected
        # six months later must arrive without anybody re-enrolling a box.
        #
        # Not yet named in `$defs/health`; proposed as CONTRACT-QUESTIONS.md 16
        # and sent ahead of adoption because that object's own description asks
        # for exactly this ("unknown fields here are expected rather than
        # tolerated: a station that learns to report something new must not
        # have to wait for the platform"). Omitted entirely when unset — never
        # 0, 0 — so "nobody has been to this site" stays distinguishable.
        position = self.reported_position()
        if position is not None:
            payload["position"] = position
        return payload

    def _check_clock(self) -> None:
        """Whether anything is keeping this clock honest.

        `contract/enrolment.md` §6 is about a clock that is *wrong*; this is the
        condition that precedes it. A Pi has no battery-backed clock, so between
        boot and the first NTP exchange its time is whatever the filesystem
        suggested, and a box that never syncs at all is one credential lifetime
        away from a site visit. Reported rather than acted on: sensing and
        recording do not need a correct clock, and enrolling does — which is
        already refused separately.
        """
        state = clock.discipline()
        if state.synchronised is False:
            self.health.raise_condition(
                "clock.unsynchronised", "warning",
                f"The clock is not disciplined by anything ({state.detail}). "
                "This box has no battery-backed clock, so its time is only as "
                "good as its last sync. Check NTP reachability; fit an RTC or a "
                "GPS time source (HARDWARE.md §4).",
            )
        else:
            self.health.clear("clock.unsynchronised")

    def _unsourced_fields(self) -> dict:
        """Fields the console renders for which this station has no sensor."""
        out: dict[str, list[str]] = {}
        for report in self.inventory.report():
            if report.absent and report.telemetry_kind:
                out[report.telemetry_kind] = list(report.absent)
        return out

    def raw_samples(self) -> dict:
        """The last raw line(s) each connected sensor produced, per slot.

        For the setup page's datastream fields, and only there — this is a
        local diagnostic, and putting it in the health frame would spend
        metered bytes repeating what telemetry already carries in structured
        form. Each driver keeps its own bounded tap (`raw_sample()`); a slot
        with no driver, or a driver that is not currently hearing anything,
        is an empty list, which the page renders as an empty field.
        """
        samples: dict[str, list[str]] = {}
        for slot in self.inventory.fitted:
            driver = self.radio if slot == "radio" else self.inventory.drivers.get(slot)
            tap = getattr(driver, "raw_sample", None)
            lines: list[str] = []
            if tap is not None:
                try:
                    lines = [str(line)[:200] for line in list(tap())[:4]]
                except Exception:  # noqa: BLE001 - a diagnostic must not wound
                    lines = []
            samples[slot] = lines
        return samples

    def snapshot(self) -> dict:
        """What the local console shows, in the installer's terms."""
        self._report_capabilities()
        return {
            "enrolled": self.enrolment is not None,
            "station": self.enrolment.site.name if self.enrolment else None,
            "station_id": self.enrolment.station_id if self.enrolment else None,
            "contract_version": CONTRACT_VERSION,
            "version": self.config.version,
            "update": self.updates.state(),
            "broker": redact_url(self.config.broker_url or self.enrolment.broker.url)
            if self.enrolment else None,
            "platform": self.config.platform_url,
            "link": bool(self.transport and self.transport.connected),
            "published": self._published,
            "dropped": self.transport.dropped if self.transport else 0,
            "radio": {
                "fitted": self.radio is not None,
                "freq_mhz": round(self.radio.freq_hz / 1e6, 3) if self.radio else None,
                "squelch_open": self.radio.squelch_open if self.radio else False,
                "monitor": self.radio.monitor if self.radio else False,
                "auto": self.radio.auto_squelch if self.radio else False,
                "threshold_db": round(self.radio.last_threshold_db, 1) if self.radio else None,
                "auto_margin_db": round(self.radio.auto_margin_db, 1) if self.radio else None,
                "hang_s": round(self.radio.hang_seconds, 2) if self.radio else None,
                # The signal meter and the stepped gain control on the setup
                # page's radio tab — the same numbers the platform panel shows.
                "rssi_db": round(self.radio.rssi_db, 1) if self.radio else None,
                "floor_db": round(self.radio.noise_floor_db, 1) if self.radio else None,
                "gain": self.radio.gain if self.radio else None,
                "gains": self.radio.available_gains if self.radio else [],
                "managed_gain_db": (
                    self.radio.managed_gain_db if self.radio else None
                ),
                "ppm": self.radio.ppm if self.radio else 0,
                # Unconditionally here, unlike the telemetry frame, and that is
                # the whole difference: this is served over loopback or the
                # local network to somebody standing at the box, so 128 small
                # integers every 2.5 seconds costs nothing. The metered link
                # is what the demand window on the telemetry side is for.
                "spectrum": self.radio.spectrum_for_display() if self.radio else [],
                "span_hz": self.radio.spectrum_span_hz() if self.radio else 0,
            },
            # The preview fields ride only here: the setup page needs them,
            # the health frame pays for its bytes on a metered link and the
            # platform has the video channel itself.
            "video": {**self.video_state(), **self.video.preview_state()},
            "health": self.health.to_list(),
            "devices": [report.to_dict() for report in self.inventory.report()],
            "resources": [resource.to_dict() for resource in self.inventory.resources()],
            "conflicts": self.inventory.conflicts(),
            "unsourced_streams": sorted(self.inventory.unsourced_streams()),
            "unsourced_fields": self._unsourced_fields(),
            "raw_samples": self.raw_samples(),
            "events": [event.to_dict() for event in self.store.recent_events(15)],
            "storage": self.store.stats(),
            "clock": datetime.now(UTC).isoformat(),
            "clock_source": clock.discipline().to_dict(),
            "security": self.security(),
            "serial_ports": [port.to_dict() for port in self.inventory.serial_ports()],
            "position": self.position_state(),
            "config_version": self.site.version,
        }

    # --- lifecycle ------------------------------------------------------

    def _take_lock(self) -> bool:
        if not self.config.single_instance:
            return True
        import fcntl

        # Two agents on one station publish two independent worlds onto the same
        # channel; the console alternates between them and aircraft teleport.
        # Easy to do by accident and hard to recognise from the outside.
        self._lock_handle = open(self.config.lock_path, "w")
        try:
            fcntl.flock(self._lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.error(
                "Another agent is already running (%s is locked). Stop it first.",
                self.config.lock_path,
            )
            return False
        self._lock_handle.write(str(time.time()))
        self._lock_handle.flush()
        return True

    def another_agent_is_running(self) -> bool:
        """Whether a station service already holds this home's lock.

        For the CLI commands that touch hardware. The sensor lease
        (`camera/ownership.py`) arbitrates within one process and cannot see
        across processes at all — so `gsu camera` run on a box where the
        service is up is two independent openers of one sensor, with nothing
        in between them. That is the one contention path the lease is
        structurally unable to close, and it is closed here instead.

        Read-only: this takes the lock and gives it straight back, so asking
        the question does not answer it differently for the next asker.
        """
        if not self.config.single_instance:
            return False
        import fcntl

        try:
            with open(self.config.lock_path, "a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return True
                fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            # No lock file, or nowhere to put one. Nothing is running.
            return False
        return False

    def _install_signals(self) -> None:
        def handle(signum, _frame):
            log.info("Signal %s: shutting down.", signum)
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handle)
            except ValueError:
                pass  # not the main thread; the caller owns signals

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        # Ordered so nothing is left half-written and, above all, so the radio
        # is stopped gracefully: a dongle killed mid-transfer needs a physical
        # replug, which on an unattended site is a truck
        # (server/docs/05-radio-integration.md obligation 2).
        # Stopped before the drivers are closed: they hold the camera and would
        # otherwise be mid-capture on a device being shut underneath them. The
        # stream first — a `rpicam-vid` left running holds the sensor, and the
        # next start fails with a device-busy that reads like broken hardware.
        # Stop the command worker first. _stop is already set on the normal path;
        # set it here too so a direct shutdown() (the tests) stops it as well. It
        # sits between commands almost always, and is a daemon regardless, so an
        # in-flight stream start cannot hold shutdown up.
        self._stop.set()
        worker, self._command_worker = self._command_worker, None
        if worker is not None:
            worker.join(timeout=2.0)
        self.stream.stop("the station is shutting down")
        self.video.stop()
        self.transcriber.shutdown()
        if self.radio is not None:
            try:
                self.radio.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("Receiver shutdown failed.")
        for driver in self.inventory.drivers.values():
            close = getattr(driver, "close", None)
            if close:
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        if self.renewer is not None:
            self.renewer.stop()
        if self.transport is not None:
            self.transport.stop()
        self.store.close()
        if self._lock_handle is not None:
            try:
                self._lock_handle.close()
            except Exception:  # noqa: BLE001
                pass
        log.info("Stopped.")
