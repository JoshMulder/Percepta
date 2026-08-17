"""A small still of what the camera sees, sent to the platform on a lease.

WHAT THIS IS FOR. The wall shows every station at once, and a tile with no
picture is a row of numbers about a place nobody can see. Live video is not the
answer at that scale: one stream is ~2.6 Mbit/s through an in-process relay, and
a wall of them is a thousand times the traffic and a deployment that cannot run
more than one worker. A scaled JPEG once a minute is ~2.75 kbit/s — about an
eighth of what squelch-gated Opus costs while somebody is talking.

LEASED, NOT PERIODIC. The platform asks and keeps asking; silence is the stop
signal. That is the same rule audio and video already follow, and it exists
because most consumers never say goodbye — a browser tab closes, a laptop lid
shuts, and a station left sending for ever is the most expensive failure this
system has. Nothing is captured unless somebody is looking at it right now.

IT TAKES NO PICTURES OF ITS OWN. `video.py` says, at length, that the preview
publishes nothing, and that two readers of one sensor was the whole camera-wedge
bug. This module does not become the second reader. It registers a demand with
the preview under its own name, waits for a frame to appear there, and sends
whatever the preview last took. A human on the setup page and the wall therefore
share captures rather than competing for the sensor — and when both are
watching, neither pays twice.

THE STATE-OF-CHARGE GATE IS NOT OPTIONAL, and it is why this module is more
than a POST. This station's failure mode is sustained load: on 2026-08-15 it
logged `Undervoltage detected!` with the core rail at 7.7 A and stopped
executing in the same second. A capture is an RTSP handshake, a decode and a
JPEG encode — modest, but standing, and standing load on a board that cannot
hold its own maximum is exactly the wrong thing to add without a brake. So the
station refuses below `shed_poster_below_soc_pct`, on its own authority, the
same way it already sheds the floodlight. The platform can ask; the site
decides.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

#: Longest a poster may be, in bytes. A 480x270 JPEG is ~20 kB; 256 kB is an
#: order of magnitude of headroom and still small enough that a camera
#: misconfigured to full resolution is refused here rather than discovered on
#: somebody's data bill. The platform bounds it at the same figure.
MAX_POSTER_BYTES = 256 * 1024

#: How long one POST may take before it is abandoned. Short: a poster is worth
#: having promptly or not at all, and the next lease tick brings another. A slow
#: link must not accumulate half-finished uploads behind a station's telemetry.
POST_TIMEOUT_S = 20.0

#: The tile's size on the wall. Height, not width, because `-2` on the other
#: axis lets ffmpeg keep the camera's aspect ratio and round to an even number
#: — a 4:3 camera becomes 360x270 rather than being stretched into 16:9.
POSTER_HEIGHT = 270

#: A JPEG→JPEG rescale is a local subprocess with no network in it, so this is
#: generous by an order of magnitude. It exists so a wedged ffmpeg cannot hold
#: the poster thread for ever, not as a performance budget.
SCALE_TIMEOUT_S = 15.0

#: How often a picture is worth sending. The lease is longer than this so a
#: single dropped command does not stop the pictures.
DEFAULT_INTERVAL_S = 60.0

#: How long a lease runs when the platform does not say. Longer than the wall's
#: own refresh so one dropped command does not blank a tile, short enough that a
#: console nobody is watching stops the camera within minutes.
DEFAULT_LEASE_S = 180.0

#: How often the thread wakes to see whether a new frame is worth sending.
#: Cheap — a clock comparison and a timestamp comparison — and it sets how
#: promptly a poster follows the capture it is made from.
POLL_S = 0.5

#: What the station will actually hold to, whatever the platform states. The
#: shared 5–300 s clamp from `contract/transport.md`'s timings table: the floor
#: stops a lease so short it expires before the next renewal can arrive, and the
#: ceiling is the bound that makes "silence is the stop signal" a promise with a
#: number on it rather than a sentiment.
LEASE_MIN_S = 5.0
LEASE_MAX_S = 300.0

#: And the same for the cadence. A caller may ask for LESS often and never for
#: more — the floor here is a request bound, and `CameraPreview.MIN_INTERVAL_S`
#: is the real one, applied again where the camera is actually driven.
INTERVAL_MIN_S = 5.0
INTERVAL_MAX_S = 3600.0


def scale(jpeg: bytes, *, ffmpeg: str | None, height: int = POSTER_HEIGHT) -> bytes:
    """Shrink one JPEG to tile size. Returns the original if it cannot.

    SEPARATE FROM THE CAPTURE, deliberately. The obvious alternative is to ask
    the camera driver for a small frame, and it is wrong: the setup page shares
    these captures and is somebody standing in front of the box aiming a lens,
    for whom 480x270 is not enough to focus on. Sizing at send time means the
    two consumers never have to agree — the preview keeps taking the picture it
    always took, and only the copy that goes on the wire is small.

    Best-effort by design. A station with no ffmpeg (a CSI camera box that never
    needed one) still posts, just at whatever size the camera gave; the size cap
    in `send` is what stops that being unbounded.
    """
    if not ffmpeg or not jpeg:
        return jpeg
    try:
        done = subprocess.run(
            [
                ffmpeg, "-nostdin", "-loglevel", "error",
                "-f", "image2pipe", "-i", "pipe:0",
                # `-2` keeps the aspect ratio and rounds to an even width, which
                # the JPEG encoder requires for subsampled chroma. `min(...)`
                # never enlarges: a camera already smaller than the tile is left
                # alone rather than interpolated up to look worse.
                "-vf", f"scale=-2:'min({height},ih)'",
                "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg",
                "pipe:1",
            ],
            input=jpeg, capture_output=True, timeout=SCALE_TIMEOUT_S, check=False,
        )
    except Exception as exc:  # noqa: BLE001 - a poster must not break the station
        log.debug("Poster rescale could not run (%s); sending full size.", exc)
        return jpeg
    if done.returncode != 0 or not done.stdout:
        log.debug("Poster rescale failed; sending full size.")
        return jpeg
    return done.stdout


def send(
    *,
    url: str,
    secret: str,
    jpeg: bytes,
    captured_at: str,
    width: int,
    height: int,
    context=None,
) -> tuple[bool, str]:
    """POST one poster. Returns (sent, reason). Never raises.

    OVER HTTP, NOT THE BROKER. `contract/transport.md` is explicit that the
    broker carries control and telemetry which must not be delayed by bulk
    data, and a JPEG is bulk data — base64 inside a broker frame would also add
    a third to its size to no purpose.

    Best-effort: a failed poster costs a tile one cycle. It must never be able
    to disturb telemetry, which is the thing that actually matters.
    """
    if not jpeg:
        return False, "no frame"
    if len(jpeg) > MAX_POSTER_BYTES:
        # Refused HERE rather than by the platform, so a misconfigured camera
        # costs nothing on the link at all. The platform bounds it too — this
        # is the cheap half of the same check.
        return False, f"frame too large ({len(jpeg) // 1024} kB)"

    request = urllib.request.Request(url, data=jpeg, method="POST")
    request.add_header("Content-Type", "image/jpeg")
    # The station's own credential. The platform derives WHICH station this is
    # from the secret and never from anything in the body — a station cannot
    # post a picture as somebody else.
    request.add_header("Authorization", f"Bearer {secret}")
    request.add_header("X-Captured-At", captured_at)
    request.add_header("X-Frame-Size", f"{width}x{height}")
    # Named, because urllib otherwise announces itself as Python-urllib and a
    # bot-detecting edge has already refused this station once on exactly that.
    request.add_header("User-Agent", "percepta-gsu")

    try:
        with urllib.request.urlopen(
            request, timeout=POST_TIMEOUT_S, context=context
        ) as response:
            if 200 <= response.status < 300:
                return True, ""
            return False, f"platform said {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"platform said {exc.code}"
    except Exception as exc:  # noqa: BLE001 - a poster must not break the station
        return False, str(exc)


def poster_url(config, enrolment=None) -> str | None:
    """Where the platform takes posters. `None` if there is no platform URL.

    Derived from the API address rather than given its own environment
    variable, unlike the media and console links. Those exist because a
    WebSocket may have to be routed somewhere else entirely; this is an
    ordinary POST to the same host that already serves enrolment, over the same
    connection policy, so a second knob would be a second thing to get wrong.
    """
    api = (getattr(config, "platform_url", "") or "").rstrip("/")
    return f"{api}/media/poster" if api else None


class PosterPublisher:
    """Holds the platform's poster lease and sends what the preview took.

    One thread, asleep unless a lease is live. Built whether or not a camera is
    fitted — a station that grows one later should not need a restart, and the
    thread costs a clock comparison a second to be wrong about that.
    """

    #: The name this publisher registers its demand under, so the preview can
    #: tell it apart from the setup page. See `CameraPreview.request`.
    CALLER = "poster"

    def __init__(self, agent, *, url: str | None, secret: str = "", trust=None) -> None:
        self.agent = agent
        self.url = url
        self.secret = secret
        self.trust = trust
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Monotonic deadline the platform's lease runs to. Zero means nobody
        #: is watching, which is the state a station spends nearly all its life
        #: in and the reason this costs nothing.
        self._lease_until = 0.0
        self._interval = DEFAULT_INTERVAL_S
        self._lock = threading.Lock()
        #: The capture timestamp of the last frame actually sent, so the same
        #: picture is never posted twice. A camera the preview could not reach
        #: leaves its last frame standing (deliberately — `video.py`), and
        #: without this the wall would be shown the same minute-old still over
        #: and over as though it were current.
        self._last_sent_at = None
        #: Monotonic time of the last upload ATTEMPT, which is what paces the
        #: link. Distinct from `_last_sent_at` above: that one is the capture
        #: timestamp and stops the same picture going twice, this one stops a
        #: fast camera turning into a fast uplink. Zero so the first poster of a
        #: lease goes out at once rather than an interval late.
        self._sent_at = 0.0
        #: The refusal counter's own pacer — see `tick`. Separate from
        #: `_sent_at` so a refusal never delays the first picture after the
        #: battery recovers.
        self._refused_at = 0.0
        self.sent = 0
        self.failed = 0
        self.refused = 0
        self.last_reason = ""

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="gsu-poster", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    def update_secret(self, secret: str) -> None:
        """Adopt a renewed credential. Called from the agent's renewal path for
        the same reason the console proxy and host shell have one: a station
        that renewed at 3am must not start failing its posters at 4am."""
        self.secret = secret

    # --- the lease ------------------------------------------------------

    def request(self, lease_seconds=None, interval_s=None) -> str:
        """`video.poster`: the platform would like pictures.

        REPLACES, never extends — the same rule as audio and video. A shorter
        renewal shortens the lease, which is how the platform stops the
        pictures sooner than the last one promised rather than having to wait
        out a lease it has changed its mind about.
        """
        # Clamped, like every other lease this station honours — the platform
        # states a number and the station decides what it is willing to hold to.
        # `contract/transport.md`'s timings table owns both bounds.
        lease = (
            DEFAULT_LEASE_S if lease_seconds is None
            else max(LEASE_MIN_S, min(LEASE_MAX_S, float(lease_seconds)))
        )
        interval = (
            DEFAULT_INTERVAL_S if interval_s is None
            else max(INTERVAL_MIN_S, min(INTERVAL_MAX_S, float(interval_s)))
        )
        with self._lock:
            self._lease_until = time.monotonic() + lease
            self._interval = interval
        return f"posters every {interval:.0f}s for {lease:.0f}s"

    def release(self, reason: str = "stopped by the platform") -> str:
        """Stop now rather than at the end of the lease."""
        with self._lock:
            self._lease_until = 0.0
        self._drop_demand()
        self.last_reason = reason
        return "posters off"

    @property
    def leased(self) -> bool:
        with self._lock:
            return time.monotonic() < self._lease_until

    # --- the gate -------------------------------------------------------

    def shed_reason(self) -> str:
        """Why this station is refusing to take a picture, or "" if it is not.

        THE STATION'S OWN DECISION, taken here rather than by the platform. The
        platform cannot see this site's battery in time to matter and would not
        be believed if it could: the box that browns out is the box that gets to
        say no. Same posture as the floodlight shed in `agent.py`, and the same
        threshold shape in `config.py`.

        UNKNOWN IS NOT LOW. A station with no power monitoring returns no
        reading at all, and refusing on that would mean every mains-powered
        station silently never posted. Only a reading that exists and is under
        the threshold refuses.

        AND SIMULATED IS NOT LOW EITHER. A station is routinely part real — the
        field box at Kennels Road has a live camera and a demo power head, and
        the demo bank drifts down to 2% whenever the simulated mains is
        simulated to be out. Stopping a real camera on the strength of an
        invented battery would take a station off the wall for a reason that
        does not exist anywhere but in `sensors/simulated.py`, and the symptom
        — a tile that goes blank at dusk — looks exactly like the hardware
        fault it is not. The floodlight shed can act on a simulated reading
        because on such a box the floodlight is simulated too and nothing real
        happens; here only one half is fiction, which is the whole difference.
        """
        simulated = getattr(self.agent, "sensor_is_simulated", None)
        if simulated is not None and simulated("power"):
            return ""
        soc = getattr(getattr(self.agent, "last_power", None), "soc_pct", None)
        if soc is None:
            return ""
        floor = getattr(self.agent.site, "shed_poster_below_soc_pct", 0.0)
        if soc >= floor:
            return ""
        return f"battery at {soc:.0f}%, below the {floor:.0f}% poster floor"

    # --- the loop -------------------------------------------------------

    def _drop_demand(self) -> None:
        """Take our appetite off the preview immediately.

        `release`, not merely letting the window lapse: the two cases that call
        this are a platform that has stopped watching and a battery too low to
        afford a capture, and both want the camera to stop within the second
        rather than at the end of a ninety-second window.
        """
        preview = getattr(self.agent, "video", None)
        if preview is not None and hasattr(preview, "release"):
            preview.release(self.CALLER)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a dead thread is a dead poster
                log.exception("Poster tick failed; continuing.")
            self._stop.wait(POLL_S)

    def tick(self) -> bool:
        """One pass. True if a poster went out.

        Public so a test can drive it without a thread, exactly as
        `CameraPreview.cycle` is.
        """
        if not self.leased:
            return False

        shed = self.shed_reason()
        if shed:
            # COUNTED ONCE PER REFUSED POSTER, not once per poll. This loop
            # wakes twice a second; counting here unconditionally would have a
            # station that spent one night on a flat battery reporting a hundred
            # thousand "refusals" against a handful of sends, and the ratio is
            # the only thing that number is for. Paced against the same clock a
            # send uses, so `refused` counts the pictures the wall did not get.
            now = time.monotonic()
            with self._lock:
                interval = self._interval
            # Its OWN clock, not the send pacer. Sharing `_sent_at` would make
            # every refusal push the next upload a full interval away, so a
            # station whose battery recovered thirty seconds into a minute
            # would sit on a good picture until the minute was up — the wall
            # staying blank for a site that had just come back is the opposite
            # of what the recovery is worth reporting.
            if now - self._refused_at >= interval:
                self._refused_at = now
                self.refused += 1
            if self.last_reason != shed:
                log.warning("Refusing posters: %s.", shed)
            self.last_reason = shed
            # Dropped rather than merely not renewed: the point of refusing is
            # that the camera stops NOW.
            self._drop_demand()
            return False

        preview = getattr(self.agent, "video", None)
        if preview is None:
            self.last_reason = "no camera preview on this station"
            return False

        with self._lock:
            interval = self._interval
            lease_left = max(0.0, self._lease_until - time.monotonic())
        # The demand window is the REST OF THE LEASE, not one interval. A window
        # of exactly one interval would lapse in the instant between the
        # preview's last capture and this thread noticing it, and every single
        # capture would be a cold start against a camera that had just been let
        # go.
        #
        # CAPPED AT THE LEASE, never `max`ed past it. It used to be
        # `max(interval * 1.5, lease_left)`, which meant that in the last
        # seconds of a lease the station asked the camera to stay warm for
        # another ninety — so a platform that stopped renewing still had a
        # camera running a minute and a half after its authority ran out. The
        # floor now applies only while the lease has room for it.
        window = min(max(interval * 1.5, POLL_S * 4), lease_left)
        preview.request(
            interval_s=interval,
            window_s=window,
            caller=self.CALLER,
        )

        # **The interval bounds the UPLINK, not just the camera.**
        #
        # This was the hole. `interval_s` was passed to the preview and nothing
        # else, so the only gate on sending was "is this a frame I have not sent
        # before" — and the preview's cadence is the FASTEST any caller wants,
        # not ours. A technician opening the setup page drives captures every
        # two seconds, and this thread would have faithfully uploaded all of
        # them: thirty posters a minute instead of one, on a metered link, for
        # a tile that redraws once a minute. The cost argument this whole
        # feature rests on would have been wrong by thirty times, and only while
        # somebody was standing at the site, which is the hardest time to
        # notice it.
        #
        # Sharing captures with the setup page is still right — one capture
        # serves everybody. Sharing its RATE is not.
        since_sent = time.monotonic() - self._sent_at
        if since_sent < interval:
            return False

        frame = preview.last_frame
        if frame is None:
            self.last_reason = preview.last_reason or "no frame yet"
            return False
        if frame.captured_at == self._last_sent_at:
            # The preview has not taken a new one since we last sent. Not a
            # failure and not worth a log line: it is the normal state for
            # fifty-nine of every sixty seconds.
            return False

        if not self.url or not self.secret:
            self.last_reason = "not enrolled"
            return False

        # STAMPED BEFORE THE ATTEMPT, not after. Everything below can raise —
        # `_context()` calls `trust.context()`, which refuses outright when the
        # pinned CA is missing — and an exception here would escape `tick` with
        # the pacing clock never updated. The thread's own handler catches it
        # and comes round in half a second, and the interval gate above would
        # wave it straight through: an ffmpeg spawn and a traceback twice a
        # second, for ever, on a board that browns out under load. Pacing has
        # to survive the failure it is most needed for.
        self._sent_at = time.monotonic()
        self._last_sent_at = frame.captured_at

        try:
            context = self._context()
        except Exception as exc:  # noqa: BLE001 - reported, never raised upward
            self.failed += 1
            self.last_reason = f"no usable TLS trust: {exc}"[:200]
            log.warning("Poster not sent: %s", self.last_reason)
            return False

        jpeg = scale(frame.jpeg, ffmpeg=self._ffmpeg())
        ok, reason = send(
            url=self.url,
            secret=self.secret,
            jpeg=jpeg,
            captured_at=frame.captured_at.isoformat(),
            width=frame.width,
            height=frame.height,
            context=context,
        )
        if ok:
            self.sent += 1
            self.last_reason = ""
            return True
        self.failed += 1
        self.last_reason = reason
        log.warning("Poster not sent: %s", reason)
        return False

    def _ffmpeg(self) -> str | None:
        """The ffmpeg the camera driver already found, if there is one. Asked
        of the driver rather than resolved again here, so a box with ffmpeg
        somewhere unusual does not have to be told twice."""
        camera = getattr(self.agent, "camera", None)
        return getattr(camera, "_ffmpeg", None)

    def _context(self):
        """The station's pinned TLS context, or None for a plaintext dev URL —
        the same test `EnrolmentClient` makes, for the same reason: a refusal
        must never be reachable by retrying on weaker terms."""
        if not (self.url or "").startswith("https"):
            return None
        return self.trust.context() if self.trust is not None else None

    # --- what it is doing ------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            leased = time.monotonic() < self._lease_until
            interval = self._interval
        return {
            "leased": leased,
            "interval_s": round(interval, 1),
            "sent": self.sent,
            "failed": self.failed,
            # Named `refused` rather than folded into `failed`: a station
            # deliberately protecting its battery and a station that cannot
            # reach the platform look identical in a single counter, and they
            # want opposite responses.
            "refused": self.refused,
            "reason": self.last_reason,
        }
