"""Time, and the specific way a wrong clock strands a remote site.

`contract/enrolment.md` §6: a station with a wrong clock cannot authenticate,
and if it believes its credential has already expired it cannot renew either.
That is a site visit, hours away, for a bad number.

Three rules follow, and all three are implemented here rather than assumed:

1. **Refuse to enrol with an implausible clock, and say so.** A box with no
   battery-backed clock boots in 1970 or at its filesystem's build date. Binding
   a credential to a lifetime measured from that is how a station enrols
   successfully and is dead by morning.
2. **The platform is never the only clock authority.** `/api/enrol/status`
   returns `server_time` and it is a *reference*: worth comparing against, worth
   alarming on, never worth silently adopting. A station that took its time from
   the platform would have no idea it was wrong the moment the platform was
   unreachable, which is exactly when it matters.
3. **Say what is keeping the clock honest, or say that nothing is.**
   `discipline()` reports whether time is disciplined and by what — NTP, GPS or
   nothing — and it is published in the health frame, because on an unattended
   box "is your clock synced" is not a question anyone can go and ask.

## The GPS time source, and why there is no GPS code here

The owner intends a GPS receiver to keep time. The right place for that is the
**operating system's clock discipline, not this process**: `gpsd` feeds `chrony`
over SHM, and the PPS line — which is what makes GPS timing worth having, sub-
microsecond rather than the ~100 ms of a serial NMEA sentence — is a kernel
`pps-gpio` device that only `chrony` can use properly. A Python process reading
`$GPRMC` and calling `settimeofday` would be a worse clock than NTP, and would
fight whatever else was disciplining the system.

So the drop-in is deliberate and requires no change here: fit the receiver, wire
PPS, configure chrony, and `discipline()` starts reporting `source: "gps"`
because chrony's reference id becomes `PPS` or `GPS`. The station's own code
path — plausibility check, refuse-to-enrol, health condition — is identical.
That is the design, and DEPLOYMENT.md carries the chrony configuration.
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

log = logging.getLogger("gsu.clock")

#: No credible station clock reads earlier than the release it is running. A
#: value before this means the clock has reset, not that time has moved.
NOT_BEFORE = datetime(2026, 1, 1, tzinfo=UTC)

#: And none reads a decade ahead. Both bounds are wide on purpose: this test
#: exists to catch a reset clock, not to police drift.
MAX_AHEAD = timedelta(days=3652)

#: Skew beyond this against the platform's reference clock is reported as a
#: health condition. Well inside any credential lifetime, so it is a warning
#: long before it is an outage.
SKEW_WARN = timedelta(minutes=5)


def now() -> datetime:
    return datetime.now(UTC)


def implausible_reason(at: datetime | None = None) -> str | None:
    """Why this clock cannot be trusted, or None if it can."""
    at = at or now()
    if at < NOT_BEFORE:
        return (
            f"the clock reads {at.isoformat()}, before this software existed. "
            "Time has not synchronised yet."
        )
    if at > NOT_BEFORE + MAX_AHEAD:
        return f"the clock reads {at.isoformat()}, implausibly far ahead."
    return None


def parse(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from the platform, tolerating `Z` and a
    missing timezone. A naive timestamp is treated as UTC — the platform sends
    UTC, and guessing local here would be worse than assuming."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def skew(server_time: str | datetime | None, at: datetime | None = None) -> timedelta | None:
    """How far this clock is from the platform's, positive if we are ahead."""
    reference = parse(server_time) if isinstance(server_time, str) else server_time
    if reference is None:
        return None
    return (at or now()) - reference


class ClockImplausible(RuntimeError):
    """Raised rather than enrolling. The technician needs to see this."""


# --- what, if anything, is keeping this clock honest ---------------------

#: chrony reference ids that mean a local timing source rather than a network
#: peer. `PPS` is the pulse-per-second line, which is the GPS case worth having;
#: `GPS`/`NMEA`/`SHM` are the serial sentence, better than nothing and about
#: 100 ms accurate. All of them are "gps" as far as an operator is concerned.
GPS_REFERENCE_IDS = ("PPS", "GPS", "NMEA", "SHM", "GPSD")

#: Re-checked at most this often. `chronyc` is a subprocess and this is a
#: 900 MHz ARMv7 core; the answer changes on the scale of minutes.
DISCIPLINE_CACHE_SECONDS = 60.0

_cache_lock = threading.Lock()
_cached: tuple[float, "Discipline"] | None = None


@dataclass(frozen=True)
class Discipline:
    """How the system clock is being kept, as far as this process can tell.

    `synchronised` is deliberately three-valued. False is "nothing is
    disciplining this clock", which is worth an alarm on a box with no RTC.
    None is "could not tell" — an unprivileged container, a distribution using
    neither chrony nor timesyncd — and reporting that as a fault would cry wolf
    on exactly the machines where it is least likely to matter.
    """

    synchronised: bool | None
    source: str            # gps | ntp | rtc-only | none | unknown
    detail: str
    rtc_present: bool

    def to_dict(self) -> dict:
        return {
            "synchronised": self.synchronised,
            "source": self.source,
            "detail": self.detail,
            "rtc_present": self.rtc_present,
        }


def rtc_present() -> bool:
    """Whether the kernel has a real-time clock device.

    False on a bare Pi and True the moment a DS3231 is fitted and its overlay
    enabled, which makes this the cheapest possible check on whether the
    recommendation in HARDWARE.md §4 was actually carried out.
    """
    return os.path.exists("/sys/class/rtc/rtc0")


def _chrony() -> tuple[bool | None, str, str] | None:
    """Ask chrony what it is tracking. None if chrony is not in use."""
    if shutil.which("chronyc") is None:
        return None
    try:
        result = subprocess.run(
            ["chronyc", "-n", "tracking"],
            capture_output=True, text=True, timeout=3.0, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None if isinstance(exc, FileNotFoundError) else (None, "unknown", str(exc))
    if result.returncode != 0:
        return (None, "unknown", "chronyd is installed but not answering")
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    reference = fields.get("Reference ID", "")
    leap = fields.get("Leap status", "")
    stratum = fields.get("Stratum", "")
    # Stratum 0 with reference 00000000 is chrony running and tracking nothing.
    unset = stratum.strip() in ("0", "") or reference.startswith("00000000")
    upper = reference.upper()
    is_gps = any(marker in upper for marker in GPS_REFERENCE_IDS)
    if unset:
        return (False, "none", "chronyd is running but has no source yet")
    synchronised = leap in ("Normal", "Insert second", "Delete second")
    return (
        synchronised,
        "gps" if is_gps else "ntp",
        f"chronyd tracking {reference or 'a peer'} at stratum {stratum or '?'}",
    )


def _timesyncd() -> tuple[bool | None, str, str] | None:
    """systemd-timesyncd, which is what Raspberry Pi OS ships by default.

    It writes a flag file when it has synchronised, so the common case needs no
    subprocess at all — which matters on a box where every wake-up costs power.
    """
    if os.path.exists("/run/systemd/timesync/synchronized"):
        return (True, "ntp", "systemd-timesyncd has synchronised")
    if os.path.exists("/run/systemd/timesync"):
        return (False, "none", "systemd-timesyncd is running but has not synchronised")
    return None


def _timedatectl() -> tuple[bool | None, str, str] | None:
    if shutil.which("timedatectl") is None:
        return None
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=3.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip().lower()
    if value in ("yes", "true"):
        return (True, "ntp", "timedatectl reports the clock synchronised")
    if value in ("no", "false"):
        return (False, "none", "timedatectl reports the clock not synchronised")
    return None


#: `adjtimex(2)` status bit, set when the kernel clock is disciplined by
#: *nothing*. Cleared by whatever is keeping time — chrony, systemd-timesyncd,
#: ntpd, a GPS/PPS refclock — so unlike the daemon probes below it does not
#: depend on which one is in use, or on being able to see it.
STA_UNSYNC = 0x0040

#: adjtimex()'s return value. TIME_ERROR is "the clock is not synchronised"; the
#: other states (TIME_OK and the leap-second ones) all mean it is disciplined.
TIME_ERROR = 5


class _Timex(ctypes.Structure):
    """glibc's `struct timex`, enough of it to read `status`.

    Every field is named so `status` lands at the offset the kernel writes it
    to; the trailing padding matches glibc so the buffer is the size adjtimex
    copies back into. `modes = 0` makes the call a pure query — it needs no
    privilege and sets nothing, which is the whole reason it is safe to run as
    the unprivileged container user.
    """

    _fields_ = [
        ("modes", ctypes.c_int),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("constant", ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("time_sec", ctypes.c_long),
        ("time_usec", ctypes.c_long),
        ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long),
        ("jitter", ctypes.c_long),
        ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long),
        ("jitcnt", ctypes.c_long),
        ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long),
        ("stbcnt", ctypes.c_long),
        ("tai", ctypes.c_int),
        ("_padding", ctypes.c_int * 11),
    ]


def _kernel_synchronised() -> bool | None:
    """Whether the kernel clock is disciplined, from adjtimex(2) directly.

    This is the one sync signal a container can always read: it shares the host
    kernel's clock, and the STA_UNSYNC bit is set by the kernel itself, so it
    reports the truth whether the box runs chrony (invisible from in here —
    `chronyc` is not in the image), timesyncd, or a GPS refclock. `None` on
    anything without this call — the dev machines — where the probes below are
    the only answer anyway. Never raises.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        adjtimex = libc.adjtimex
    except (OSError, AttributeError):
        return None
    adjtimex.restype = ctypes.c_int
    buf = _Timex()
    buf.modes = 0
    try:
        state = adjtimex(ctypes.byref(buf))
    except (OSError, ValueError):
        return None
    if state < 0:
        return None
    return state != TIME_ERROR and not (buf.status & STA_UNSYNC)


def discipline(force: bool = False) -> Discipline:
    """What is keeping this clock, cached because it is asked every health frame.

    Never raises and never blocks for long: this is diagnostic, and a station
    that stalled its sensing loop asking about time would have turned a cosmetic
    gap into a real one.
    """
    global _cached
    with _cache_lock:
        if _cached is not None and not force:
            when, value = _cached
            if time.monotonic() - when < DISCIPLINE_CACHE_SECONDS:
                return value

    rtc = rtc_present()
    kernel = _kernel_synchronised()
    answer: tuple[bool | None, str, str] | None = None
    for probe in (_chrony, _timesyncd, _timedatectl):
        try:
            answer = probe()
        except Exception:  # noqa: BLE001 - diagnostics must not break the loop
            log.debug("Clock probe %s failed.", probe.__name__, exc_info=True)
            answer = None
        if answer is not None:
            break

    if answer is None:
        probe_sync: bool | None = None
        source = "rtc-only" if rtc else "unknown"
        detail = (
            "No chrony or systemd-timesyncd found; cannot tell what is keeping "
            "this clock." + (" A hardware RTC is present." if rtc else "")
        )
    else:
        probe_sync, source, detail = answer

    # The kernel is the authority on *whether* the clock is disciplined; the
    # daemon probes only name *by what*. In a container the probes are half
    # blind — chronyc is not in the image, and timesyncd's flag file belongs to
    # a daemon the box may not even run — so they can report "nothing is keeping
    # this clock" about one chrony is keeping perfectly. Believe the kernel for
    # the alarm, and keep the probe only as the label.
    if kernel is None:
        synchronised: bool | None = probe_sync
    else:
        synchronised = kernel
        if kernel and probe_sync is not True:
            # Disciplined, but not by anything visible from in here. Say that,
            # rather than the probe's "not synchronised", which is simply wrong.
            source = source if source in ("gps", "ntp") else "ntp"
            detail = (
                "the kernel clock is synchronised (adjtimex); what is "
                "disciplining it is not visible from inside the container"
            )

    if source == "none" and rtc and synchronised is not True:
        source = "rtc-only"
        detail += "; a hardware RTC is present, so the time is at least held over a reboot"

    state = Discipline(synchronised, source, detail, rtc)
    with _cache_lock:
        _cached = (time.monotonic(), state)
    return state
