"""Configuration, in two halves that change on completely different clocks.

**Agent configuration** is how this process finds the world: where its state
lives, which platform to enrol against, which broker to use if the enrolment
response cannot be believed. It comes from the environment, is read once at
start, and is the sort of thing a technician or an image sets.

**Site configuration** is everything in `contract/enrolment.md` §7: thresholds,
retention, duty cycling, cadence. It is versioned, persisted, delivered by the
platform on the command channel as `config.set`, and reported back in telemetry.
The station owns the persisted copy — a station that forgets its thresholds when
the link drops is a station whose alerting depends on the platform, which is the
one thing the design forbids.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

#: Where state lives by default. Deliberately inside the checkout while there is
#: no answer to "which compute platform" (contract/enrolment.md §9.1) — see
#: DECISIONS.md. On real hardware this is a persistent partition, not a repo.
DEFAULT_HOME = Path(__file__).resolve().parent.parent / "var"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class AgentConfig:
    """Process-level settings. Read once; changing them means a restart."""

    home: Path = DEFAULT_HOME
    #: The station software version, baked into the image at build time
    #: (GSU_VERSION) and reported so an operator — and the remote updater — can
    #: see what a box is on. "dev" for a local checkout that set nothing.
    version: str = "dev"

    #: Where /api/enrol lives. Only ever used for enrolment, renewal and status;
    #: no telemetry path goes near it.
    platform_url: str = "http://localhost:8000"

    #: Overrides the broker URL the enrolment response carries. Needed whenever
    #: the platform reports an address that is only routable from inside its own
    #: network (a container hostname, say) — the station is by definition
    #: somewhere else. The username and topics from enrolment are still used.
    broker_url: str | None = None

    #: One-shot enrolment code, for a headless first boot. The normal path is a
    #: technician typing it into the setup page (contract/enrolment.md §5).
    enrol_token: str | None = None

    tick_seconds: float = 1.0

    #: Bind the setup page to loopback by default. On real hardware it belongs
    #: on the box's own setup network, which is decision §9.1/§9.2 territory.
    setup_host: str = "127.0.0.1"
    setup_port: int = 8088
    setup_enabled: bool = True

    #: The password an installer types to reach the setup page from the LAN.
    #: Either a plain value or, preferably, the output of
    #: `python -m gsu setup-password` — see `gsu/setup_access.py`.
    #:
    #: **Without one, `setup_host` is ignored and the page binds to loopback.**
    #: That is the whole safety property: there is no code path that opens this
    #: listener to a routable interface without a secret in front of it, so
    #: forgetting to set one produces an unreachable page rather than an open
    #: one. Loopback callers never need it — they arrived over SSH, which has
    #: already authenticated them, and a second secret in front of the first
    #: protects nothing.
    setup_password: str | None = None

    #: How long the LAN listener stays up with no authenticated activity, after
    #: the station is enrolled. **0 is the default: the setup page does not
    #: auto-lock — the listener stays up for as long as the station runs.** The
    #: other three protections are untouched by this and still stand: a password
    #: is required from the LAN, a request from outside the local networks is
    #: refused, and the listener binds loopback-only unless a host and password
    #: are both set. So pinning it open removes the timed close, not the
    #: authentication. Set a positive number of minutes to bring the timed window
    #: back, where on expiry the socket closes and rebinds to loopback rather
    #: than answering 403 — and while the station is still *un*enrolled the
    #: window never runs down regardless, because an installer is still working
    #: and a box with no credential has nothing on it worth reaching.
    setup_window_minutes: float = 0.0

    #: Let a platform admin reach this box's own setup console remotely, down the
    #: station's outbound link (`gsu/transport/console_proxy.py`). **Off by
    #: default, and the default is the safety property**: reaching into a box
    #: remotely is a trust escalation, so a station opens the console socket only
    #: when this is set — the same refuse-to-bind posture `setup_access.py` takes
    #: about the LAN listener. When on, a `console.open` command opens a
    #: time-boxed WebSocket to the platform's `/console/ingest`; the socket
    #: closes itself when the window lapses, and every open is audited on the
    #: platform.
    console_proxy: bool = False

    #: Where the platform's console ingest is, when it is not simply the API's
    #: host. Same reason `GSU_MEDIA_URL` exists — unset, it is derived from
    #: `platform_url` with the scheme switched to WebSocket.
    console_url: str | None = None

    #: Let a platform admin open a shell on this box's **host** remotely
    #: (`gsu/transport/host_shell.py` + the privileged helper in
    #: `deploy/hostshell/`). **Off by default, and the biggest trust escalation
    #: in the station** — a root-capable shell on the host, reached over the
    #: platform. Two gates must both be open: this flag, which lets the agent
    #: write the helper its instructions, and the `hostshell` compose profile,
    #: which is what makes the privileged helper container exist at all. When on,
    #: a `host.open` command writes a time-boxed request the helper acts on, and
    #: every open/close is audited on the platform.
    host_shell: bool = False

    #: Where the platform's host ingest is, when it is not simply the API's host.
    #: Same override reason as `console_url`/`media_url`.
    host_shell_url: str | None = None

    #: Provision this box as a demo station: every slot starts on its Demo
    #: sensor, so it is a complete working station out of the box with no
    #: hardware attached.
    #:
    #: Off by default, because the opposite default costs a real installation
    #: real work — six slots to un-demo, and the alertness to notice they need
    #: it. An untouched slot on a real box reads "Not fitted", which is true.
    #:
    #: **Provisioning, not runtime.** It only seeds a station that has never
    #: been configured; once a device file exists this is ignored entirely, so
    #: setting it later cannot replace somebody's real sensors, and clearing it
    #: later cannot strip a demo box someone is using.
    demo: bool = False

    #: How busy the simulated airband channel is: "off", "low" or "busy".
    #: A rural airband channel is silent the vast majority of the time and the
    #: default reflects that; "busy" is for exercising the audio path.
    airband_traffic: str = "low"

    #: Notate airband transmissions into the event log, on the box, offline.
    #: Off by default: it is CPU work on a small board, and whether recording and
    #: transcribing a channel is permitted is the operator's call and depends on
    #: where the station is. When on, each over the squelch passes is handed to
    #: whisper.cpp on a low-priority thread — live audio always wins, and overs
    #: are dropped rather than queued without bound if the board falls behind.
    radio_transcribe: bool = False
    #: The whisper.cpp binary (on PATH) and the model file for the above. A
    #: missing binary or model turns transcription off with one log line rather
    #: than an error — the station image ships both when the feature is wanted.
    radio_whisper_bin: str = "whisper-cli"
    radio_whisper_model: str | None = None
    #: How many CPU threads whisper.cpp may use.
    #:
    #: TWO, not all of them, and this is a POWER setting rather than a
    #: performance one. whisper.cpp defaults to every core; on a Pi 5 that takes
    #: the SoC to its maximum for the length of each over, and at three or four
    #: overs a minute the board sits at maximum nearly continuously. On
    #: 2026-08-15 Kennels Road logged `Undervoltage detected!` with the core rail
    #: at 7.7 A and stopped executing in the same second — that current is simply
    #: a BCM2712 with four cores saturated, and the supply could not hold it.
    #:
    #: `nice` does NOT help here and the comment in transcribe.py that implies it
    #: does is about latency: a niced process on four cores draws exactly the
    #: same current, it just yields sooner.
    #:
    #: DEFAULT 0 — whisper's own choice, which is every core.
    #:
    #: This shipped as 2 in v0.4.2 and was reverted the same day. The cap worked
    #: exactly as intended on power, and the cost was measured rather than
    #: guessed: a decode window went 12.9 s -> 20.6 s, which stayed inside the
    #: 25 s budget so the selector kept small.en and accuracy was unaffected.
    #: What it did cost was THROUGHPUT — about 2.9 overs a minute against peaks
    #: of 4 — so a busy circuit would start dropping the oldest overs. That is a
    #: bad trade while the transcript is the thing being worked on, and it does
    #: not fix the board anyway: the supply still cannot hold the maximum draw,
    #: and the remedy is the PSU and the cable.
    #:
    #: The dial stays, because it is the right lever if power ever has to win
    #: over throughput. GSU_WHISPER_THREADS=2 restores the cap.
    radio_whisper_threads: int = 0
    #: A directory to keep recent overs in, for building a ground-truth corpus.
    #:
    #: OFF unless set, and it should stay off in normal operation: it writes to
    #: the SD card of an unattended box. Switch it on for a week when a change
    #: to transcription needs measuring, rsync the directory off, hand-label the
    #: sidecars, and switch it off again. See gsu/radio/corpus.py.
    radio_over_capture: str | None = None
    #: An initial prompt biasing whisper toward what this channel carries. Empty
    #: uses the built-in aviation vocabulary (`transcribe.AVIATION_PROMPT`); set
    #: GSU_WHISPER_PROMPT to add local aerodrome names and based-aircraft
    #: registrations a general model will otherwise not get right.
    radio_whisper_prompt: str = ""

    # --- trust (gsu/tls.py), which is two roots and not one ---------------
    #: The **broker's** CA. Normally delivered by the enrolment response as
    #: `broker.ca_pem` and persisted at `ca_path`; this pre-provisions or
    #: overrides it. The broker is always pinned — there is no system-trust
    #: option for it.
    ca_file: str | None = None

    #: The **platform API's** CA, opt-in. Unset, the API is verified against the
    #: system CA bundle, which is right for an API behind a reverse proxy with a
    #: public certificate. Set, the API is pinned to this file — which is right
    #: for a platform serving its own certificate, as it does today.
    api_ca_file: str | None = None

    #: Refuse plaintext on either link even before a CA has ever been seen. Off
    #: by default so the development stack — a local Redis container with no TLS
    #: — still works; **on** in the deployed environment file.
    require_tls: bool = False

    #: Where the platform's media endpoint is, when it is not simply the API's
    #: host. Same reason `GSU_BROKER_URL` exists: the address a platform states
    #: may only be routable from inside its own network, and the station is by
    #: definition somewhere else. Unset, it is derived from `platform_url`.
    media_url: str | None = None

    #: Which H.264 encoder to use: `auto`, `hardware` or `software`.
    #:
    #: A property of the *board*, not of the site, which is why it is here and
    #: not in site configuration: a Pi 2/3/4 has a fixed-function encode block
    #: and a Pi 5 is understood to have dropped it for a faster CPU. `auto`
    #: probes and prefers hardware, and whichever ran is reported in health
    #: telemetry with the frame rate it actually achieved — so moving a station
    #: between boards is a setting and a measurement rather than a rewrite.
    encoder: str = "auto"

    #: Where the live H.264 stream goes while there is no uplink to the
    #: platform: a file, so that whoever first has a Pi can prove the hardware
    #: encoder works without needing a platform at all. Unset in the field —
    #: `gsu/transport/stream.py` explains what replaces it.
    stream_sink: str | None = None

    #: Keep the live encoder — and the camera it reads — running even when no
    #: viewer is attached, so a `video.start` re-attaches the platform in a
    #: socket connect rather than paying the full cold start: an ffprobe against
    #: the camera, the RTSP open, and the wait for the camera's first keyframe,
    #: which together are the ~15 seconds a console waits on a page load. The
    #: uplink stays on-demand — nothing is sent up the metered link while nobody
    #: is watching — so this buys the latency back without spending the bandwidth
    #: the on-demand design exists to save. What it costs is a standing encode:
    #: a remux is cheap, but it is CPU and a little power that never sleeps, and
    #: it holds the camera slot (so a snapshot preview reads as "in use by the
    #: live stream" — the setup page's live view still works, through the same
    #: encoder). Off by default, and meant to be turned on per box where the
    #: board can afford it — a Pi 5 before a Pi 2B.
    video_keep_warm: bool = False

    #: Refuse to start a second agent for the same station. Two instances
    #: publishing independent worlds onto one channel makes the console flicker
    #: between them, which looks like a platform bug rather than an operator
    #: mistake.
    single_instance: bool = True

    @classmethod
    def from_env(cls) -> AgentConfig:
        return cls(
            home=Path(_env("GSU_HOME", str(DEFAULT_HOME))),
            version=_env("GSU_VERSION", "dev"),
            platform_url=_env("GSU_PLATFORM_URL", "http://localhost:8000"),
            broker_url=_env("GSU_BROKER_URL"),
            enrol_token=_env("GSU_ENROL_TOKEN"),
            tick_seconds=float(_env("GSU_TICK_SECONDS", "1.0")),
            setup_host=_env("GSU_SETUP_HOST", "127.0.0.1"),
            setup_port=int(_env("GSU_SETUP_PORT", "8088")),
            setup_enabled=_env("GSU_SETUP", "1") not in ("0", "false", "no"),
            # The hash is preferred and wins if both are set: an environment
            # file that has been migrated to a hash but still carries the old
            # plain line must not silently keep honouring the old line.
            setup_password=_env("GSU_SETUP_PASSWORD_HASH") or _env("GSU_SETUP_PASSWORD"),
            setup_window_minutes=float(_env("GSU_SETUP_WINDOW_MINUTES", "0")),
            console_proxy=_env("GSU_CONSOLE_PROXY", "0")
            not in ("0", "false", "no", ""),
            console_url=_env("GSU_CONSOLE_URL"),
            host_shell=_env("GSU_HOST_SHELL", "0") not in ("0", "false", "no", ""),
            host_shell_url=_env("GSU_HOST_SHELL_URL"),
            demo=_env("GSU_DEMO", "0") not in ("0", "false", "no", ""),
            airband_traffic=_env("GSU_AIRBAND_TRAFFIC", "low"),
            radio_transcribe=_env("GSU_RADIO_TRANSCRIBE", "0")
            not in ("0", "false", "no", ""),
            radio_whisper_bin=_env("GSU_WHISPER_BIN", "whisper-cli"),
            radio_whisper_model=_env("GSU_WHISPER_MODEL"),
            radio_whisper_prompt=_env("GSU_WHISPER_PROMPT", ""),
            radio_over_capture=_env("GSU_OVER_CAPTURE"),
            radio_whisper_threads=int(_env("GSU_WHISPER_THREADS", "0")),
            stream_sink=_env("GSU_STREAM_SINK"),
            video_keep_warm=_env("GSU_VIDEO_KEEP_WARM", "0")
            not in ("0", "false", "no", ""),
            encoder=_env("GSU_ENCODER", "auto"),
            media_url=_env("GSU_MEDIA_URL"),
            single_instance=_env("GSU_SINGLE_INSTANCE", "1") not in ("0", "false"),
            ca_file=_env("GSU_CA_FILE"),
            api_ca_file=_env("GSU_API_CA_FILE"),
            require_tls=_env("GSU_REQUIRE_TLS", "0") not in ("0", "false", "no"),
        )

    # --- paths ----------------------------------------------------------

    def ensure_home(self) -> Path:
        # 0700: the credential lives in here. See credentials.py.
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            self.home.chmod(0o700)
        except OSError:
            pass
        return self.home

    @property
    def credential_path(self) -> Path:
        return self.home / "credential.json"

    @property
    def ca_path(self) -> Path:
        # The **broker's** CA, beside the credential and 0600 like it. They are
        # one identity: the secret says who this box is, the CA says which
        # broker it may say that to. The API's trust root is separate and is
        # never delivered over the wire — see AgentConfig.api_ca_file.
        return self.home / "broker-ca.pem"

    @property
    def site_config_path(self) -> Path:
        return self.home / "site-config.json"

    @property
    def receiver_state_path(self) -> Path:
        # Obligation 3 of server/docs/05-radio-integration.md: gain, ppm and
        # frequency survive a restart. Remote-Radio keeps its own state.json;
        # this is ours.
        return self.home / "receiver.json"

    @property
    def devices_path(self) -> Path:
        # What an installer said is fitted. Intent, not detection — the two are
        # deliberately different records (devices/inventory.py).
        return self.home / "devices.json"

    @property
    def store_path(self) -> Path:
        return self.home / "station.db"

    @property
    def recordings_dir(self) -> Path:
        return self.home / "recordings"

    @property
    def update_dir(self) -> Path:
        """Where a `system.update` request is written for the host-side updater
        to pick up. GSU_UPDATE_DIR points it at a bind-mounted handoff directory
        the host can read directly (DECISIONS.md item 48); unset, it falls back
        under the state directory, which is always writable but only reachable by
        the host through Docker's volume path."""
        override = _env("GSU_UPDATE_DIR")
        return Path(override) if override else self.home / "update"

    @property
    def host_shell_dir(self) -> Path:
        """Where a `host.open` request is written for the privileged host-shell
        helper to pick up. GSU_HOST_SHELL_DIR points it at a bind-mounted handoff
        directory the helper container shares (like GSU_UPDATE_DIR does for the
        updater); unset, it falls back under the state directory."""
        override = _env("GSU_HOST_SHELL_DIR")
        return Path(override) if override else self.home / "hostshell"

    @property
    def lock_path(self) -> Path:
        return self.home / "agent.lock"

    @property
    def setup_reopen_path(self) -> Path:
        # `touch` this and the setup window opens again, once. The deliberate
        # act that stops the setup page being a permanent back door: reaching
        # it after the window has closed needs either a shell on the box or a
        # power cycle, and both are things only somebody with real access to
        # this station can do.
        return self.home / "setup-open"


#: Strings a person or a form means as "off". `bool("false")` is `True`, which
#: would turn "video_enabled": "false" from the platform into video switched on
#: — the sort of thing nobody notices until the bill arrives.
_FALSE = {"0", "false", "no", "off", ""}


def _coerce(target: type, value: object):
    if target is bool and isinstance(value, str):
        return value.strip().lower() not in _FALSE
    return target(value)


def _bounded(label: str, raw: object, low: float, high: float) -> float:
    """A number inside its range, or a ValueError somebody can read.

    Shared by the setup page and by `config.set` so that a position typed on a
    roof and one arriving over the wire are held to the same rule. The message
    is written to be rendered: it is what the technician sees when they type a
    degrees-minutes-seconds string into a decimal-degrees box, which is the
    mistake this actually catches.
    """
    # A coordinate copied off a web page arrives with a typographic minus
    # (U+2212) or an en dash, neither of which float() accepts. Refusing that
    # paste is a refusal the person cannot see the cause of — the two strings
    # look identical in the box.
    text = str(raw).strip().replace("−", "-").replace("–", "-")
    span = f"{low:g} and {high:g}"
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number between {span}.") from None
    if value != value or value in (float("inf"), float("-inf")):
        # NaN and the infinities survive float() and then poison every range
        # and bearing computed from them, silently.
        raise ValueError(f"{label} must be a number between {span}.")
    if not low <= value <= high:
        raise ValueError(f"{label} must be between {span}.")
    return value


def parse_latitude(raw: object) -> float:
    return _bounded("Latitude", raw, -90.0, 90.0)


def parse_longitude(raw: object) -> float:
    return _bounded("Longitude", raw, -180.0, 180.0)


def parse_elevation_m(raw: object) -> float:
    # Metres above the ellipsoid, bounded by the deepest mine and low earth
    # orbit rather than by anything geodetic: the job is to reject a typed
    # latitude or a pasted phone number, not to adjudicate a survey.
    return _bounded("Elevation", raw, -500.0, 100_000.0)


#: Fields that may legitimately be unset, and how to read one when it is not.
#: `apply()` normally coerces with `type(current)`, and `NoneType("-42.4")`
#: raises — so without this a stored position would be silently discarded by
#: the `load()` that is supposed to restore it. Empty and null both clear.
NULLABLE: dict[str, object] = {
    "latitude": parse_latitude,
    "longitude": parse_longitude,
    "elevation_m": parse_elevation_m,
}


@dataclass
class SiteConfig:
    """Versioned site policy. Persisted locally, updated by `config.set`.

    Defaults are deliberately conservative and deliberately *local*: every
    threshold here is one the station must be able to act on with the platform
    unreachable.
    """

    version: int = 0

    #: Where this station is. **The station is the only source of this.** The
    #: platform holds a position too — it arrives in the enrolment response as
    #: `station.latitude`/`station.longitude` — but the owner's decision is that
    #: it must not be settable there, because two editable copies of one fact
    #: disagree and the disagreement is invisible from both ends. So this is
    #: typed on the setup page by somebody standing at the site, reported up in
    #: the health frame, and the platform's copy becomes a display of what was
    #: reported. `agent.effective_position` is the precedence in code.
    #:
    #: Unset is a real state and is reported as absent rather than as 0, 0 —
    #: the Gulf of Guinea is a place, and a fleet map that quietly draws every
    #: unconfigured station there looks like data instead of a gap.
    latitude: float | None = None
    longitude: float | None = None
    #: Metres. No platform equivalent at all: it exists here because range to
    #: an aircraft is slant range and the station's own height is the term
    #: nobody can supply remotely.
    elevation_m: float | None = None

    #: Proximity alert. The station decides, because the threshold belongs with
    #: the site (contract/schemas/telemetry.schema.json, aircraft.alert).
    alert_range_km: float = 12.0
    alert_altitude_m: float = 1500.0
    #: Degrees either side of a direct course for the site. A contact that is
    #: near and low but tracking AWAY is not a proximity event, and counting it
    #: as one means roughly half the alerts under a transit lane are about
    #: aircraft that have already gone. Widen it toward 180 to get the old
    #: behaviour back — every contact in the ring, whichever way it is pointing.
    alert_track_tolerance_deg: float = 30.0
    #: Knots. Below this a contact is judged parked rather than approaching and
    #: its heading is not consulted at all — a tower or a mast reports whatever
    #: direction it last faced, and testing that is a coin toss that lands the
    #: same way for ever. Set to 0 to alert on stationary contacts again.
    alert_min_speed_kt: float = 10.0

    #: Transcribe airband transmissions into the event log. The setup page's
    #: switch, live: the agent reads it every sub-tick, so turning it on or off
    #: takes effect without a restart. Only has any effect when the whisper.cpp
    #: binary and model are actually present — the env override
    #: `GSU_RADIO_TRANSCRIBE` and this are OR'd, so either turns it on.
    radio_transcribe: bool = False

    low_battery_pct: float = 20.0
    critical_battery_pct: float = 10.0
    #: Duty cycling: the floodlight is the first load shed, and it is shed by
    #: the station itself rather than by a command that may never arrive.
    shed_light_below_soc_pct: float = 12.0

    wind_alarm_kt: float = 45.0

    #: Local recording retention. Bounded by both, whichever bites first.
    audio_retention_hours: float = 24.0
    audio_retention_mb: float = 200.0
    event_retention_days: float = 30.0
    #: How long airband transcripts are kept on the box, in days. Separate from
    #: `event_retention_days`: transcripts have no sync channel, so they are
    #: pruned by age alone (see `store.prune`), and an operator may want a
    #: different horizon for "what was said" than for the rest of the log. Zero
    #: keeps them until cleared by hand from the setup page.
    transcript_retention_days: float = 30.0

    #: Cadence, from contract/transport.md. A site may need to differ.
    weather_period_s: float = 5.0
    health_period_s: float = 30.0

    #: `video_enabled` is the platform's one lever on the camera: it switches
    #: the setup page's preview off, and with it every capture this station
    #: makes outside a live stream. It is honoured because a `config.set` that
    #: silently did nothing would be worse than a setting that was never
    #: offered.
    #:
    #: `video_fps` is **retained and inert.** It set the rate of the periodic
    #: snapshot channel, which was removed — two readers of one sensor was the
    #: camera wedge, and the platform has the media channel for live video
    #: (CONTRACT-QUESTIONS.md item 17). It is still parsed and still stored so
    #: that a `config.set` from a platform that predates the removal is a
    #: no-op rather than an error, and so a site file written before it does
    #: not fail to load. Nothing reads it. Deleting it is a contract
    #: conversation, not a tidy-up.
    video_enabled: bool = True
    video_fps: float = 2.0

    #: The live H.264 stream, which is a different thing from the snapshots
    #: above and an order of magnitude more expensive. These are the **ceiling**:
    #: `video.start` may ask for less and never for more, because the link is
    #: the site's constraint and not the viewer's choice.
    #:
    #: 1080p30 at 3 Mbit/s is the owner's requirement. Whether the Pi 2B's
    #: hardware encoder sustains it is the largest open hardware question in
    #: this build — HARDWARE.md §9, and `gsu stream` is how it gets answered.
    stream_width: int = 1920
    stream_height: int = 1080
    stream_fps: float = 30.0
    stream_bitrate_kbps: int = 3000
    #: A stream still stops here even if the platform keeps renewing its lease.
    #: Nobody watches a remote site for an hour; something has gone wrong.
    stream_max_minutes: float = 60.0

    def apply(self, patch: dict, version: int | None = None) -> list[str]:
        """Merge a `config.set` payload. Unknown keys are ignored, not rejected:
        a newer platform must be able to talk to an older station."""
        known = {f.name for f in fields(self)} - {"version"}
        changed: list[str] = []
        for key, value in (patch or {}).items():
            if key not in known:
                continue
            current = getattr(self, key)
            if key in NULLABLE:
                # Nullable fields cannot go through `type(current)`: while one
                # is unset that type is NoneType. An out-of-range value from
                # the platform is dropped like any other unusable value —
                # `config.set` is not a channel that reports errors, and the
                # setup page is where a person gets told (console._location).
                try:
                    coerced = (
                        None if value is None or str(value).strip() == ""
                        else NULLABLE[key](value)
                    )
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    coerced = _coerce(type(current), value)
                except (TypeError, ValueError):
                    continue
            if coerced != current:
                setattr(self, key, coerced)
                changed.append(key)
        if version is not None:
            self.version = int(version)
        return changed

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, sort_keys=True))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> SiteConfig:
        config = cls()
        if path.exists():
            try:
                config.apply(json.loads(path.read_text()))
                config.version = int(json.loads(path.read_text()).get("version", 0))
            except (ValueError, OSError):
                # A corrupt config file must not stop a remote station booting;
                # defaults are safe and the platform will resend its version.
                pass
        return config
