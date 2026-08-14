"""The uAvionix ping RX Pro, and a simulation that goes through the same code.

The receiver emits `ADSB_VEHICLE` over a serial link. `mavlink.py` does the
framing, the decode and every unit conversion; this holds the contact table,
works out range and bearing from the station's own position, and decides what
the station is entitled to say about the airspace.

**Three states, and telling them apart is the point.**

    absent          nothing has ever arrived on the port. The device is
                    configured and not talking. Publishes *nothing*.
    stalled         it was talking and has stopped. Also publishes nothing.
    present, quiet  frames are arriving and there are no contacts. Publishes an
                    empty list, which honestly means "clear airspace".

An empty `aircraft` array and no telemetry at all mean completely different
things — one is clear sky, one is a dead receiver — and the console can only
distinguish them if the station is disciplined about which it sends. Sending an
empty list from a receiver that has never said anything is the exact failure
this project keeps designing against.

**A contact with no position is not published.** The schema allows null
`latitude`/`longitude` for a Mode S response with no position, but *requires*
`range_km` and `bearing`, which can only be computed from a position. Rather
than invent a range, such contacts are counted and reported in health telemetry;
the contradiction is written up in CONTRACT-QUESTIONS.md.

**Everything the receiver said is forwarded.** This module used to keep six of
the decoder's fields and drop the rest — emitter type, squawk, vertical speed,
time since last contact, the simulated flag and the altitude datum all reached
`mavlink.AdsbVehicle` and stopped there. They are all published now. Nothing is
defaulted on the way through: a field the receiver's validity flag says is
absent stays None from the wire to the payload.

**The proximity alert is judged on the reported barometric altitude.**
Deliberately: the alert is the one thing the station must still get right with
the platform unreachable, so it hangs on the one altitude ADS-B always carries
rather than a second sensor a dead barometer could quietly move the threshold
with. (A local-datum correction was once tried and has since been removed; the
station reports the datum as received and derives nothing from it here.)
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections import deque

from ..sensors import Aircraft, Device, bearing_to
from . import mavlink
from .serialio import ByteSource

log = logging.getLogger("gsu.adsb")

#: How long a contact survives without an update. ADS-B position reports are
#: roughly 1 Hz; a minute of silence means it has left the area or the receiver
#: has stopped hearing it, and either way it should leave the map.
CONTACT_TTL_SECONDS = 60.0

#: Past this with no valid frame at all, the receiver is treated as stalled.
#: The ping RX Pro emits a heartbeat, so silence is a real symptom.
SILENT_AFTER_SECONDS = 10.0

#: How far off a direct course for the station a contact may be and still count
#: as closing. Degrees, either side.
#:
#: Range and altitude alone answer "is something near me", which is not the
#: question. An aircraft that has already passed and is opening is inside the
#: ring for exactly as long as one that is inbound, and it raises exactly the
#: same alert — so on any site under a transit lane, half of every alert is
#: about an aircraft that is leaving. That is the fastest way to teach an
#: operator that this alert means nothing.
#:
#: Thirty degrees is a course, not a pinpoint. It admits an aircraft tracking a
#: little wide of the site — which is most of them, since nothing flies exactly
#: at a point on the ground — and excludes the ones crossing or departing.
ALERT_TRACK_TOLERANCE_DEG = 30.0

#: Below this ground speed a contact is not going anywhere, and its heading
#: means nothing. Knots.
#:
#: A track is only a course while something is moving along it. A parked
#: aircraft, a service vehicle, a tower or a mast reports whatever heading it
#: was last pointing, and the +/-30 degree test then admits it about one time in
#: six — at random, permanently, for a thing that will never move. That is worse
#: than either always alerting or never alerting, because whether a fixed object
#: alarms for ever comes down to which way it happens to face.
#:
#: Ten knots is under any airspeed an aircraft can hold and comfortably above
#: the wander a GPS fix shows while sitting still.
ALERT_MIN_SPEED_KT = 10.0

#: How far a contact must move between two fixes before a course derived from
#: them is worth believing. Kilometres.
#:
#: A bearing between two points a few metres apart is mostly position noise: at
#: a 20 m baseline an ADS-B fix's own error swings the answer through most of
#: the compass. Two hundred metres puts that error a couple of degrees at worst,
#: which is well inside a 30 degree gate.
DERIVED_TRACK_MIN_KM = 0.2

#: How long a contact may sit inside that radius before it is judged STOPPED
#: rather than merely unmeasured. Seconds.
#:
#: DERIVED, not chosen, and that is the point: it is exactly the time the
#: slowest contact this alert cares about takes to cross the baseline above. So
#: "has not moved 200 m in this long" and "is slower than ten knots" are the
#: same statement, and the three constants cannot drift into disagreeing with
#: each other. Change either of the two above and this follows.
STATIONARY_AFTER_S = (DERIVED_TRACK_MIN_KM * 1000.0) / (ALERT_MIN_SPEED_KT * 0.514444)


def relative_bearing(from_deg: float, to_deg: float) -> float:
    """Smallest angle between two compass bearings, 0-180.

    The `((a - b + 180) % 360) - 180` fold is the whole trick, and it is here
    rather than inline because getting it wrong is invisible: a naive
    `abs(a - b)` is correct for 350° against 340° and says 340 for 350° against
    10°, so an aircraft crossing north would fail every test that used it while
    behaving perfectly everywhere else on the compass.
    """
    return abs((from_deg - to_deg + 180.0) % 360.0 - 180.0)


def is_closing(
    bearing_from_station: float,
    track_deg: float | None,
    tolerance_deg: float = ALERT_TRACK_TOLERANCE_DEG,
    speed_kt: float | None = None,
    min_speed_kt: float = ALERT_MIN_SPEED_KT,
) -> bool:
    """Is a contact on `track_deg` heading toward the station?

    `bearing_from_station` is what `bearing_to` returns — the bearing FROM the
    station TO the aircraft. What the aircraft's track has to be compared
    against is the RECIPROCAL of that: the bearing from the aircraft back to the
    station. Comparing the track against the outbound bearing instead is the
    obvious mistake and it inverts the whole feature — it would alert on
    everything departing and stay silent for everything inbound, which is worse
    than no filter at all because it looks like one that works.

    SPEED FIRST, because a heading is only a course while something is moving
    along it. A stationary contact reports whatever direction it last faced, so
    testing that heading is a coin toss weighted six to one — and it lands the
    same way for ever, since the thing never moves. A tower, a mast, a parked
    aircraft: not approaching, whichever way they point.

    UNKNOWN IS NOT ZERO, for either field. Both heading and velocity are
    optional on the wire and plenty of transponders omit them. Treating a
    missing field as "not coming this way" would turn silence in the data into
    silence in the alert, which is the one failure a proximity alert must never
    have. Both gates remove contacts KNOWN to be harmless and never contacts
    merely unaccounted for — and `PingRxAdsb._course_of` narrows "unknown" by
    measuring what the wire did not say, rather than by assuming it.
    """
    if speed_kt is not None and speed_kt < min_speed_kt:
        return False
    if track_deg is None:
        return True
    to_station = (bearing_from_station + 180.0) % 360.0
    return relative_bearing(track_deg, to_station) <= tolerance_deg


class PingRxAdsb:
    def __init__(
        self,
        source: ByteSource,
        latitude: float = 0.0,
        longitude: float = 0.0,
        alert_range_km: float = 12.0,
        alert_altitude_m: float = 1500.0,
        alert_track_tolerance_deg: float = ALERT_TRACK_TOLERANCE_DEG,
        alert_min_speed_kt: float = ALERT_MIN_SPEED_KT,
        port: str = "",
        label: str = "uAvionix ping RX Pro",
    ) -> None:
        self.source = source
        self.lat = latitude
        self.lon = longitude
        self.alert_range_km = alert_range_km
        self.alert_altitude_m = alert_altitude_m
        self.alert_track_tolerance_deg = alert_track_tolerance_deg
        self.alert_min_speed_kt = alert_min_speed_kt
        self.port = port
        self.label = label
        self._parser = mavlink.MavlinkParser()
        self._vehicles: dict[str, tuple[mavlink.AdsbVehicle, float]] = {}
        #: icao -> (lat, lon, monotonic) of the fix a derived course measures
        #: FROM. Deliberately not the previous poll's position — see _course_of.
        self._anchors: dict[str, tuple[float, float, float]] = {}
        #: icao -> (course, ground speed kt) last measured from movement. Held
        #: between legs so a derived heading does not flicker off in the polls
        #: between one baseline being crossed and the next.
        self._derived: dict[str, tuple[float | None, float | None]] = {}
        self._last_frame: float | None = None
        self._failed = False
        self.positionless = 0
        # The last few frames, decoded to one line each, for the setup page's
        # datastream field. Decoded rather than hex: MAVLink bytes tell an
        # installer nothing, "which aircraft just reported" tells them the
        # receiver works. Bounded — a tap, never a history.
        self._raw: deque[str] = deque(maxlen=4)

    def set_site(self, latitude: float, longitude: float) -> None:
        self.lat = latitude
        self.lon = longitude

    def set_thresholds(
        self,
        range_km: float,
        altitude_m: float,
        track_tolerance_deg: float | None = None,
        min_speed_kt: float | None = None,
    ) -> None:
        self.alert_range_km = range_km
        self.alert_altitude_m = altitude_m
        # Optional, so the agent's existing two-argument call keeps working:
        # this is called through getattr on a driver that may be any of several,
        # and a signature change that broke one of them would take the alert out
        # entirely rather than fail loudly.
        if track_tolerance_deg is not None:
            self.alert_track_tolerance_deg = track_tolerance_deg
        if min_speed_kt is not None:
            self.alert_min_speed_kt = min_speed_kt

    # --- reading --------------------------------------------------------

    def pump(self) -> None:
        try:
            data = self.source.read()
        except OSError:
            self._failed = True
            return
        if not data:
            return
        now = time.monotonic()
        for frame in self._parser.feed(data):
            self._last_frame = now
            if frame.msgid != mavlink.MSG_ADSB_VEHICLE:
                if frame.msgid == mavlink.MSG_HEARTBEAT:
                    self._raw.append("HEARTBEAT")
                continue
            vehicle = mavlink.decode_adsb_vehicle(frame.payload)
            self._vehicles[vehicle.icao] = (vehicle, now)
            altitude = (
                f"{vehicle.altitude_m:.0f} m" if vehicle.altitude_m is not None
                else "alt n/a"
            )
            position = (
                f"{vehicle.latitude:.4f},{vehicle.longitude:.4f}"
                if vehicle.latitude is not None and vehicle.longitude is not None
                else "no position"
            )
            # Enough of the new fields to prove on site that they are arriving,
            # and no more: this is four lines on a setup page, not a log.
            # `baro_valid` appears here because it is the one decoded datapoint
            # the contract has nowhere to carry.
            extra = f"E{vehicle.emitter_type} {vehicle.source}"
            if vehicle.squawk is not None:
                extra += f" sq{vehicle.squawk:04d}"
            if vehicle.altitude_type:
                extra += f" {vehicle.altitude_type}"
            if vehicle.baro_valid:
                extra += "+baro"
            self._raw.append(
                f"ADSB_VEHICLE {vehicle.icao} "
                f"{vehicle.callsign or '-'} {position} {altitude} "
                f"{extra} tslc {vehicle.tslc_s}s"
            )

    def _course_of(
        self, vehicle: mavlink.AdsbVehicle, now: float
    ) -> tuple[float | None, float | None]:
        """(track, ground speed) for the ALERT, measured when the wire is silent.

        A transponder that reports no heading is common, and that made the
        closing test unanswerable — so every such contact fell through to
        alerting on range and altitude alone, which is the behaviour the whole
        filter exists to replace. Two position fixes are all a course needs, and
        this receiver already holds a position table.

        HOW THE BASELINE WORKS. One anchor fix is kept per contact and is NOT
        replaced every poll. Replacing it each time would measure the bearing
        between two points a second apart — at 1 Hz that is metres — and the
        answer would be almost entirely position noise, pointing somewhere new
        every second. That is the same randomness the speed gate exists to
        remove, reintroduced one layer down. Instead the anchor stays put until
        the contact has moved DERIVED_TRACK_MIN_KM from it, and only then does
        the far end become the new anchor. The baseline grows until it is long
        enough to mean something, however slowly the contact is moving.

        NOT MOVING IS A MEASUREMENT, NOT AN ABSENCE. A contact reporting neither
        heading nor velocity that never crosses the baseline would pass both
        gates for ever on "unknown" — a mast alerting permanently, the exact case
        gating on speed was meant to stop. So once it has sat inside that radius
        for STATIONARY_AFTER_S, this reports a ground speed of zero: we did not
        fail to learn its speed, we watched it not move.

        The derived values feed the alert decision ONLY and are never written
        into the payload. This module's contract with the console is that a
        field the receiver's validity flag says is absent stays None from the
        wire to the payload — a console cannot tell a derived heading from a
        reported one, and this file does not get to blur that.
        """
        icao = vehicle.icao
        lat, lon = vehicle.latitude, vehicle.longitude
        anchor = self._anchors.get(icao)
        if anchor is None:
            self._anchors[icao] = (lat, lon, now)
            return vehicle.heading_deg, vehicle.speed_kt

        anchor_lat, anchor_lon, anchor_at = anchor
        moved_km, course = bearing_to(anchor_lat, anchor_lon, lat, lon)
        elapsed = max(now - anchor_at, 1e-6)

        if moved_km >= DERIVED_TRACK_MIN_KM:
            # A real leg. Both answers come out of it, and the far end anchors
            # the next one.
            self._derived[icao] = (course, moved_km / (elapsed / 3600.0) / 1.852)
            self._anchors[icao] = (lat, lon, now)
        elif elapsed >= STATIONARY_AFTER_S:
            # Long enough inside the radius to call it stopped. The course is
            # left at whatever was last measured: a contact that stops has not
            # acquired a new heading, and inventing one out of noise is the
            # thing this method exists to avoid.
            previous = self._derived.get(icao, (None, None))
            self._derived[icao] = (previous[0], 0.0)
            self._anchors[icao] = (lat, lon, now)

        track, speed = self._derived.get(icao, (None, None))
        # The wire always wins. This fills gaps; it does not correct anybody.
        return (
            vehicle.heading_deg if vehicle.heading_deg is not None else track,
            vehicle.speed_kt if vehicle.speed_kt is not None else speed,
        )

    def poll(self, dt: float) -> list[Aircraft] | None:
        """Contacts, or None when the receiver is not talking to us."""
        self.pump()
        if self.status not in ("streaming",):
            return None

        now = time.monotonic()
        for icao, (_, seen) in list(self._vehicles.items()):
            if now - seen > CONTACT_TTL_SECONDS:
                del self._vehicles[icao]
                # The derivation state goes with it, or a receiver under a busy
                # circuit accumulates an entry per aircraft it has ever heard
                # and never gives one back.
                self._anchors.pop(icao, None)
                self._derived.pop(icao, None)

        contacts: list[Aircraft] = []
        positionless = 0
        for vehicle, _ in self._vehicles.values():
            if vehicle.latitude is None or vehicle.longitude is None:
                # No position means no range and no bearing, both of which the
                # contract requires. Counted, not invented.
                positionless += 1
                continue
            range_km, bearing = bearing_to(
                self.lat, self.lon, vehicle.latitude, vehicle.longitude
            )
            # For the alert only. The payload below still carries exactly what
            # the receiver said, including None where it said nothing.
            track, speed = self._course_of(vehicle, now)
            contacts.append(
                Aircraft(
                    icao=vehicle.icao,
                    callsign=vehicle.callsign,
                    latitude=vehicle.latitude,
                    longitude=vehicle.longitude,
                    altitude=vehicle.altitude_m,
                    track=vehicle.heading_deg,
                    speed=vehicle.speed_kt,
                    range_km=range_km,
                    bearing=bearing,
                    alert=(
                        range_km < self.alert_range_km
                        and vehicle.altitude_m is not None
                        and vehicle.altitude_m < self.alert_altitude_m
                        # Near and low is not the question — near, low and
                        # COMING THIS WAY is. Without this an aircraft that has
                        # already passed raises the same alert as one inbound,
                        # for as long as it takes to leave the ring.
                        and is_closing(
                            bearing,
                            track,
                            self.alert_track_tolerance_deg,
                            speed,
                            self.alert_min_speed_kt,
                        )
                    ),
                    # Everything below is passed through from the decoder
                    # untouched. Each is either what the receiver said or None
                    # because it said nothing; none of it is defaulted here.
                    altitude_type=vehicle.altitude_type,
                    vertical_speed=vehicle.vertical_speed_ms,
                    emitter_type=vehicle.emitter_type,
                    squawk=vehicle.squawk,
                    seconds_since_contact=vehicle.tslc_s,
                    on_ground=vehicle.on_ground,
                    simulated=vehicle.simulated,
                    source=vehicle.source,
                )
            )
        self.positionless = positionless
        return contacts

    # --- state ----------------------------------------------------------

    @property
    def status(self) -> str:
        if self._failed:
            return "failed"
        if self._last_frame is None:
            return "absent"
        if time.monotonic() - self._last_frame > SILENT_AFTER_SECONDS:
            return "stalled"
        return "streaming"

    def describe(self) -> Device:
        where = f" on {self.port}" if self.port else ""
        detail = f"{self.label}{where}, {self.status}"
        if self._parser.good_frames:
            detail += f", {self._parser.good_frames} frames"
        if self._parser.false_starts:
            # Not "bad frames": these are byte positions that looked like a
            # start byte and did not pan out. 0xFD and 0xFE occur inside the
            # payloads of perfectly good frames, and the parser deliberately
            # tries every candidate rather than trusting the first, so a
            # healthy receiver produces these continuously — 57 of them
            # alongside 38 good frames is what a working link looks like.
            # Called "bad" it read as a corruption rate and invited exactly
            # the wrong conclusion about the hardware.
            detail += f", {self._parser.false_starts} false starts"
        if self.positionless:
            detail += f", {self.positionless} contact(s) with no position"
        return Device(
            id="adsb", kind="adsb-receiver",
            present=self.status == "streaming",
            detail=detail, simulated=False,
        )

    def raw_sample(self) -> list[str]:
        """The last frames, one line each, only while the receiver talks."""
        return list(self._raw) if self.status == "streaming" else []

    def close(self) -> None:
        self.source.close()


# ---------------------------------------------------------------------------


#: The simulated sky, as a table. One row per `ADSB_EMITTER_TYPE` worth
#: rendering, because the console's aircraft icons are chosen from this integer
#: and a simulator that only ever emitted type 0 gives that work nothing to draw
#: against. Between them these cover the whole enumeration except the values
#: MAVLink leaves unassigned (8, 13, 16) and `space` (15), which no station in
#: the Southern Alps is going to see.
#:
#: Each row is (emitter, weight, callsign prefixes, altitude band m, speed band
#: km/h, spawn band km). `surface` rows are static and are deliberately spawned
#: outside the default 12 km alert ring: a point obstacle never moves, and one
#: inside the ring would hold the console in a permanent proximity alert that
#: is a property of the simulator rather than of anything happening.
_PROFILES: tuple[tuple[int, float, tuple[str, ...], tuple[float, float],
                       tuple[float, float], tuple[float, float]], ...] = (
    #  emitter                       weight  callsigns                       altitude m        speed km/h     spawn km
    (3,  4.0, ("ANZ", "JST", "QFA", "VAN"),  (7000.0, 11600.0), (700.0, 900.0),  (80.0, 80.0)),   # large
    (5,  1.5, ("SIA", "UAE", "CPA", "QFA"),  (9000.0, 12000.0), (800.0, 930.0),  (80.0, 80.0)),   # heavy
    (4,  1.0, ("DAL", "UAL"),                (8000.0, 11000.0), (780.0, 900.0),  (80.0, 80.0)),   # high-vortex large
    (2,  3.0, ("ANZ", "ORG", "SDA"),         (2000.0, 7000.0),  (350.0, 560.0),  (80.0, 80.0)),   # small
    (1,  3.0, ("ZKFLY", "ZKJAB", "ZKCUB"),   (600.0, 3000.0),   (150.0, 300.0),  (60.0, 80.0)),   # light
    (7,  2.0, ("RSCU", "HEMS", "PHNZ"),      (150.0, 1500.0),   (150.0, 260.0),  (30.0, 70.0)),   # rotorcraft
    (6,  1.0, ("NZAF", "KIWI"),              (3000.0, 10000.0), (700.0, 1100.0), (80.0, 80.0)),   # highly manoeuvrable
    (9,  1.5, ("GLD", "ZKGSC"),              (800.0, 4200.0),   (80.0, 170.0),   (25.0, 70.0)),   # glider
    (12, 1.0, ("MICRO", "ZKULT"),            (300.0, 1300.0),   (60.0, 150.0),   (20.0, 60.0)),   # ultralight
    (11, 0.6, ("JUMP",),                     (200.0, 4000.0),   (20.0, 60.0),    (15.0, 45.0)),   # parachute
    (10, 0.6, ("BAL", "ZKBAL"),              (300.0, 2600.0),   (8.0, 45.0),     (15.0, 55.0)),   # lighter-than-air
    (14, 1.5, ("UAV", "DRN", "SURV"),        (100.0, 450.0),    (40.0, 130.0),   (14.0, 40.0)),   # UAV
    (0,  2.0, ("", "MODE", "UNK"),           (1000.0, 9000.0),  (300.0, 750.0),  (70.0, 80.0)),   # no information
    (18, 0.8, ("OPS", "TUG"),                (20.0, 60.0),      (0.0, 0.0),      (15.0, 40.0)),   # service surface
    (17, 0.5, ("FIRE", "RSQ"),               (20.0, 60.0),      (0.0, 0.0),      (15.0, 40.0)),   # emergency surface
    (19, 0.8, ("MAST", "TOWER"),             (90.0, 320.0),     (0.0, 0.0),      (15.0, 45.0)),   # point obstacle
)

#: Mode A codes worth seeing. Squawk digits are octal, so 8 and 9 never appear
#: in one — a simulator that emitted 1984 would be teaching the console a shape
#: no transponder can send. The emergencies are in the list because they are the
#: codes anything downstream is most likely to special-case and least likely to
#: have ever been handed one to test with.
_CONSPICUITY_SQUAWKS = (1200, 2000, 3000, 4000)
_EMERGENCY_SQUAWKS = (7500, 7600, 7700)


class _SimulatedPingSource:
    """A byte source that emits real MAVLink frames.

    Deliberately not a shortcut past the parser: the simulated receiver encodes
    `ADSB_VEHICLE` exactly as the hardware does and the driver decodes it with
    the same code, so the units, the flags and the framing are all exercised
    every time the station runs. A simulation that bypassed them would test
    nothing and hide precisely the unit-scaling mistakes that put an aircraft at
    a plausible wrong altitude.

    **It sets the validity flags it means.** Roughly a quarter of contacts carry
    no squawk and no vertical velocity, with the flags clear rather than the
    values zeroed, so that the null-not-zero path downstream is exercised by
    simply running the station rather than only by a test. A simulator whose
    every field is always populated proves the easy half of the contract.
    """

    RANGE_KM = 80.0

    def __init__(self, latitude: float, longitude: float, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self.lat = latitude
        self.lon = longitude
        self._contacts = [self._new_contact() for _ in range(self._rng.randint(3, 6))]
        self._seq = 0
        self._last = time.monotonic()

    def set_site(self, latitude: float, longitude: float) -> None:
        self.lat, self.lon = latitude, longitude

    def _squawk(self) -> int | None:
        """A Mode A code, or None for a contact the receiver got no squawk from."""
        roll = self._rng.random()
        if roll < 0.25:
            return None                                     # flag stays clear
        if roll < 0.45:
            return self._rng.choice(_CONSPICUITY_SQUAWKS)
        if roll > 0.996:
            # Rare on purpose. A code is chosen once and kept for the life of
            # the contact, so at six contacts on screen a 1-in-70 chance would
            # mean something is squawking an emergency most of the time — which
            # trains whoever is watching to ignore it.
            return self._rng.choice(_EMERGENCY_SQUAWKS)
        # Four octal digits, assembled as digits so the integer reads the way
        # the contract wants it: 7700 is 7700, not 0o7700.
        return int("".join(str(self._rng.randint(0, 7)) for _ in range(4)))

    def _new_contact(self) -> dict:
        emitter, _, callsigns, altitude_band, speed_band, spawn_band = self._rng.choices(
            _PROFILES, weights=[row[1] for row in _PROFILES], k=1
        )[0]
        surface = emitter in (17, 18, 19)

        spawn_km = self._rng.uniform(*spawn_band)
        angle = self._rng.uniform(0, 2 * math.pi)
        x = math.sin(angle) * spawn_km
        y = math.cos(angle) * spawn_km
        speed_kmh = self._rng.uniform(*speed_band)
        # Movers are launched roughly inbound so they cross the site; static
        # emitters have no track at all and are given none.
        heading = math.atan2(-x, -y) + self._rng.uniform(-0.7, 0.7)

        prefix = self._rng.choice(callsigns)
        callsign = f"{prefix}{self._rng.randint(100, 999)}" if prefix else ""

        return {
            "icao": self._rng.randint(0, 0xFFFFFF),
            "callsign": callsign,
            "emitter": emitter,
            "x": x, "y": y,
            "vx": 0.0 if surface else math.sin(heading) * speed_kmh / 3600,
            "vy": 0.0 if surface else math.cos(heading) * speed_kmh / 3600,
            "altitude": self._rng.uniform(*altitude_band),
            "speed_ms": speed_kmh / 3.6,
            "surface": surface,
            # Some contacts are heard without a position, which is what the
            # validity flags exist for.
            "positioned": self._rng.random() > 0.1,
            "squawk": self._squawk(),
            # A quarter report no vertical velocity. Of the rest, most are level
            # and a few are climbing or descending at an airliner's rate.
            "climb_ms": (
                None if self._rng.random() < 0.25
                else round(self._rng.choice((0.0, 0.0, 1.0, -1.0)) *
                           self._rng.uniform(2.0, 12.0), 2)
            ),
            # Geometric altitude is the minority report from real traffic, and
            # is the case the barometric correction must decline to touch.
            "geometric": self._rng.random() < 0.15,
            # 978 MHz UAT is a real second source on a dual-band receiver.
            "uat": self._rng.random() < 0.1,
            # How often this contact is heard, which is what tslc measures.
            # Most are 1 Hz; a distant or intermittent one is not. The phase is
            # random because contacts that all report in lockstep make tslc look
            # like a counter with two values rather than a spread, and the
            # console would be built against the wrong shape.
            "heard_every": (interval := self._rng.choice((1.0, 1.0, 1.0, 2.0, 4.0, 7.0))),
            "last_heard": self._rng.uniform(0.0, interval),
        }

    def read(self) -> bytes:
        # One read is one report cycle, rather than being gated on wall-clock
        # time: the driver pumps once per tick, and tying the simulation to the
        # clock would make it behave differently under test than in the field.
        now = time.monotonic()
        dt = min(5.0, max(0.0, now - self._last))
        self._last = now

        out = bytearray()
        # A heartbeat, so "present and quiet" is distinguishable from "absent"
        # even when there is nothing in the sky.
        out += mavlink.build_frame(mavlink.MSG_HEARTBEAT, bytes(9), self._next_seq())

        for contact in self._contacts:
            contact["x"] += contact["vx"] * dt
            contact["y"] += contact["vy"] * dt
            contact["last_heard"] += dt
            if contact["climb_ms"]:
                contact["altitude"] = max(50.0, contact["altitude"] + contact["climb_ms"] * dt)
            # Occasionally a contact simply stops being heard — it has flown
            # behind terrain or left the receiver's range. It stays in the
            # table with tslc climbing, which is the case the contract calls
            # out ("a track still on the map at 30 seconds is a memory") and
            # which nothing would otherwise produce.
            if self._rng.random() < 0.002:
                contact["heard_every"] = math.inf
        self._contacts = [
            c for c in self._contacts
            if math.hypot(c["x"], c["y"]) <= self.RANGE_KM * 1.1
            and c["last_heard"] < 120.0
        ]
        while len(self._contacts) < 3 or (
            self._rng.random() < 0.004 and len(self._contacts) < 9
        ):
            self._contacts.append(self._new_contact())

        for contact in self._contacts:
            # A contact is only re-heard on its own interval. Between times it
            # is still reported — that is what a receiver's contact table does —
            # with tslc counting up, which is the only honest way for the
            # console to know a track has become a memory.
            if contact["last_heard"] >= contact["heard_every"]:
                contact["last_heard"] = 0.0

            flags = (
                mavlink.FLAG_VALID_ALTITUDE
                | mavlink.FLAG_VALID_HEADING
                | mavlink.FLAG_VALID_VELOCITY
                | mavlink.FLAG_SIMULATED
            )
            # A contact with no callsign leaves the flag clear, rather than
            # flagging nine bytes of padding as a valid identifier.
            if contact["callsign"]:
                flags |= mavlink.FLAG_VALID_CALLSIGN
            lat_e7 = lon_e7 = mavlink.INVALID_I32
            if contact["positioned"]:
                flags |= mavlink.FLAG_VALID_COORDS
                lat = self.lat + contact["y"] / 111.0
                lon = self.lon + contact["x"] / (
                    111.0 * max(0.05, math.cos(math.radians(self.lat)))
                )
                lat_e7 = int(lat * 1e7)
                lon_e7 = int(lon * 1e7)

            # Absent values leave their flag clear and their field at the wire
            # sentinel. Setting the flag and sending zero is the bug this whole
            # exercise is about, and the simulator must not model it.
            squawk = contact["squawk"]
            if squawk is not None:
                flags |= mavlink.FLAG_VALID_SQUAWK
            ver_cms = 0x7FFF
            if contact["climb_ms"] is not None:
                flags |= mavlink.FLAG_VERTICAL_VELOCITY_VALID
                ver_cms = int(contact["climb_ms"] * 100)

            altitude_type = (
                mavlink.ALTITUDE_TYPE_GEOMETRIC if contact["geometric"]
                else mavlink.ALTITUDE_TYPE_PRESSURE
            )
            if not contact["geometric"]:
                flags |= mavlink.FLAG_BARO_VALID
            if contact["uat"]:
                flags |= mavlink.FLAG_SOURCE_UAT

            track = (math.degrees(math.atan2(contact["vx"], contact["vy"])) + 360) % 360
            payload = mavlink.encode_adsb_vehicle(
                icao=contact["icao"],
                flags=flags,
                lat_e7=lat_e7,
                lon_e7=lon_e7,
                altitude_mm=int(contact["altitude"] * 1000),
                heading_cdeg=int(track * 100),
                hor_cms=int(contact["speed_ms"] * 100),
                ver_cms=ver_cms,
                callsign=contact["callsign"],
                squawk=squawk or 0,
                emitter_type=contact["emitter"],
                altitude_type=altitude_type,
                # uint8 on the wire, so it saturates rather than wrapping — a
                # contact last heard four minutes ago must not report 5 seconds.
                tslc=min(255, int(contact["last_heard"])),
            )
            out += mavlink.build_frame(mavlink.MSG_ADSB_VEHICLE, payload, self._next_seq())
        return bytes(out)

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    def close(self) -> None:
        pass


class SimulatedPingRx(PingRxAdsb):
    """The driver, fed by the simulated source. Says it is simulated."""

    def __init__(
        self,
        latitude: float = -43.5,
        longitude: float = 172.6,
        alert_range_km: float = 12.0,
        alert_altitude_m: float = 1500.0,
        alert_track_tolerance_deg: float = ALERT_TRACK_TOLERANCE_DEG,
        alert_min_speed_kt: float = ALERT_MIN_SPEED_KT,
    ) -> None:
        source = _SimulatedPingSource(latitude, longitude)
        super().__init__(
            source, latitude=latitude, longitude=longitude,
            alert_range_km=alert_range_km, alert_altitude_m=alert_altitude_m,
            alert_track_tolerance_deg=alert_track_tolerance_deg,
            alert_min_speed_kt=alert_min_speed_kt,
            label="simulated ADS-B receiver (MAVLink)",
        )
        self._source = source

    def set_site(self, latitude: float, longitude: float) -> None:
        super().set_site(latitude, longitude)
        self._source.set_site(latitude, longitude)

    def describe(self) -> Device:
        device = super().describe()
        return Device(
            id=device.id, kind=device.kind, present=device.present,
            detail=device.detail, simulated=True,
        )
