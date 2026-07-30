"""A network camera speaking RTSP, read through ffmpeg.

This is the long-term camera path — the CSI ribbon was the easy test article —
and it is a different job in one important way: the camera does its own
capture and its own H.264 encode. This station never decodes for the live
stream and never re-encodes anything; a Pi 2B cannot transcode and must never
be asked to. Two paths, both ffmpeg subprocesses:

    snapshot   one JPEG per capture: connect, decode one frame, encode one
               JPEG, exit. This *does* decode — it is the only place a decoder
               runs, it is bounded to a single frame at the snapshot cadence,
               and its cost is why the frame rate lever (`video_fps`) matters
               more on this camera than on the CSI one.
    stream     remux without re-encode (`-c copy`): the camera's own H.264
               copied into Annex B on stdout, cut into access units by the
               same reader as every other encoder, muxed into the same fMP4.
               A source that is not H.264 fails with ffmpeg's own sentence
               rather than with an attempted transcode that would peg the CPU
               and deliver seconds per frame.

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
from urllib.parse import quote, urlsplit

from .. import clock
from ..sensors import Device
from . import Frame, complete_jpeg, jpeg_dimensions
from .h264 import EncoderProbe, ProcessEncoder, StreamSettings

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
        fps: float = 15.0,
    ) -> None:
        # Raises on a credentialled URL — the inventory records the sentence
        # as the slot's reason, which is where an installer will read it.
        self._url = build_url(address, port, rtsp_path, username, password)
        self.transport = "udp" if str(transport).lower() == "udp" else "tcp"
        #: The camera's own configured frame rate. The station cannot change
        #: it — remux copies what arrives — so the muxer's clock is paced to
        #: this, and a wrong value here plays the stream fast or slow.
        self.stream_fps = max(1.0, float(fps or 15.0))

        self._ffmpeg = shutil.which("ffmpeg")
        self.backend = "ffmpeg" if self._ffmpeg else "none"
        self.backend_reason = (
            f"RTSP via ffmpeg from {redact(self._url)}; snapshots decode one "
            "frame, the live stream is remuxed without re-encoding"
            if self._ffmpeg else NO_FFMPEG
        )
        self._reason = "" if self._ffmpeg else NO_FFMPEG
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
        """The live path: this camera's own H.264, remuxed. None with the
        reason in `unavailable_reason` when there is nothing to remux with."""
        if self._ffmpeg is None:
            self._reason = NO_FFMPEG
            return None
        return RtspRemuxSource(settings, url=self._url, transport=self.transport)

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
    """The camera's H.264 copied to Annex B on stdout. No encoder anywhere.

    Everything downstream — the access-unit reader, the fMP4 muxer, the
    uplink — is byte-identical to the rpicam paths, which is the point: one
    container, one bug surface, and the synthetic source proves it. What is
    different is stated: the bitrate, resolution and keyframe interval are the
    *camera's*, set on the camera, and the settings this station computed are
    hints it cannot enforce. A non-H.264 source dies at start with ffmpeg's
    own sentence; attempting a transcode instead would peg a Pi 2B and
    deliver seconds per frame, which is a worse failure than an honest one.
    """

    name = "rtsp-remux"
    kind = "RTSP remux, no re-encode (ffmpeg -c copy)"

    def __init__(self, settings: StreamSettings | None = None, *,
                 url: str, transport: str = "tcp") -> None:
        super().__init__(settings)
        self.url = url
        self.transport = transport
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
            "-bsf:v", "h264_mp4toannexb",      # normalise to start codes
            "-f", "h264",
            "pipe:1",
        ]

    @classmethod
    def probe(cls) -> EncoderProbe:
        if shutil.which("ffmpeg"):
            return EncoderProbe(cls.name, True, "ffmpeg -c copy from the camera")
        return EncoderProbe(cls.name, False, NO_FFMPEG)
