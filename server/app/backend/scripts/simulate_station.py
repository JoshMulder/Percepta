"""Publish plausible telemetry so the console has something live to render.

    docker compose exec -d app python -m backend.scripts.simulate_station

Stands in for the onboard computer until real hardware exists. It publishes
across the real station boundary - `gsu/{station_id}/telemetry` and
`gsu/{station_id}/audio`, per `contract/transport.md` - and subscribes to
`cmd/gsu/{station_id}`. Nothing downstream can tell it from a station, because
as far as the platform is concerned it is one.

That makes it the reference implementation the contract points at: whatever the
station team builds has to behave like this on the same channels.

Development only. It writes no database rows except last_seen_at.
"""

import asyncio
import base64
import json
import logging
import math
import pathlib
import random
import sys
import uuid

import httpx
import redis
from sqlalchemy import select

from backend.core.config import settings
from backend.database.models.ground_station import GroundStation
from backend.database.session import PrivilegedSessionLocal
from backend.scripts import _opus as opus
from backend.services.airband_demo import AUDIO_RATE, AirbandDemo, channel_floor_db
from backend.realtime.bus import command_channel
from backend.realtime.groups import status_group
from backend.realtime.hub import Hub

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("simulate")

TICK_SECONDS = 1.0
ADSB_RANGE_KM = 80.0

#: How often the station describes itself rather than its surroundings. Slow by
#: design - it is state, not a reading - and reported in `health.cadence` so a
#: console derives staleness from what this station actually does.
HEALTH_SECONDS = 30.0

#: Reported in health so a fleet view can tell what is running. Prefixed
#: `simulator-` because a console showing this alongside real stations should
#: never have to guess which it is looking at.
AGENT_VERSION = "1.0"

#: Bins in a spectrum sweep, decimated by peak and rounded to whole dB - the
#: schema caps this at 256, and a canvas cannot render more than a few hundred.
SPECTRUM_BINS = 128
SPECTRUM_SPAN_HZ = 120_000

#: How far below the in-channel noise floor one bin of the spectrum sits.
#:
#: `noise_floor_db` and `threshold_db` are *in-channel* power — the sum over the
#: ~30 bins of a 25 kHz channel — because that is what the squelch compares
#: against. The `spectrum` array is *per-bin* power, which the contract is
#: explicit about, and one bin holds far less than the whole channel: about
#: 10·log10(channel bins) less. A real receiver measured here shows a noise
#: trace ~14 dB under its own floor, which is what leaves room between the
#: trace and the dashed squelch line for a carrier to rise into.
#:
#: The simulator was drawing the bins at the in-channel level instead, so the
#: trace sat right under the threshold and a quiet channel looked like a wall
#: of noise about to open the gate. Measured against a real station's decimated
#: spectrum, the gap is ~14 dB; the exact figure is not load-bearing on a demo,
#: only that the floor sits clearly below the line and a carrier crosses it.
SPECTRUM_BIN_CORRECTION_DB = 14.0

# How far above the measured noise floor AUTO holds the gate. Remote-Radio's
# guidance is a few dB above the floor; 8 is comfortably clear of it without
# missing weak transmissions.
AUTO_SQUELCH_MARGIN_DB = 8.0

# What the RTL2832U actually offers, from Remote-Radio's own gain table.
AVAILABLE_GAINS = [0.0, 9.0, 14.4, 27.7, 37.2, 42.1, 43.4, 49.6]


LOCK_KEY = "percepta:simulator:lock"
# Comfortably longer than a tick, so a live instance always renews in time,
# but short enough that a killed one frees the lock quickly.
LOCK_TTL_SECONDS = 30


class Contact:
    """One aircraft crossing the area, moving in a straight line.

    Tracked in km offsets from the station and converted to lat/lon on the way
    out, because that is what real ADS-B reports and what the map plots.
    """

    #: `ADSB_EMITTER_TYPE`, weighted the way a quiet rural airspace actually
    #: looks rather than uniformly: mostly light and small aircraft, a helicopter
    #: or two, the occasional airliner, and 0 — a transponder that was never
    #: configured with a category — which is common on general aviation and is
    #: the case the console must render as "not set" rather than "unknown type".
    EMITTERS = [0, 0, 0, 1, 1, 1, 1, 2, 2, 3, 5, 6, 7, 7, 9, 10, 11, 12, 14]

    def __init__(self) -> None:
        self.icao = f"{random.randint(0, 0xFFFFFF):06X}"
        self.callsign = random.choice(
            ["ANZ", "JST", "QFA", "VAN", "RSCU", "KIWI", "NZAF"]
        ) + str(random.randint(100, 999))
        angle = random.uniform(0, 2 * math.pi)
        self.x = math.sin(angle) * ADSB_RANGE_KM
        self.y = math.cos(angle) * ADSB_RANGE_KM
        heading = math.atan2(-self.x, -self.y) + random.uniform(-0.7, 0.7)
        speed_kmh = random.uniform(320, 850)
        self.vx = math.sin(heading) * speed_kmh / 3600
        self.vy = math.cos(heading) * speed_kmh / 3600
        self.altitude = random.uniform(600, 11000)
        self.speed_kt = speed_kmh / 1.852

        self.emitter_type = random.choice(self.EMITTERS)
        # **Absent, not zero.** A real receiver attaches a validity flag to each
        # of these and the station preserves the difference all the way to the
        # wire, so a simulator that always sends a number would hide the one
        # case the console most needs to get right — and would let a `?? 0`
        # creep into the console unnoticed. A quarter of contacts send no
        # squawk and no vertical rate, which is about what the real receiver on
        # the bench produces.
        self.squawk = (
            None if random.random() < 0.25
            # Octal digits only. 7500/7600/7700 are real and rare.
            else random.choice([7000, 7000, 1200, 2000, 3000, 7700])
            if random.random() < 0.06
            else int(f"{random.randint(0,7)}{random.randint(0,7)}"
                     f"{random.randint(0,7)}{random.randint(0,7)}")
        )
        self.vertical_speed = (
            None if random.random() < 0.25 else round(random.uniform(-12, 12), 1)
        )
        self.altitude_type = random.choice(["pressure", "pressure", "geometric"])
        self.source = "uat" if random.random() < 0.12 else "adsb"

    def step(self, dt: float) -> None:
        self.x += self.vx * dt
        self.y += self.vy * dt

    @property
    def range_km(self) -> float:
        return math.hypot(self.x, self.y)

    def to_dict(self, lat0: float, lon0: float) -> dict:
        bearing = (math.degrees(math.atan2(self.x, self.y)) + 360) % 360
        track = (math.degrees(math.atan2(self.vx, self.vy)) + 360) % 360
        # Longitude degrees shrink with latitude, which is a large correction at
        # the ~45S these stations sit at - ignoring it would skew every contact
        # noticeably east or west of where it actually is.
        lat = lat0 + self.y / 111.0
        lon = lon0 + self.x / (111.0 * max(0.05, math.cos(math.radians(lat0))))
        return {
            "icao": self.icao,
            "callsign": self.callsign,
            "latitude": round(lat, 5),
            "longitude": round(lon, 5),
            "altitude_m": round(self.altitude),
            "track_deg": round(track, 1),
            "speed_kt": round(self.speed_kt),
            "range_km": round(self.range_km, 2),
            "bearing_deg": round(bearing, 1),
            # Low and close is what an operator wants flagged. The threshold is
            # arbitrary here; a real deployment would make it configurable per
            # station and feed it from the alerting path, not the display.
            "alert": self.range_km < 12 and self.altitude < 1500,
            "altitude_type": self.altitude_type,
            "vertical_speed_ms": self.vertical_speed,
            "emitter_type": self.emitter_type,
            "squawk": self.squawk,
            "seconds_since_contact": round(random.uniform(0.5, 4.0), 1),
            # Unanswerable from ADSB_VEHICLE for airborne categories, so null —
            # the same thing the real receiver reports (CONTRACT-QUESTIONS 19).
            "on_ground": None,
            # Honest about itself. A demo station's contacts are not traffic,
            # and the console says so on the panel.
            "simulated": True,
            "source": self.source,
        }


class StationSim:
    def __init__(
        self, station_id: uuid.UUID, org_id: uuid.UUID, name: str,
        lat: float | None, lon: float | None,
    ) -> None:
        self.station_id = station_id
        self.org_id = org_id
        self.name = name
        self.lat = lat if lat is not None else -43.5
        self.lon = lon if lon is not None else 172.6
        # Held in memory only. A real station persists this and renews it -
        # re-claiming a token on every boot is a simulator shortcut, not the
        # design (contract/enrolment.md section 6).
        self.credential: str | None = None
        self.contacts = [Contact() for _ in range(random.randint(2, 5))]
        self.soc = random.uniform(55, 95)
        self.light_on = False
        self.freq_hz = 118_700_000
        # Manual threshold is an absolute dBFS value the operator set. Auto
        # tracks the noise floor instead, which is the whole difference between
        # the two modes: a fixed threshold drifts out of usefulness as the floor
        # moves, and riding it is what AUTO is for.
        self.manual_threshold_db: float | None = None
        self.auto_squelch = True
        self.monitor = False
        # Last threshold actually applied, so switching out of AUTO can freeze
        # at it rather than jumping.
        self.last_threshold_db = -70.0
        # Fixed, not "auto". Remote-Radio's handover is explicit that the
        # tuner's own AGC desenses badly near strong broadcast transmitters -
        # a stronger signal can make the measured level go *down* - and a
        # ground station's mast-mounted antenna is precisely where that bites.
        self.gain: str | float = 37.2
        self.ppm = 0
        self.sky = "clear"
        # A tipping-bucket gauge reports an accumulating total that resets at
        # local midnight, plus a derived rate. Both matter: the rate says what
        # is happening now, the total says what the ground has already taken.
        self.rain_mm_today = round(random.uniform(0, 6), 1)
        self.rain_rate = 0.0
        # One receiver per station, each with its own broadcast positions, so
        # two stations tuned to the same channel are not lock-step.
        self.radio = AirbandDemo()
        self.wind_dir = random.uniform(0, 360)
        self.t = random.uniform(0, 1000)
        self.was_online = True
        #: Monotonic-ish deadlines, in simulated seconds. Audio and the
        #: spectrum are leased: the platform asks, re-asks while somebody is
        #: there, and silence stops them. A station that streamed either
        #: unasked would be the exact behaviour the leases exist to prevent,
        #: and a reference implementation that did it would teach it.
        self.audio_until = 0.0
        # Held for the life of the simulator, like a real station's:
        # Opus carries prediction state between packets.
        self.opus = opus.Encoder(AUDIO_RATE)
        self.spectrum_until = 0.0
        #: `video.start` is answered honestly rather than ignored: there is no
        #: camera here, and "unavailable, and why" is what a station with no
        #: camera owes the platform. Silence would look like a wedged encoder.
        self.stream_state = "unavailable"
        #: And the same for `video.poster`, for the same reason: a wall asking
        #: this station for a still gets told there is no camera, rather than
        #: getting a tile that stays blank with no explanation anywhere.
        self.poster_reason = "simulated station: no camera"
        #: Whether the platform currently holds a poster lease here. See
        #: the command handler for why this is tracked rather than nailed
        #: to false.
        self.poster_leased = False
        self.config_version = 1
        self.uptime_s = 0.0
        self.health_due = 0.0

    def apply(self, command: dict) -> None:
        """Act on an operator command, exactly as the onboard computer would.

        The console does not update itself when a command is sent; it waits for
        the state to come back on telemetry. So this is what actually makes the
        buttons appear to work, and it is also what would expose a station that
        silently ignored a command.
        """
        kind = command.get("kind")
        if kind == "radio.tune":
            hz = int(command.get("freq_hz", self.freq_hz))
            self.freq_hz = max(108_000_000, min(137_000_000, hz))
        elif kind == "radio.squelch":
            # Setting a threshold by hand implies leaving auto, exactly as
            # moving the slider does on a real receiver.
            self.manual_threshold_db = float(command.get("db", -70))
            self.auto_squelch = False
        elif kind == "radio.auto_squelch":
            want = bool(command.get("on"))
            if not want and self.auto_squelch:
                # Freeze where auto had it, per Remote-Radio's documented
                # behaviour. Leaving manual_threshold_db unset meant the gate
                # carried on riding the floor with AUTO showing off - the
                # control appeared to do nothing.
                self.manual_threshold_db = self.last_threshold_db
            self.auto_squelch = want
        elif kind == "radio.monitor":
            self.monitor = bool(command.get("on"))
        elif kind == "radio.gain":
            self.gain = command.get("gain", "auto")
        elif kind == "radio.ppm":
            self.ppm = int(command.get("ppm", 0))
        elif kind == "light.set":
            self.light_on = bool(command.get("on"))
        elif kind == "radio.audio":
            # Leased, and a repeat is a renewal rather than a second start.
            # There is no "off": the platform stops asking and the lease runs
            # out, which is the only version that survives the platform
            # crashing rather than merely closing a tab.
            lease = command.get("lease_seconds", 30)
            try:
                lease = max(5.0, min(300.0, float(lease)))
            except (TypeError, ValueError):
                lease = 30.0
            self.audio_until = self.t + lease
        elif kind == "radio.spectrum":
            # Same shape, shorter window: re-requested while a console has the
            # display open, so closing the tab stops the traffic without
            # anybody having to say goodbye.
            self.spectrum_until = self.t + 12.0 if command.get("on", True) else 0.0
        elif kind == "video.start":
            # Reported, not assumed. A station with no camera says so and says
            # why; it does not go quiet, because quiet is what a broken
            # encoder looks like.
            self.stream_state = "unavailable"
        elif kind == "video.stop":
            self.stream_state = "unavailable"
        elif kind in ("video.poster", "video.poster_stop"):
            # Accepted and answered, never acted on. A simulated station has no
            # camera to photograph, and the honest report of that is a poster
            # state carrying the reason — the same rule `video.start` follows
            # two branches up. Ignoring it would leave a developer's wall
            # showing blank tiles with nothing anywhere saying why.
            #
            # `leased` tracks the command, because the contract names it as the
            # observable for `video.poster` and a field that is always false is
            # not an observable — a lease that never registers here would let a
            # broken demand path look identical to a working one against the
            # only station most development ever runs against.
            self.poster_leased = kind == "video.poster"
            self.poster_reason = (
                "simulated station: no camera"
                if kind == "video.poster"
                else "stopped by the platform"
            )
        elif kind == "config.set":
            # Apply what is recognised, persist, and report the new version in
            # health. The platform never assumes the change took.
            version = command.get("version", command.get("config_version"))
            if isinstance(version, int):
                self.config_version = version

    def tick(self, dt: float) -> list[tuple[str, dict]]:
        self.t += dt
        self.uptime_s += dt
        events: list[tuple[str, dict]] = []

        for contact in self.contacts:
            contact.step(dt)
        self.contacts = [c for c in self.contacts if c.range_km <= ADSB_RANGE_KM * 1.1]
        while len(self.contacts) < 2 or random.random() < 0.004:
            self.contacts.append(Contact())
            if len(self.contacts) > 8:
                break
        events.append(
            ("telemetry", {
                "kind": "adsb",
                "aircraft": [c.to_dict(self.lat, self.lon) for c in self.contacts],
            })
        )

        # Solar follows a crude day cycle; the battery charges while the sun is
        # up and drains after dark, which is the behaviour the power panel and
        # its low/critical thresholds exist to show.
        day = (math.sin(self.t / 240) + 1) / 2
        pv_w = round(day * 780, 1)
        load_w = round(120 + (60 if self.light_on else 0) + random.uniform(-8, 8), 1)
        net = pv_w - load_w
        self.soc = max(5.0, min(100.0, self.soc + net * dt / 40000))
        events.append(
            ("telemetry", {
                "kind": "power",
                "soc_pct": round(self.soc, 1),
                "battery_v": round(48.0 + (self.soc - 50) * 0.042, 2),
                "pv_w": pv_w,
                "load_w": load_w,
                "runtime_h": None if net > 0 else round(self.soc * 0.42, 1),
            })
        )

        if int(self.t) % 5 == 0:
            # Sky follows humidity and visibility rather than being independent
            # of them, so the icon never contradicts the numbers beside it.
            humidity = 62 + 14 * math.sin(self.t / 130)
            visibility = max(2, 30 - 12 * abs(math.sin(self.t / 200)))
            if visibility < 5:
                self.sky = "fog"
            elif humidity > 82:
                self.sky = "rain"
            elif humidity > 72:
                self.sky = "cloudy"
            elif humidity > 62:
                self.sky = "partly"
            else:
                self.sky = "clear"

            # Only raining when the sky says so, so the gauge never contradicts
            # the icon beside it.
            self.rain_rate = (
                round(random.uniform(0.4, 7.5), 1) if self.sky == "rain" else 0.0
            )
            # Five seconds of that rate, in mm.
            self.rain_mm_today += self.rain_rate * (5 / 3600)

            events.append(
                ("telemetry", {
                    "kind": "weather",
                    "wind_kt": round(8 + 6 * math.sin(self.t / 90) + random.uniform(-1, 1), 1),
                    "gust_kt": round(12 + 8 * math.sin(self.t / 70) + random.uniform(0, 3), 1),
                    # Backs and veers slowly rather than jumping, so the compass
                    # needle moves the way a real one does.
                    "wind_dir_deg": round((self.wind_dir + 18 * math.sin(self.t / 260)) % 360, 1),
                    "temperature_c": round(9 + 6 * day + random.uniform(-0.4, 0.4), 1),
                    "humidity_pct": round(62 + 14 * math.sin(self.t / 130), 0),
                    "pressure_hpa": round(1013 + 9 * math.sin(self.t / 400), 1),
                    "sky": self.sky,
                    "is_day": day > 0.35,
                    "rain_rate_mmh": round(self.rain_rate, 1),
                    "rain_mm_today": round(self.rain_mm_today, 1),
                    "visibility_km": round(max(2, 30 - 12 * abs(math.sin(self.t / 200))), 1),
                })
            )

        # Airband is quiet most of the time; the squelch opens in bursts. That
        # ratio is the whole argument for squelch-gating the audio uplink.
        # Signal and audio both come from the simulated receiver now, so the
        # meter, the gate and what you hear cannot disagree with each other.
        audio, rssi = self.radio.block(self.freq_hz, int(AUDIO_RATE * dt))
        noise = channel_floor_db(self.freq_hz) + random.uniform(-1.0, 1.0)
        if self.auto_squelch or self.manual_threshold_db is None:
            threshold = noise + AUTO_SQUELCH_MARGIN_DB
        else:
            threshold = self.manual_threshold_db
        self.last_threshold_db = threshold
        radio = {
            "kind": "radio",
            "freq_hz": self.freq_hz,
            "rssi_db": round(rssi, 1),
            "noise_floor_db": round(noise, 1),
            "threshold_db": round(threshold, 1),
            # Gated on the threshold rather than announced independently, so
            # a badly set squelch visibly stops opening on real traffic -
            # which is the behaviour an operator is adjusting against.
            "squelch_open": rssi > threshold or self.monitor,
            "monitor": self.monitor,
            "auto_squelch": self.auto_squelch,
            "gain": self.gain,
            "gains": AVAILABLE_GAINS,
            "ppm": self.ppm,
            "tx_capable": False,
        }
        # Only while a console has asked for it. Sending it continuously is
        # tens of megabytes a day on a metered link for a display that is open
        # for minutes at commissioning, which is why it is leased like audio.
        if self.t < self.spectrum_until:
            centre = SPECTRUM_BINS // 2
            # Per-bin, not in-channel — see SPECTRUM_BIN_CORRECTION_DB. The
            # noise trace sits ~14 dB below the floor the meter reports, which
            # is where a real one sits and what makes the squelch line readable.
            per_bin_floor = noise - SPECTRUM_BIN_CORRECTION_DB
            # The carrier, when there is one, is drawn at the in-channel level
            # so its peak crosses the dashed threshold line exactly when the
            # squelch opens — the one thing this display exists to show. No peak
            # on a quiet channel, so the trace is flat noise between overs.
            peak = rssi if rssi > threshold else per_bin_floor
            radio["spectrum"] = [
                round(
                    (peak if abs(i - centre) < 3 else per_bin_floor)
                    + random.uniform(-2.0, 2.0)
                )
                for i in range(SPECTRUM_BINS)
            ]
            radio["span_hz"] = SPECTRUM_SPAN_HZ
        events.append(("telemetry", radio))

        # The floodlight only changes when something commands it. An earlier
        # version toggled it at random to make the panel move, which was a bad
        # idea: on a security console a light switching itself is indistinguishable
        # from a fault, and it sent operators looking for a bug that was not there.
        events.append(("telemetry", {"kind": "light", "on": self.light_on}))

        # Audio needs BOTH gates, and the contract marks both Required.
        #
        #   1. the squelch is open - airband is silent most of the time
        #   2. somebody is listening - the platform leases it and re-asks
        #
        # This used to test only the first, which meant the reference
        # implementation streamed 512 kbit/s to nobody whenever the band was
        # busy, and modelled for anyone copying it exactly the behaviour the
        # lease was added to prevent. A station that has never been asked sends
        # no audio at all.
        #
        # Opus, raw packets, no container — `contract/schemas/audio.schema.json`.
        # This used to send base64 PCM16, which contract 2.0 deleted: about
        # 384 kbit/s per listener against 16–24 for Opus, on a link somebody
        # pays for by the gigabyte. Being wrong here is worse than being wrong
        # elsewhere, because `contract/NOTES.md` designates this the reference
        # implementation and anybody copying it inherits the mistake.
        #
        # At least four packets per frame, which the schema requires: fewer is
        # a JSON envelope per 80 ms of speech, and most of the saving spent on
        # punctuation.
        if (rssi > threshold or self.monitor) and self.t < self.audio_until:
            packets = self.opus.encode(AirbandDemo.to_pcm16(audio))
            if len(packets) >= 4:
                events.append(
                    ("audio", {
                        "kind": "audio",
                        "codec": "opus",
                        "rate": AUDIO_RATE,
                        "channels": 1,
                        "frame_ms": opus.FRAME_MS,
                        "packets": [base64.b64encode(p).decode() for p in packets],
                    })
                )

        if self.t >= self.health_due:
            self.health_due = self.t + HEALTH_SECONDS
            events.append(("telemetry", self.health()))

        return events

    def health(self) -> dict:
        """What the station says about itself, on the slow cadence.

        Here because the contract tells consumers to read `health.cadence`
        rather than assume the table in transport.md, and a reference
        implementation that never sent a health frame left the one shape a
        console most needs to handle without an example.
        """
        return {
            "kind": "health",
            "status": "ok",
            "agent_version": f"simulator-{AGENT_VERSION}",
            "config_version": self.config_version,
            "uptime_s": round(self.uptime_s, 1),
            # What this station is actually running, which is what a console
            # must derive staleness from - not the defaults.
            "cadence": {
                "adsb": TICK_SECONDS,
                "power": TICK_SECONDS,
                "radio": TICK_SECONDS,
                "light": TICK_SECONDS,
                "weather": 5.0,
                "health": HEALTH_SECONDS,
            },
            "uplink": {"connected": True, "dropped_frames": 0, "offline_seconds": 0},
            # The station is the author of its own position (enrolment.md §7).
            "position": {
                "latitude": self.lat,
                "longitude": self.lon,
                "source": "configured",
            },
            "devices": [
                {"slot": "adsb", "status": "present", "simulated": True},
                {"slot": "radio", "status": "present", "simulated": True},
                {"slot": "weather", "status": "present", "simulated": True},
                {"slot": "power", "status": "present", "simulated": True},
                {"slot": "light", "status": "present", "simulated": True},
                # No camera, and it says so rather than being absent from the
                # list: "never fitted" and "failed" are different facts and an
                # operator does different things about each.
                {"slot": "camera", "status": "not_fitted"},
            ],
            "video": {
                "stream": {
                    "state": self.stream_state,
                    "reason": "this is a simulator and has no camera",
                },
                "poster": {
                    "leased": self.poster_leased,
                    "reason": self.poster_reason,
                },
            },
        }


async def ensure_enrolled(sim) -> bool:
    """Take the station through the real enrolment flow.

    This function plays three parts that are three different people in reality,
    and it is worth being explicit about which is which, because only the last
    is the station team's to build.

      admin      issues an enrolment token for the station record
      technician carries that token to the box
      station    claims it, and keeps the credential

    Only the third step is what real hardware does, and it is done here the way
    hardware must: an HTTP POST to /api/enrol carrying the token, with the
    credential kept in memory afterwards. The first two are shortcut through the
    service layer because there is no admin sitting in a development stack.

    Re-enrols every run. A real station stores its credential and renews it
    rather than claiming a fresh token each boot - see contract/enrolment.md.
    """
    from backend.services import enrolment as enrolment_service

    with PrivilegedSessionLocal() as db:
        station = db.get(GroundStation, sim.station_id)
        if station is None:
            return False
        # --- admin: issue a code ---
        _, token = enrolment_service.issue_token(
            db, station=station, issued_by_user_id=None
        )
        # A record already in service would reject a claim from a second box, so
        # clear the enrolment the previous run left behind.
        station.enrolled_at = None
        enrolment_service.revoke_credentials(
            db, station_id=station.id, reason="simulator-restart"
        )
        db.commit()

    # --- station: claim it ---
    url = f"{settings.simulator_enrol_url}/api/enrol"
    body = {
        "token": token,
        "hardware": {
            "model": "percepta-simulator",
            "serial": str(sim.station_id)[:8],
            "os": "container",
            "agent_version": "0.1",
        },
    }
    for attempt in range(10):
        try:
            # Verifies the API against the same CA a real station pins, rather
            # than skipping verification because it is talking to itself. A
            # development shortcut here would be a shortcut the reference
            # implementation appears to endorse.
            async with httpx.AsyncClient(
                timeout=10.0,
                verify=settings.tls_ca_file
                if pathlib.Path(settings.tls_ca_file).exists()
                else True,
            ) as client:
                response = await client.post(url, json=body)
            if response.status_code == 200:
                data = response.json()
                sim.credential = data["credential"]["secret"]
                log.info(
                    "%s enrolled; credential expires %s.",
                    sim.name, data["credential"]["expires_at"],
                )
                return True
            log.warning(
                "Enrolment for %s returned %s: %s",
                sim.name, response.status_code, response.text[:200],
            )
            return False
        except Exception as exc:
            # The API may still be starting; this runs seconds after boot.
            log.info("Waiting for the enrolment endpoint (%s).", exc)
            await asyncio.sleep(2)
    return False


async def publish_station(hub: Hub, station_id, stream: str, payload: dict) -> None:
    """Publish as a station does - see contract/transport.md."""
    if hub.bus is not None and hub.bus._redis is not None:
        await hub.bus._redis.publish(
            f"gsu/{station_id}/{stream}", json.dumps(payload)
        )


async def run() -> None:
    # Refuse to start twice. Every instance publishes its own independent fleet
    # to the same channels at the same rate, so a second copy does not double
    # the data - it makes the console alternate between two different worlds,
    # and aircraft appear to teleport. Easy to do by accident with `exec -d`,
    # and confusing enough from the outside to look like a bug in the console.
    lock = redis.Redis.from_url(settings.redis_url)
    try:
        if not lock.set(LOCK_KEY, "1", nx=True, ex=LOCK_TTL_SECONDS):
            log.error(
                "Another simulator is already running (Redis key %s).\n"
                "Stop it first, or clear the lock with:\n"
                "  docker compose exec redis redis-cli del %s",
                LOCK_KEY, LOCK_KEY,
            )
            return
    except Exception as exc:
        log.error("Could not reach Redis to take the simulator lock: %s", exc)
        return

    hub = Hub()
    await hub.start()
    if hub.bus is None:
        log.error("Realtime bus is not available; the console would see nothing.")
        return

    excluded = {
        part.strip().lower()
        for part in settings.simulator_exclude_stations.split(",")
        if part.strip()
    }

    # Drives only stations already marked synthetic, and never writes the flag.
    #
    # This was the other way round - every active station except a deny-list -
    # and it did real damage: it adopted a station created for actual hardware,
    # revoked the enrolment code waiting for that hardware, issued and claimed
    # its own, and took a credential. A deny-list is the wrong shape here. It
    # fails open, so every station anyone creates from now on is hijacked by
    # default until somebody remembers to add a UUID to a `.env` on the server,
    # and the person creating it is usually not that somebody.
    #
    # The flag is set in the console, per station, by whoever knows whether a
    # site is real. The simulator reading it and not writing it is what makes
    # that checkbox mean something - it used to overwrite the operator's answer
    # on every run.
    with PrivilegedSessionLocal() as db:
        rows = db.execute(
            select(GroundStation).where(
                GroundStation.is_active.is_(True),
                GroundStation.is_simulated.is_(True),
            )
        ).scalars().all()
        sims = [
            StationSim(s.id, s.organization_id, s.name, s.latitude, s.longitude)
            for s in rows
            if str(s.id).lower() not in excluded
        ]
        skipped = [s.name for s in rows if str(s.id).lower() in excluded]

    if skipped:
        log.info("Leaving alone (SIMULATOR_EXCLUDE_STATIONS): %s", ", ".join(skipped))

    if not sims:
        log.error(
            "No stations are marked as simulated. This drives only stations "
            "with 'This station's data is synthetic' set - tick it in Settings "
            "> Stations, or run seed_dev to create the demo fleet."
        )
        return

    log.info("Driving %d simulated station(s): %s",
             len(sims), ", ".join(s.name for s in sims))

    for sim in sims:
        if not await ensure_enrolled(sim):
            log.error(
                "Station %s could not enrol; the ingest will drop everything "
                "it publishes.", sim.name,
            )
            return

    by_id = {str(s.station_id): s for s in sims}
    commands = redis.Redis.from_url(settings.redis_url).pubsub()
    for sim in sims:
        commands.subscribe(command_channel(sim.station_id))

    log.info("Simulating %d station(s). Ctrl-C to stop.", len(sims))

    try:
        next_tick = asyncio.get_running_loop().time()

        while True:
            # Drain any commands that arrived since the last tick. Non-blocking,
            # so a quiet channel costs nothing.
            while True:
                message = commands.get_message(ignore_subscribe_messages=True)
                if not message:
                    break
                try:
                    channel = message["channel"]
                    if isinstance(channel, bytes):
                        channel = channel.decode()
                    # cmd/gsu/{station_id} - the station id is the last segment.
                    station = by_id.get(channel.rsplit("/", 1)[-1])
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    if station:
                        station.apply(json.loads(data))
                    else:
                        # Silence here once cost a full conformance run: the
                        # channel format changed and every command was dropped
                        # without a trace.
                        log.warning("Command on unrecognised channel %r.", channel)
                except Exception:
                    log.warning("Ignoring malformed command.", exc_info=True)

            for sim in sims:
                for stream, payload in sim.tick(TICK_SECONDS):
                    # Out through the station boundary, exactly as hardware
                    # would: the payload alone, on a channel named for this
                    # station, saying nothing about which org it belongs to.
                    await publish_station(hub, sim.station_id, stream, payload)
                    if payload.get("kind") == "power" and payload["soc_pct"] < 20:
                        await hub.publish(
                            status_group(sim.org_id),
                            hub.status_message(
                                sim.station_id,
                                {"alarm": f"Battery low ({payload['soc_pct']:.0f}%)",
                                 "severity": "warning"},
                            ),
                        )

            # Renew the lock. It carries a TTL so a killed instance releases it
            # on its own rather than needing a manual clear.
            try:
                lock.expire(LOCK_KEY, LOCK_TTL_SECONDS)
            except Exception:
                pass

            # last_seen_at is written by the ingest, not here: a station has
            # no business reaching into the platform's database, and the ingest
            # is what actually observes whether traffic is arriving.
            #
            # Paced against an absolute deadline, not `sleep(TICK_SECONDS)`.
            # Sleeping a fixed second AFTER the tick's work made the true
            # period one second plus the work - measured at 1.027s - while
            # every frame carries exactly one second of audio. A player fed
            # 1.000s of sound every 1.027s starves by 27ms per second and
            # drops out on a regular cycle, which is exactly how it presented:
            # rhythmic in-out-in on the demo radio, and nothing wrong with
            # either the player or the receiver model.
            next_tick += TICK_SECONDS
            now = asyncio.get_running_loop().time()
            if next_tick < now - 5 * TICK_SECONDS:
                # Hopelessly behind (a paused laptop, a debugger): jump rather
                # than fast-forwarding through a backlog of silent ticks.
                next_tick = now
            await asyncio.sleep(max(0.0, next_tick - now))
    finally:
        await hub.stop()
        try:
            commands.close()
        except Exception:
            pass
        try:
            lock.delete(LOCK_KEY)
        except Exception:
            pass


def main() -> int:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
