"""H.264: what a station produces, and how it is cut into access units.

The live stream is H.264 because nothing else fits. A 1080p JPEG is 200-400 kB;
thirty of those a second is 50-100 Mbit/s, which is not a tuning problem on a
satellite link, it is the wrong format. The same picture as H.264 is 2-4 Mbit/s.
`contract/schemas/video.schema.json` says so itself: if this ever needs smooth
full-rate video, MJPEG should be replaced rather than tuned.

**The station never encodes H.264 itself.** Its job is to start the encoder,
read what comes out, and cut it into access units without ever looking inside a
macroblock. Which encoder does the work is discovered rather than assumed and is
reported in telemetry: a Pi 2/3/4 has a fixed-function encode block, and a Pi 5
is understood to have dropped it in exchange for a CPU that can run x264. Those
are opposite ends of the same interface, so both are implementations of it —
`HardwareEncoder` and `SoftwareEncoder` below, chosen by `choose_encoder()`.

What comes out of `rpicam-vid -o -` is an **Annex B byte stream**: NAL units
separated by `00 00 01` or `00 00 00 01` start codes, parameter sets inline at
every keyframe when `--inline` is given. That is the cheapest thing the Pi can
produce and the only thing it should be asked to produce — asking for fragmented
MP4 means muxing on the CPU that is already running the station.

This file deliberately contains no transport. What the platform wants on the
wire is `gsu/transport/stream.py`, and it is a stub until the platform says.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .. import clock

log = logging.getLogger("gsu.h264")

#: The capture tools, best first — `rpicam-vid` on Bookworm, `libcamera-vid` on
#: anything older, and both names exist on current images.
VID_TOOLS = ("rpicam-vid", "libcamera-vid")

#: NAL unit types that matter here. Everything else is passed through untouched:
#: this station is a pipe, not a decoder.
NAL_SPS = 7
NAL_PPS = 8
NAL_IDR = 5
NAL_NON_IDR = 1
NAL_SEI = 6
NAL_AUD = 9

#: How long to wait for the first frame before calling the camera dead. A cold
#: libcamera pipeline takes a second or two; ten is generous and bounded.
FIRST_FRAME_TIMEOUT_S = 10.0


@dataclass
class AccessUnit:
    """One frame's worth of NAL units, with when it was captured.

    `captured_at` is stamped when the last NAL of the access unit is read, which
    is as close to the exposure as this side of the process boundary can get.
    The same rule as the snapshot path: it is the age of the picture, not the
    age of the packet, and an operator assumes it either way.
    """

    data: bytes
    captured_at: object
    keyframe: bool = False
    #: Parameter sets seen so far, so a viewer joining mid-stream can be sent
    #: them without waiting for the next keyframe.
    parameter_sets: bytes = b""

    @property
    def bytes(self) -> int:
        return len(self.data)


def nal_type(nal: bytes) -> int:
    """The type of one NAL unit, given without its start code."""
    return nal[0] & 0x1F if nal else 0


def split_annexb(data: bytes) -> list[bytes]:
    """Annex B byte stream → NAL units, start codes removed.

    Written out rather than pulled from a library because it is fifteen lines
    and because every dependency here would have to be built for ARMv7 on a box
    that is meant to boot with what is in its image.
    """
    units: list[bytes] = []
    index = 0
    length = len(data)
    start = None
    while index < length - 2:
        if data[index] == 0 and data[index + 1] == 0 and data[index + 2] == 1:
            if start is not None:
                end = index
                # A start code may be preceded by a trailing zero belonging to
                # the four-byte form; it is not part of the NAL.
                while end > start and data[end - 1] == 0:
                    end -= 1
                units.append(data[start:end])
            start = index + 3
            index += 3
            continue
        index += 1
    if start is not None:
        units.append(data[start:])
    return [unit for unit in units if unit]


class AnnexBReader:
    """Cuts a stream of bytes into access units as they arrive.

    The rule is: a NAL that begins a picture ends the access unit before it,
    **but only once that unit already contains a slice**. Without that second
    half the parameter sets that `--inline` puts in front of every keyframe
    become an access unit of their own, and a viewer receives an SPS and a PPS
    with no picture attached to them.

    One access unit of latency is inherent: a frame is only known to be complete
    when the next one starts. At 30 fps that is 33 ms, and the alternative —
    asking the encoder for access unit delimiters — costs bytes on every frame
    for the same information. `flush()` releases the last one when the stream
    stops so that a short capture is not one frame short.

    This assumes one slice per picture, which is what `rpicam-vid` produces. It
    is a limitation and it is stated rather than hidden: a multi-slice encoder
    would need `first_mb_in_slice` parsed rather than merely looked at.
    """

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.parameter_sets = bytearray()
        self._pending: list[bytes] = []
        self._seen_slice = False

    def feed(self, chunk: bytes) -> list[AccessUnit]:
        self.buffer += chunk
        units: list[AccessUnit] = []
        while True:
            nal, rest = self._take_nal()
            if nal is None:
                break
            self.buffer = rest
            unit = self._add(nal)
            if unit is not None:
                units.append(unit)
        return units

    def flush(self) -> list[AccessUnit]:
        """Release whatever is still held. Called when the encoder stops.

        The last NAL of a stream has no start code after it, so it is still in
        the buffer rather than in the pending unit — and it has to go through
        the same picture-boundary logic, or the final two frames arrive glued
        into one access unit.
        """
        remainder = bytes(self.buffer)
        self.buffer = bytearray()
        units: list[AccessUnit] = []
        start = remainder.find(b"\x00\x00\x01")
        if start >= 0:
            nal = remainder[start + 3:].rstrip(b"\x00")
            if nal:
                unit = self._add(nal)
                if unit is not None:
                    units.append(unit)
        last = self._emit()
        if last is not None:
            units.append(last)
        return units

    def _take_nal(self) -> tuple[bytes | None, bytearray]:
        """The next complete NAL unit, without start codes."""
        start = self.buffer.find(b"\x00\x00\x01")
        if start < 0:
            return None, self.buffer
        begin = start + 3
        end = self.buffer.find(b"\x00\x00\x01", begin)
        if end < 0:
            return None, self.buffer
        stop = end
        while stop > begin and self.buffer[stop - 1] == 0:
            stop -= 1
        return bytes(self.buffer[begin:stop]), self.buffer[end:]

    def _add(self, nal: bytes) -> AccessUnit | None:
        """Add one NAL, returning an access unit if this one started a picture."""
        kind = nal_type(nal)
        starts_picture = kind in (NAL_AUD, NAL_SPS, NAL_PPS, NAL_SEI) or (
            kind in (NAL_IDR, NAL_NON_IDR) and len(nal) > 1 and nal[1] & 0x80
        )
        unit = None
        if starts_picture and self._seen_slice:
            unit = self._emit()
        if kind in (NAL_SPS, NAL_PPS):
            # Kept so a viewer that attaches mid-stream can be given them
            # immediately instead of waiting for the next keyframe, which at a
            # two-second interval is two seconds of nothing.
            self.parameter_sets += b"\x00\x00\x00\x01" + nal
        if kind in (NAL_IDR, NAL_NON_IDR):
            self._seen_slice = True
        self._pending.append(nal)
        return unit

    def _emit(self) -> AccessUnit | None:
        nals, self._pending = self._pending, []
        self._seen_slice = False
        if not nals:
            return None
        return AccessUnit(
            data=b"".join(b"\x00\x00\x00\x01" + nal for nal in nals),
            captured_at=clock.now(),
            keyframe=any(nal_type(nal) == NAL_IDR for nal in nals),
            parameter_sets=bytes(self.parameter_sets),
        )


@dataclass
class StreamSettings:
    """What to ask the encoder for. Every one of these costs bandwidth."""

    width: int = 1920
    height: int = 1080
    fps: int = 30
    #: Target bitrate. The hardware encoder is rate-controlled, so this is the
    #: number that actually decides what the link carries — not the resolution.
    bitrate_kbps: int = 3000
    #: Keyframe interval in frames. Two seconds at 30 fps. Shorter costs
    #: bandwidth; longer costs a viewer up to that long staring at nothing when
    #: they attach or after a dropout.
    intra_period: int = 60
    rotation: int = 0
    camera_num: int = 0
    profile: str = "high"
    level: str = "4"

    def bytes_per_second(self) -> float:
        return self.bitrate_kbps * 1000 / 8

# --- encoders -------------------------------------------------------------
#
# Two ways to turn a camera into H.264, and the station discovers which it has
# rather than being told. They sit at opposite ends of the hardware:
#
#   hardware   a fixed-function encode block. The VideoCore in a Pi 2/3/4 has
#              one; encoding costs the CPU almost nothing.
#   software   libx264 on the CPU. The Pi 5's BCM2712 is understood to have
#              **dropped** the H.264 encode block, trading it for a much faster
#              CPU — see HARDWARE.md §9, where that claim is flagged as needing
#              verification because it would change a purchase.
#
# Neither is chosen at build time. `probe_encoders()` reports what this box can
# actually do, `choose_encoder()` picks, and the answer goes out in health
# telemetry beside the bitrate and frame rate actually achieved — so which path
# a station is on is never something anybody has to guess at afterwards.


@dataclass(frozen=True)
class EncoderProbe:
    """One encoder, and whether this box can use it."""

    name: str
    available: bool
    detail: str

    def to_dict(self) -> dict:
        return {"name": self.name, "available": self.available, "detail": self.detail}


class ProcessEncoder:
    """An encoder that is a subprocess writing Annex B to a pipe.

    Everything shared between the hardware and software paths lives here: start
    it, read it on a thread, cut the stream into access units, stop it properly.
    The subclasses differ only in the command line, which is the honest size of
    the difference — both are `rpicam-vid`, and which block of silicon does the
    work is a flag.

    **Now run against hardware.** On the first real station (Pi 2B, ov5647)
    the hardware path encoded 1080p30 through /dev/video11 and carried a live
    stream a browser decoded; HARDWARE.md §7 is the register. One measured
    property matters to callers: on an acquisition failure the process spawns
    cleanly and dies *asynchronously* — `start` returning True is not proof of
    a camera, and retrying the spawn never helps. It is written to fail
    legibly: every failure path produces a sentence naming the tool and what
    it said.
    """

    name = "process"
    #: What a person should understand this to be, in telemetry and on the
    #: console. Not the tool — the *path*.
    kind = "unknown"

    def __init__(self, settings: StreamSettings | None = None) -> None:
        self.settings = settings or StreamSettings()
        self.tool = next((tool for tool in VID_TOOLS if shutil.which(tool)), None)
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reader = AnnexBReader()
        self._on_unit = None
        self.started_at: float | None = None
        self.frames = 0
        self.bytes_out = 0
        self.keyframes = 0
        self.reason = "" if self.tool else (
            "no rpicam-vid on this box: install rpicam-apps. Without it there is "
            "nothing to drive the camera with."
        )

    # --- what the subclasses provide ------------------------------------

    def command(self) -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    @classmethod
    def probe(cls) -> EncoderProbe:  # pragma: no cover - abstract
        raise NotImplementedError

    def _base_command(self) -> list[str]:
        """The half of the command line that is about the camera, not the codec.

        `--inline` puts SPS/PPS in front of every keyframe: without it a viewer
        that attaches after the first frame gets a stream it cannot decode, and
        on a link that drops that is the normal case rather than the edge one.
        """
        settings = self.settings
        command = [
            self.tool or "rpicam-vid",
            "--nopreview",
            "--timeout", "0",                       # until we stop it
            "--inline",                             # SPS/PPS before each IDR
            "--flush",                              # do not sit on a frame
            "--width", str(settings.width),
            "--height", str(settings.height),
            "--framerate", str(settings.fps),
            "--output", "-",
        ]
        if settings.rotation == 180:
            command += ["--rotation", "180"]
        if settings.camera_num:
            command += ["--camera", str(settings.camera_num)]
        return command

    # --- lifecycle ------------------------------------------------------

    @property
    def running(self) -> bool:
        # The pump thread, not the process. Between acquire-failure respawns
        # the process is dead by definition, and judging the session by the
        # process let the stream's liveness monitor tear everything down one
        # second into a four-attempt retry - "attempt 1 of 4" followed by
        # "the encoder exited" is that race, verbatim, in the first wedged
        # station's journal. The thread lives exactly as long as the session:
        # through the waits, and not one line past the final give-up.
        thread = self._thread
        if thread is not None and thread.is_alive():
            return True
        return self._process is not None and self._process.poll() is None

    def start(self, on_unit) -> bool:
        """Start encoding, delivering each access unit to `on_unit`."""
        if self.tool is None:
            return False
        if self.running:
            return True
        self._stop.clear()
        self._on_unit = on_unit
        try:
            self._process = subprocess.Popen(
                self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self.reason = f"{self.tool} could not be started: {exc}"[:200]
            log.error("%s", self.reason)
            return False
        self.started_at = time.monotonic()
        self.frames = 0
        self.bytes_out = 0
        self.keyframes = 0
        self._reader = AnnexBReader()
        self._thread = threading.Thread(target=self._pump, name="gsu-h264", daemon=True)
        self._thread.start()
        log.info("Started %s", " ".join(self.command()))
        return True

    def stop(self) -> None:
        """Stop the encoder. Terminated, then killed, and always waited for.

        A camera process left running holds the sensor and the encoder, and the
        next start fails with a device-busy that reads like broken hardware.
        """
        self._stop.set()
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                log.warning("%s did not exit; killing it.", self.tool)
                process.kill()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                    log.error("%s could not be killed.", self.tool)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)

    #: How many times a brand-new encoder process is respawned when the camera
    #: refuses to be acquired, and how long to wait between tries. The snapshot
    #: path's in-flight subprocess owns the sensor for up to a few seconds, it
    #: cannot be signalled - only outlasted - and the encoder loses that race by
    #: dying asynchronously AFTER a clean spawn, which is why no amount of
    #: retrying the spawn ever helped. Four tries spaced 1.5s apart outlasts the
    #: slowest capture this hardware has produced, with room.
    ACQUIRE_RETRIES = 4
    ACQUIRE_RETRY_DELAY_S = 1.5

    #: What a lost acquisition race looks like in libcamera's own words. An
    #: exit for any other reason is a real fault and is not retried.
    _ACQUIRE_MARKERS = (
        "failed to acquire camera",
        "no cameras available",
        "device or resource busy",
    )

    def _pump(self) -> None:
        attempts = 0
        while True:
            process = self._process
            if process is None or process.stdout is None:  # pragma: no cover - defensive
                return
            frames_before = self.frames
            try:
                while not self._stop.is_set():
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        break
                    self.bytes_out += len(chunk)
                    for unit in self._reader.feed(chunk):
                        self._deliver(unit)
            except (OSError, ValueError) as exc:  # pragma: no cover - defensive
                log.warning("Reading from %s stopped: %s", self.tool, exc)
            for unit in self._reader.flush():
                self._deliver(unit)
            if self._stop.is_set():
                return

            # It ended on its own, which is a fault: read what it said,
            # because libcamera's own message is the whole diagnosis.
            detail = b""
            if process.stderr is not None:
                try:
                    detail = process.stderr.read() or b""
                except (OSError, ValueError):  # pragma: no cover
                    pass
            text = detail.decode("utf-8", "replace").strip()
            last_line = text.splitlines()[-1] if text else ""

            lost_race = (
                self.frames == frames_before
                and attempts < self.ACQUIRE_RETRIES
                and any(m in text.lower() for m in self._ACQUIRE_MARKERS)
            )
            if not lost_race:
                self.reason = (
                    f"{self.tool} exited: {last_line}"
                    if text else f"{self.tool} exited unexpectedly"
                )[:200]
                log.error("%s", self.reason)
                return

            # Died at birth because something else held the sensor. Wait for
            # that something to finish and go again, from in here rather than
            # from the caller: the death is asynchronous, so this thread is the
            # only place that ever actually sees it.
            attempts += 1
            log.info(
                "%s could not take the camera (attempt %d of %d); retrying in %.1fs.",
                self.tool, attempts, self.ACQUIRE_RETRIES, self.ACQUIRE_RETRY_DELAY_S,
            )
            if self._stop.wait(self.ACQUIRE_RETRY_DELAY_S):
                return
            try:
                self._process = subprocess.Popen(
                    self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            except OSError as exc:  # pragma: no cover - the tool just ran
                self.reason = f"{self.tool} could not be restarted: {exc}"[:200]
                log.error("%s", self.reason)
                return
            self._reader = AnnexBReader()

    def _deliver(self, unit) -> None:
        self.frames += 1
        if unit.keyframe:
            self.keyframes += 1
        if self._on_unit is not None:
            self._on_unit(unit)

    def stats(self) -> dict:
        elapsed = max(0.001, time.monotonic() - (self.started_at or time.monotonic()))
        return {
            "encoder": self.name,
            "kind": self.kind,
            "running": self.running,
            "tool": self.tool,
            "frames": self.frames,
            "keyframes": self.keyframes,
            "bytes": self.bytes_out,
            "fps_measured": round(self.frames / elapsed, 1),
            "bitrate_bps": round(self.bytes_out * 8 / elapsed),
            "reason": self.reason,
        }


class HardwareEncoder(ProcessEncoder):
    """H.264 on a fixed-function encode block, through V4L2 M2M.

    On a Pi 2/3/4 this is the VideoCore's encoder reached via `bcm2835-codec`,
    and it costs the CPU almost nothing — which is the entire reason a 900 MHz
    Cortex-A7 can be considered for 1080p30 at all.
    """

    name = "hardware"
    kind = "V4L2 M2M (fixed-function encode block)"

    #: The encoder half of `bcm2835-codec`. Present means the kernel has bound a
    #: hardware encoder; absent means this board does not have one, which is
    #: understood to be the case on a Pi 5.
    DEVICE = "/dev/video11"

    def command(self) -> list[str]:
        settings = self.settings
        return self._base_command() + [
            "--codec", "h264",
            "--bitrate", str(settings.bitrate_kbps * 1000),
            "--intra", str(settings.intra_period),
            "--profile", settings.profile,
            "--level", settings.level,
        ]

    @classmethod
    def probe(cls) -> EncoderProbe:
        tool = next((tool for tool in VID_TOOLS if shutil.which(tool)), None)
        if tool is None:
            return EncoderProbe(cls.name, False, "no rpicam-vid; install rpicam-apps")
        if not os.path.exists(cls.DEVICE):
            return EncoderProbe(
                cls.name, False,
                f"no {cls.DEVICE}: this board has no V4L2 hardware H.264 encoder. "
                "Expected on a Pi 5, which is understood to have dropped the "
                "encode block — use the software encoder.",
            )
        return EncoderProbe(cls.name, True, f"{tool} onto {cls.DEVICE}")


class SoftwareEncoder(ProcessEncoder):
    """H.264 on the CPU, through libav/x264 inside `rpicam-vid`.

    The path for a board with no encode block. It is not free — 1080p30 x264 is
    real CPU work — so what it actually achieved is measured and reported rather
    than assumed, which is the whole point of having both behind one interface.
    """

    name = "software"
    kind = "libav/x264 on the CPU"

    def command(self) -> list[str]:
        settings = self.settings
        return self._base_command() + [
            "--codec", "libav",
            "--libav-video-codec", "libx264",
            "--libav-format", "h264",
            "--bitrate", str(settings.bitrate_kbps * 1000),
            "--intra", str(settings.intra_period),
        ]

    @classmethod
    def probe(cls) -> EncoderProbe:
        tool = next((tool for tool in VID_TOOLS if shutil.which(tool)), None)
        if tool is None:
            return EncoderProbe(cls.name, False, "no rpicam-vid; install rpicam-apps")
        # Whether this build of rpicam-apps carries libav cannot be told from
        # the filesystem, and asking costs a subprocess in a sensing tick. It is
        # reported as "probably" and settled by `gsu stream`, which is a
        # measurement rather than a guess.
        return EncoderProbe(
            cls.name, True,
            f"{tool} --codec libav (needs an rpicam-apps built with libav; "
            "`gsu stream` says so in one line if it is not)",
        )


#: Every encoder this station knows how to be, in the order `auto` prefers them.
#: Hardware first because it costs nothing when it exists.
ENCODERS = (HardwareEncoder, SoftwareEncoder)


def probe_encoders() -> list[EncoderProbe]:
    """What this box can actually do. Reported in health telemetry."""
    return [encoder.probe() for encoder in ENCODERS]


def choose_encoder(preference: str = "auto") -> tuple[type[ProcessEncoder] | None, str]:
    """Pick an encoder, and say why that one.

    `preference` is `auto`, `hardware` or `software`, and comes from
    configuration rather than from code: moving a station between a board with an
    encode block and one without is then a setting and a measurement, not a
    rewrite.
    """
    preference = (preference or "auto").strip().lower()
    probes = {probe.name: probe for probe in probe_encoders()}
    if preference in ("hardware", "software"):
        chosen = next(e for e in ENCODERS if e.name == preference)
        probe = probes[preference]
        if not probe.available:
            return None, (
                f"the {preference} encoder was asked for and is not usable: "
                f"{probe.detail}"
            )
        return chosen, f"{preference} encoder, as configured — {probe.detail}"
    for encoder in ENCODERS:
        probe = probes[encoder.name]
        if probe.available:
            return encoder, f"{encoder.name} encoder, chosen automatically — {probe.detail}"
    return None, "; ".join(probe.detail for probe in probes.values())
