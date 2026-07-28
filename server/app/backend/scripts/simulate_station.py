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
import random
import sys
import uuid

import redis
from sqlalchemy import select

from backend.core.config import settings
from backend.database.models.ground_station import GroundStation
from backend.database.session import PrivilegedSessionLocal
from backend.services.airband_demo import AUDIO_RATE, AirbandDemo, channel_floor_db
from backend.realtime.bus import command_channel
from backend.realtime.groups import status_group
from backend.realtime.hub import Hub

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("simulate")

TICK_SECONDS = 1.0
ADSB_RANGE_KM = 80.0

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
            "altitude": round(self.altitude),
            "track": round(track, 1),
            "speed": round(self.speed_kt),
            "range_km": round(self.range_km, 2),
            "bearing": round(bearing, 1),
            # Low and close is what an operator wants flagged. The threshold is
            # arbitrary here; a real deployment would make it configurable per
            # station and feed it from the alerting path, not the display.
            "alert": self.range_km < 12 and self.altitude < 1500,
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

    def tick(self, dt: float) -> list[tuple[str, dict]]:
        self.t += dt
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
        events.append(
            ("telemetry", {
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
            })
        )

        # The floodlight only changes when something commands it. An earlier
        # version toggled it at random to make the panel move, which was a bad
        # idea: on a security console a light switching itself is indistinguishable
        # from a fault, and it sent operators looking for a bug that was not there.
        events.append(("telemetry", {"kind": "light", "on": self.light_on}))

        # Audio only while the gate is open. Airband is silent most of the time,
        # so this is the difference between a continuous 384 kbit/s per listener
        # and almost nothing - which on a metered Starlink link is the whole
        # argument. Base64 in JSON rides the existing fan-out; binary frames and
        # Opus would cut it further and are the obvious next step.
        if rssi > threshold or self.monitor:
            events.append(
                ("audio", {
                    "kind": "audio",
                    "rate": AUDIO_RATE,
                    "pcm": base64.b64encode(AirbandDemo.to_pcm16(audio)).decode(),
                })
            )

        return events


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

    with PrivilegedSessionLocal() as db:
        rows = db.execute(
            select(GroundStation).where(GroundStation.is_active.is_(True))
        ).scalars().all()
        sims = [
            StationSim(s.id, s.organization_id, s.name, s.latitude, s.longitude)
            for s in rows
        ]

    if not sims:
        log.error("No active ground stations. Run seed_dev first.")
        return

    by_id = {str(s.station_id): s for s in sims}
    commands = redis.Redis.from_url(settings.redis_url).pubsub()
    for sim in sims:
        commands.subscribe(command_channel(sim.station_id))

    log.info("Simulating %d station(s). Ctrl-C to stop.", len(sims))

    try:
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
            await asyncio.sleep(TICK_SECONDS)
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
