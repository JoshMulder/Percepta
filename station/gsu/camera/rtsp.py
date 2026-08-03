"""A network camera speaking RTSP, read through ffmpeg.

This is the long-term camera path — the CSI ribbon was the easy test article —
and it is a different job in one important way: the camera does its own
capture and its own encode, in whichever codec it was configured for. This
station never decodes for the live stream and never re-encodes anything; a Pi
2B cannot transcode and must never be asked to. Two paths, both ffmpeg
subprocesses:

    preview    one JPEG per capture: connect, decode one frame, encode one
               JPEG, exit. This *does* decode — it is the only place a decoder
               runs — and it is bounded to a single frame, taken only while
               somebody has the setup page open. It used to run twice a second
               for ever, to feed a snapshot channel that no longer exists; at
               the current rate its cost is not worth a lever.

               This is also, literally, "a single ffmpeg frame pulled from the
               same source": the URL it reads is the one the live stream
               remuxes. Two readers of a network camera do not contend — the
               camera encodes for itself and serves both — which is why
               `owns_sensor` is False here and why this path needs no lease.
    stream     remux without re-encode (`-c copy`): the camera's own H.264 or
               HEVC copied into Annex B on stdout, cut into access units by the
               same reader as every other encoder, muxed into the same fMP4.
               Which codec it is decides the ffmpeg muxer and the NAL grammar
               together — see `STREAM_CODECS`, and the comment on it, which is
               a scar. A source that is neither is refused by name rather than
               transcoded, which would peg the CPU and deliver seconds per
               frame.

`ffmpeg` is an apt dependency (DEPLOYMENT.md §2 and the installer say so). A
box without it reports exactly that, per path, rather than a camera fault.

**Never met a real RTSP camera.** Every seam here is unit-tested against fake
subprocesses and synthetic Annex B, and the command lines follow ffmpeg's
documented behaviour — but RTSP negotiation, camera-side quirks and real
network timing have not been exercised. HARDWARE.md §10 keeps the register.

Credentials: stored in the device inventory, injected into the URL ffmpeg is
given (RTSP has no other way to carry them), and never rendered — not in
`describe()`, not in a reason, not in telemetry. The one place they are
unavoidably visible is the ffmpeg process's argument list, readable by other
local users; this box runs one application under one service account, and a
local shell already reads the inventory file itself. A URL typed with
credentials embedded is refused so there is exactly one stored copy.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from urllib.parse import quote, unquote, urlsplit

from .. import clock
from ..sensors import Device
from . import Frame, complete_jpeg, jpeg_dimensions
from .h264 import H264, EncoderProbe, ProcessEncoder, StreamSettings
from .hevc import HEVC

log = logging.getLogger("gsu.rtsp")

#: One snapshot may take an RTSP handshake plus a wait for the next keyframe.
#: Generous, and bounded: an unattended box cannot afford a subprocess that
#: never returns.
CAPTURE_TIMEOUT_S = 15.0

#: How long to leave a failing camera alone before trying again, and how many
#: failures in a row before the slot is reported failed rather than silent.
#: Same discipline as the CSI driver, for the same reasons.
RETRY_SECONDS = 10.0
FAILURES_BEFORE_FAILED = 3

NO_FFMPEG = (
    "ffmpeg is not installed, and it is what reads an RTSP camera. "
    "`apt install ffmpeg` — the installer lists it for exactly this."
)

#: What the live path can carry, by ffprobe's name for it, mapped to the NAL
#: grammar the reader downstream must use and the ffmpeg muxer that produces it.
#: Both have to change together: `-f h264` around an HEVC stream is a container
#: that lies, and the reader then looks for H.264 headers in H.265 and finds a
#: frame every few thousand. That combination is what the first real camera
#: produced, silently, and it is why this is one table rather than two defaults.
STREAM_CODECS = {
    "h264": ("h264", H264),
    "hevc": ("hevc", HEVC),
}


def build_url(address: str, port: int = 554, rtsp_path: str = "",
              username: str = "", password: str = "") -> str:
    """The URL ffmpeg is handed, credentials percent-encoded in.

    `address` is a host, a host:port, or a full rtsp:// URL for cameras whose
    path carries query strings the form's separate fields cannot express. A
    URL that already embeds credentials is refused: the username and password
    fields are the one place a secret is stored, and a second copy inside a
    URL is a copy that ends up rendered somewhere.
    """
    address = (address or "").strip()
    if not address:
        raise ValueError("no camera address set")
    if address.lower().startswith(("rtsp://", "rtsps://")):
        split = urlsplit(address)
        if split.username or split.password or "@" in split.netloc:
            raise ValueError(
                "put the camera's credentials in the username and password "
                "fields, not inside the URL — they are stored once and never "
                "shown again"
            )
        base = address
    else:
        host = address
        if ":" not in host.split("]")[-1]:       # tolerate [v6]:port
            host = f"{host}:{int(port or 554)}"
        path = rtsp_path or ""
        if path and not path.startswith("/"):
            path = "/" + path
        base = f"rtsp://{host}{path}"
    if not username and not password:
        return base
    scheme, _, rest = base.partition("://")
    auth = quote(username or "", safe="") + (
        ":" + quote(password or "", safe="") if password else ""
    )
    return f"{scheme}://{auth}@{rest}"


def split_credentials(address: str) -> tuple[str, str, str]:
    """`(address_without_userinfo, username, password)` for a pasted URL.

    Camera vendors hand installers a complete `rtsp://user:pass@host/…` line,
    and refusing it at the form makes somebody retype a password on a phone on
    a roof. The setup page calls this instead: the URL is stored without its
    userinfo, and the credentials move into the fields that are stored once
    and never rendered. Anything that is not an rtsp/rtsps URL, or carries no
    userinfo, comes back unchanged with empty credentials.

    Percent-decoded on the way out, because `build_url` re-encodes on the way
    back in — without the decode a password with an `@` in it would gain a
    layer of encoding on every save.
    """
    address = (address or "").strip()
    if not address.lower().startswith(("rtsp://", "rtsps://")):
        return address, "", ""
    scheme, separator, rest = address.partition("://")
    authority, tail = rest, ""
    for index, character in enumerate(rest):
        if character in "/?":
            authority, tail = rest[:index], rest[index:]
            break
    userinfo, at, host = authority.rpartition("@")
    if not at:
        return address, "", ""
    username, _, password = userinfo.partition(":")
    return (
        f"{scheme}{separator}{host}{tail}",
        unquote(username),
        unquote(password),
    )


def redact(url: str) -> str:
    """The same URL with the credentials gone, for humans and telemetry."""
    scheme, separator, rest = url.partition("://")
    if separator and "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    return f"{scheme}{separator}{rest}"


class RtspCamera:
    """One RTSP camera: snapshots by decoding one frame, live by remuxing."""

    #: The network camera encodes and serves itself; this station's snapshot
    #: and stream paths are both mere readers and never contend for a local
    #: sensor. See camera.sensor_exclusive().
    owns_sensor = False

    def __init__(
        self,
        address: str = "",
        port: int = 554,
        rtsp_path: str = "",
        username: str = "",
        password: str = "",
        transport: str = "tcp",
    ) -> None:
        # Raises on a credentialled URL — the inventory records the sentence
        # as the slot's reason, which is where an installer will read it.
        self._url = build_url(address, port, rtsp_path, username, password)
        self.transport = "udp" if str(transport).lower() == "udp" else "tcp"
        #: The camera's own configured frame rate, or None until the stream has
        #: been probed. **Never seeded from configuration**, and the absence of
        #: an `fps` constructor argument is the enforcement rather than a
        #: convention: `50a4d85` removed the field from the registry, but boxes
        #: provisioned before that still carry `fps: 30` in devices.json, and
        #: `Inventory._instantiate` filters stored params by *constructor
        #: signature*, not by what the registry currently declares. So a
        #: parameter that no longer exists went on being honoured, seeded the
        #: muxer's clock at 30 against a camera sending 25, and made the first
        #: stream after every restart run its timeline 20% fast — which is what
        #: a viewer shows as stutter and catch-up. A field that cannot be
        #: passed cannot be stale.
        self.stream_fps: float | None = None

        self._ffmpeg = shutil.which("ffmpeg")
        self.backend = "ffmpeg" if self._ffmpeg else "none"
        # Empty when it is working, and that is the whole change here. This
        # used to read "RTSP via ffmpeg from <url>; snapshots decode one frame,
        # the live stream is remuxed without re-encoding" — the URL is already
        # in the form directly below it on the setup page, and the rest
        # describes how this build is implemented rather than anything about
        # this camera. `backend_reason` exists to explain a fault (a venv built
        # without --system-site-packages looks exactly like slow hardware), and
        # filling it in when there is no fault is how a field people should
        # read becomes one they skip.
        self.backend_reason = "" if self._ffmpeg else NO_FFMPEG
        self._reason = "" if self._ffmpeg else NO_FFMPEG
        #: The stream's codec, probed once and cached for the session. "" means
        #: "probed and could not tell"; None means "not probed yet".
        self._codec: str | None = None
        self._failures = 0
        self._next_attempt = 0.0
        self.frames = 0
        self.last_bytes = 0
        self.last_capture_ms = 0.0
        self._last_dims: tuple[int, int] | None = None

    # --- the interface ----------------------------------------------------

    @property
    def status(self) -> str:
        if self.backend == "none":
            return "absent"
        if self._failures >= FAILURES_BEFORE_FAILED:
            return "failed"
        return "streaming" if self.frames else "silent"

    @property
    def unavailable_reason(self) -> str:
        return self._reason

    def capture(self) -> Frame | None:
        if self._ffmpeg is None:
            return None
        if time.monotonic() < self._next_attempt:
            # Backing off; the last failure's reason is still the truth.
            return None
        started = time.monotonic()
        command = [
            self._ffmpeg, "-nostdin", "-loglevel", "error",
            "-rtsp_transport", self.transport,
            "-i", self._url,
            "-an", "-dn",
            "-frames:v", "1",
            "-f", "image2pipe", "-c:v", "mjpeg",
            "pipe:1",
        ]
        try:
            done = subprocess.run(
                command, capture_output=True, timeout=CAPTURE_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self._failed(
                f"the camera did not deliver a frame within "
                f"{CAPTURE_TIMEOUT_S:.0f}s ({redact(self._url)})"
            )
        except OSError as exc:
            return self._failed(f"ffmpeg could not be run: {exc}")
        at = clock.now()
        if done.returncode != 0 or not complete_jpeg(done.stdout):
            # ffmpeg's own last line is the diagnosis — "Connection refused",
            # "401 Unauthorized", "no such codec" — minus anything that could
            # echo the URL's credentials.
            detail = (done.stderr or b"").decode("utf-8", "replace").strip()
            last = detail.splitlines()[-1] if detail else ""
            return self._failed(
                f"ffmpeg returned no complete frame"
                + (f": {redact(last)}" if last else "")
            )
        dims = jpeg_dimensions(done.stdout) or self._last_dims
        if dims is None:
            return self._failed(
                "the camera's JPEG carried no readable dimensions; the frame "
                "was dropped rather than published with invented ones"
            )
        self._last_dims = dims
        self._failures = 0
        self._reason = ""
        self.frames += 1
        self.last_bytes = len(done.stdout)
        self.last_capture_ms = (time.monotonic() - started) * 1000
        return Frame(jpeg=done.stdout, width=dims[0], height=dims[1],
                     captured_at=at)

    def stream_source(self, settings: StreamSettings):
        """The live path: this camera's own H.264 or HEVC, remuxed. None with
        the reason in `unavailable_reason` when there is nothing to remux
        with.

        **Probed fresh every session, not once per driver.** The codec and the
        frame rate are the camera's to change, and a technician who switches it
        from H.264 to H.265 does not restart this station afterwards. A value
        cached for the life of the driver would then pick the wrong NAL grammar
        and the wrong ffmpeg muxer — which fails silently, as an MSE source
        buffer that accepts everything and decodes nothing. One ffprobe against
        a camera the station is about to open an ffmpeg session to anyway is
        not a cost worth being clever about.
        """
        if self._ffmpeg is None:
            self._reason = NO_FFMPEG
            return None
        codec = self.probe_codec(refresh=True)
        if codec and codec not in STREAM_CODECS:
            # Still a refusal, and for the reason the refusal was written: the
            # station remuxes and cannot transcode, so a codec it does not carry
            # end to end has to be named rather than attempted. What changed is
            # the list. `-c copy` will pour anything into whatever container the
            # `-f` names, ffmpeg exits zero, bytes flow, and the reader
            # downstream finds nothing it recognises - one access unit in 109
            # seconds, no error anywhere. That is why the codec decides the
            # muxer and the NAL grammar together, below, instead of both being
            # assumed.
            self._reason = (
                f"this camera streams {codec.upper()}, and the live stream "
                f"carries H.264 or H.265 - the station remuxes without "
                f"re-encoding and cannot transcode. Set the camera's encoder to "
                f"one of those, or use a substream that already is. Snapshots "
                f"still work."
            )
            return None
        return RtspRemuxSource(settings, url=self._url, transport=self.transport,
                               codec=codec or "h264", stream_fps=self.stream_fps)

    def probe_codec(self, refresh: bool = False) -> str | None:
        """The stream's video codec, via ffprobe.

        None when it cannot be determined - an unreachable camera is already
        reported by the capture path, and refusing a stream because a probe
        timed out would turn a network blip into a configuration error.

        `refresh` re-asks rather than using what is cached. The cache exists so
        that `describe()` and telemetry can say what the camera streams without
        a subprocess each time; the stream path passes `refresh=True` because
        it is about to act on the answer.
        """
        if self._codec is not None and not refresh:
            return self._codec or None
        probe = shutil.which("ffprobe")
        if not probe:
            # No prober on this box. Whatever is already known is better than
            # nothing — and on a refresh, forgetting it would silently
            # downgrade a known HEVC camera to the h264 default.
            return self._codec or None
        try:
            result = subprocess.run(
                [probe, "-v", "error", "-rtsp_transport", self.transport,
                 "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,avg_frame_rate",
                 "-of", "default=nw=1:nk=1", self._url],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            # A refresh that could not reach the camera keeps what it knew.
            # Forgetting here would downgrade a known HEVC camera to the
            # h264 default on one dropped packet, which is the silent-black
            # failure this probe exists to prevent.
            return self._codec or None
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        # **A probe that ran and answered nothing is not an answer.**
        #
        # The exception path above is careful to keep what it knew, for exactly
        # the reason stated there — and then this threw it away anyway on the
        # far more common failure. ffprobe does not raise when it cannot reach a
        # camera: it exits non-zero with an empty stdout, which landed here as
        # `self._codec = ""`, which `stream_source` reads back as "unknown" and
        # turns into the h264 default. An HEVC camera then gets an H.264 muxer
        # and H.264 NAL rules, and the result is the silent-black failure this
        # whole probe exists to prevent: ffmpeg exits zero, bytes flow, and the
        # far end decodes none of them.
        #
        # One unreachable moment — a reboot, a lease renewal, a switch port
        # flapping — was enough, and the codec stayed wrong until the process
        # restarted.
        if result.returncode != 0 or not lines:
            log.warning(
                "ffprobe could not read %s (exit %d): %s. Keeping the last "
                "known codec %r rather than assuming.",
                redact(self._url), result.returncode,
                (result.stderr or "").strip()[:120] or "no output",
                self._codec or "unknown",
            )
            return self._codec or None
        self._codec = lines[0]
        # ffprobe reports the rate as a rational, "25/1". Taken from the stream
        # rather than asked of an operator: the camera decides its own rate, the
        # station only copies what arrives, and a hand-typed figure that
        # disagrees plays the result fast or slow at the far end for no reason
        # anybody could see. A wrong default of 15 or 30 against a real 25 was
        # what made the field worth removing.
        rate = 0.0
        if len(lines) > 1 and "/" in lines[1]:
            numerator, _, denominator = lines[1].partition("/")
            try:
                rate = float(numerator) / float(denominator)
            except (ValueError, ZeroDivisionError):
                rate = 0.0
        if 1.0 <= rate <= 120.0:
            self.stream_fps = rate
        else:
            # Left as None rather than guessed. The caller paces the muxer from
            # the site's configured rate instead and says so, which is a stated
            # fallback rather than a number that looks measured and is not.
            self.stream_fps = None
            log.warning(
                "ffprobe gave no usable frame rate for %s (avg_frame_rate %r); "
                "the muxer clock will fall back to the configured rate.",
                redact(self._url), lines[1] if len(lines) > 1 else "",
            )
        return self._codec or None

    def raw_sample(self) -> list[str]:
        if not self.frames:
            return []
        width, height = self._last_dims or (0, 0)
        return [
            f"frame {self.frames}: {self.last_bytes / 1024:.1f} kB, "
            f"{width}x{height}, {self.last_capture_ms:.0f} ms via ffmpeg"
        ]

    def describe(self) -> Device:
        if self.backend == "none":
            detail = self._reason
        else:
            detail = f"RTSP camera {redact(self._url)} via ffmpeg"
            if self.frames:
                width, height = self._last_dims or (0, 0)
                detail += (
                    f", {width}x{height}, {self.last_bytes / 1024:.1f} kB/frame, "
                    f"{self.last_capture_ms:.0f} ms/capture"
                )
            elif self._reason:
                detail += f" — {self._reason}"
        return Device(
            id="camera", kind="camera",
            present=self.status in ("streaming", "silent"),
            detail=detail[:200], simulated=False,
        )

    def close(self) -> None:
        # Nothing held between captures: each is a subprocess that has already
        # exited, and the live source is owned and stopped by StreamSession.
        return None

    # --- failure ------------------------------------------------------------

    def _failed(self, reason: str) -> None:
        self._failures += 1
        self._reason = reason[:200]
        self._next_attempt = time.monotonic() + RETRY_SECONDS
        if self._failures == FAILURES_BEFORE_FAILED:
            log.error(
                "The RTSP camera has failed %d captures in a row: %s. "
                "Retrying every %.0fs.", self._failures, self._reason,
                RETRY_SECONDS,
            )
        return None


class RtspRemuxSource(ProcessEncoder):
    """The camera's own H.264 or HEVC copied to Annex B on stdout.

    No encoder anywhere. Everything downstream — the access-unit reader, the
    fMP4 muxer, the uplink — is the same code on both codecs, which is the
    point: one container, one bug surface, and the synthetic source proves the
    H.264 half of it. What is different is stated: the bitrate, resolution and
    keyframe interval are the *camera's*, set on the camera, and the settings
    this station computed are hints it cannot enforce. Attempting a transcode
    instead would peg a Pi 2B and deliver seconds per frame, which is a worse
    failure than an honest one.

    `codec` is what ffprobe said, and it decides two things that must agree:
    the ffmpeg muxer that frames stdout, and the NAL grammar the reader parses
    it with. They were previously both fixed at H.264, and an HEVC camera
    therefore produced an H.264-labelled container full of H.265 that failed
    without a single error message in it.
    """

    name = "rtsp-remux"

    # The camera decided its resolution, rate and bitrate before this station
    # connected, and `-c copy` changes none of them. Saying otherwise is how a
    # log came to read "1920x1080 at 30 fps, 3000 kbit/s" for a camera sending
    # 1080p at 5.
    enforces_settings = False

    def __init__(self, settings: StreamSettings | None = None, *,
                 url: str, transport: str = "tcp", codec: str = "h264",
                 stream_fps: float | None = None) -> None:
        # Before super().__init__, which builds the first access-unit reader
        # and needs to be told which grammar it is reading.
        self.codec = codec if codec in STREAM_CODECS else "h264"
        self.muxer_format, self.nal_rules = STREAM_CODECS[self.codec]
        super().__init__(settings)
        self.url = url
        self.transport = transport
        #: The rate the camera is actually sending at, measured by ffprobe on
        #: the way in. The muxer's clock is built from this and not from
        #: `settings.fps`: the station copies what arrives and cannot change
        #: the rate, so a timeline paced to the site's *policy* rather than to
        #: the stream runs fast or slow at the far end. None when the probe
        #: could not tell, and the caller then falls back to the configured
        #: rate and says which it used.
        self.stream_fps = stream_fps
        self.kind = (
            f"RTSP remux of the camera's {self.codec.upper()}, no re-encode "
            f"(ffmpeg -c copy)"
        )
        # ProcessEncoder probes for rpicam-vid; this source's tool is ffmpeg.
        self.tool = "ffmpeg" if shutil.which("ffmpeg") else None
        self.reason = "" if self.tool else NO_FFMPEG

    def command(self) -> list[str]:
        return [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-rtsp_transport", self.transport,
            "-i", self.url,
            "-an", "-dn",                      # video only; no audio, no data
            "-c:v", "copy",                    # the whole design: never encode
            # No h264_mp4toannexb / hevc_mp4toannexb. RTP already delivers both
            # codecs as Annex B, and the filter refuses a stream that is not in
            # the MP4 length-prefixed form - "Error initializing output stream
            # 0:0" against the first real camera this met. The raw `h264` and
            # `hevc` muxers emit Annex B regardless, which is what AnnexBReader
            # downstream expects.
            "-f", self.muxer_format,
            "pipe:1",
        ]

    @classmethod
    def probe(cls) -> EncoderProbe:
        if shutil.which("ffmpeg"):
            return EncoderProbe(cls.name, True, "ffmpeg -c copy from the camera")
        return EncoderProbe(cls.name, False, NO_FFMPEG)
