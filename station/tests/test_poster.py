"""The wall's stills: when they are sent, and when the station refuses.

The tests worth having here are the refusals. Sending a picture is the easy
path and it is one POST; what this module actually exists to get right is
everything that stops a picture being taken — a lapsed lease, a flat battery, a
frame that has not changed, a camera that never answered — because each of those
is a way the wall could quietly cost a field station its afternoon.

Driven through `tick()` with a fake clock rather than by running the thread: the
thread's only job is to call `tick()` twice a second, and every decision lives
in `tick()`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import gsu.poster as poster_mod
from gsu.camera import Frame


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _Preview:
    """Stands in for CameraPreview: records demands, hands out a frame."""

    def __init__(self, frame=None) -> None:
        self.last_frame = frame
        self.last_reason = ""
        self.demands: dict[str, tuple] = {}
        self.released: list[str] = []

    def request(self, interval_s=None, *, window_s=None, caller="console") -> None:
        self.demands[caller] = (interval_s, window_s)

    def release(self, caller: str) -> None:
        self.released.append(caller)
        self.demands.pop(caller, None)


class _Site:
    shed_poster_below_soc_pct = 20.0


class _Reading:
    def __init__(self, soc_pct: float) -> None:
        self.soc_pct = soc_pct


class _Agent:
    def __init__(self, preview=None, soc=None, simulated_power=False) -> None:
        self.video = preview
        self.site = _Site()
        self.last_power = None if soc is None else _Reading(soc)
        self.camera = None
        self._simulated_power = simulated_power

    def sensor_is_simulated(self, kind: str) -> bool:
        return kind == "power" and self._simulated_power


def _frame(at: datetime | None = None) -> Frame:
    # Not a real JPEG: nothing in these tests decodes it. `scale` is stubbed out
    # wherever it would run, because shelling out to ffmpeg to prove a lease
    # expired would be testing ffmpeg.
    return Frame(
        jpeg=b"\xff\xd8not-really-a-jpeg\xff\xd9",
        width=1920,
        height=1080,
        captured_at=at or datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc),
    )


def _publisher(monkeypatch, *, preview=None, soc=None, sends=None,
               simulated_power=False):
    clock = _Clock()
    monkeypatch.setattr(poster_mod.time, "monotonic", clock)
    monkeypatch.setattr(poster_mod, "scale", lambda jpeg, **kw: jpeg)
    if sends is not None:
        def record(**kwargs):
            sends.append(kwargs)
            return True, ""
        monkeypatch.setattr(poster_mod, "send", record)
    agent = _Agent(preview=preview, soc=soc, simulated_power=simulated_power)
    publisher = poster_mod.PosterPublisher(
        agent, url="https://platform.example/media/poster", secret="s3cret",
    )
    return publisher, clock


# --- the lease ----------------------------------------------------------


def test_nothing_happens_at_all_without_a_lease(monkeypatch):
    """The default state of every station, nearly all the time.

    No demand on the preview is the assertion that matters — not merely "no
    poster sent". A publisher that asked for frames and threw them away would
    pass a send-count test and still hold the camera open for ever.
    """
    preview = _Preview(_frame())
    publisher, _ = _publisher(monkeypatch, preview=preview, sends=[])

    assert publisher.tick() is False
    assert preview.demands == {}


def test_a_lapsed_lease_stops_the_pictures(monkeypatch):
    # Silence is the stop signal — the platform never has to say goodbye, which
    # is the whole reason leases exist rather than an on/off command.
    sends: list = []
    preview = _Preview(_frame())
    publisher, clock = _publisher(monkeypatch, preview=preview, sends=sends)

    publisher.request(lease_seconds=60)
    assert publisher.tick() is True
    assert len(sends) == 1

    clock.advance(61)
    preview.last_frame = _frame(datetime(2026, 8, 17, 3, 5, tzinfo=timezone.utc))
    assert publisher.tick() is False
    assert len(sends) == 1


def test_a_shorter_renewal_shortens_the_lease(monkeypatch):
    # Replaces, never extends. Otherwise the platform cannot change its mind
    # faster than it once promised.
    publisher, clock = _publisher(monkeypatch, preview=_Preview(), sends=[])
    publisher.request(lease_seconds=600)
    publisher.request(lease_seconds=10)

    clock.advance(20)
    assert publisher.leased is False


def test_a_lease_the_platform_states_is_clamped(monkeypatch):
    # The platform states a number; the station decides what it will hold to.
    # An hour-long lease would make "silence is the stop signal" a promise with
    # no number behind it.
    publisher, clock = _publisher(monkeypatch, preview=_Preview(), sends=[])
    publisher.request(lease_seconds=99999)

    clock.advance(poster_mod.LEASE_MAX_S + 1)
    assert publisher.leased is False


def test_releasing_drops_the_demand_immediately(monkeypatch):
    preview = _Preview(_frame())
    publisher, _ = _publisher(monkeypatch, preview=preview, sends=[])
    publisher.request(lease_seconds=600)
    publisher.tick()
    assert "poster" in preview.demands

    publisher.release()
    assert preview.released == ["poster"]
    assert publisher.leased is False


# --- the battery gate ---------------------------------------------------


def test_a_flat_battery_refuses_and_lets_the_camera_go(monkeypatch):
    """The reason this module is more than a POST.

    Dropping the demand matters more than not sending: the picture costs a few
    kilobytes, but the CAPTURE is an RTSP handshake, a decode and a JPEG encode
    on a board that has already stopped executing once with its core rail at
    7.7 A. Refusing to send while still asking for frames would protect the
    wrong thing entirely.
    """
    sends: list = []
    preview = _Preview(_frame())
    publisher, _ = _publisher(monkeypatch, preview=preview, soc=11.0, sends=sends)
    publisher.request(lease_seconds=600)

    assert publisher.tick() is False
    assert sends == []
    assert preview.released == ["poster"]
    assert publisher.refused == 1
    assert "11%" in publisher.last_reason


def test_a_healthy_battery_does_not_refuse(monkeypatch):
    sends: list = []
    publisher, _ = _publisher(
        monkeypatch, preview=_Preview(_frame()), soc=55.0, sends=sends)
    publisher.request(lease_seconds=600)

    assert publisher.tick() is True
    assert len(sends) == 1


def test_an_unknown_battery_is_not_a_flat_one(monkeypatch):
    """A station with no power monitoring must still post.

    Treating "no reading" as "below the floor" would silently switch every
    mains-powered station off the wall, and the failure would look exactly like
    a broken camera.
    """
    sends: list = []
    publisher, _ = _publisher(
        monkeypatch, preview=_Preview(_frame()), soc=None, sends=sends)
    publisher.request(lease_seconds=600)

    assert publisher.shed_reason() == ""
    assert publisher.tick() is True
    assert len(sends) == 1


def test_a_simulated_battery_never_stops_a_real_camera(monkeypatch):
    """The Kennels Road case: live camera, demo power head.

    The simulated bank drifts down to 2% whenever the simulated mains is
    simulated to be out. Acting on that would take a real station off the wall
    for a reason that exists nowhere but in `sensors/simulated.py`, and the
    symptom — a tile going blank at dusk — is indistinguishable from the
    hardware fault it is not.
    """
    sends: list = []
    publisher, _ = _publisher(
        monkeypatch, preview=_Preview(_frame()), soc=3.0,
        simulated_power=True, sends=sends,
    )
    publisher.request(lease_seconds=600)

    assert publisher.shed_reason() == ""
    assert publisher.tick() is True
    assert len(sends) == 1


def test_the_refusal_lifts_on_its_own_when_the_sun_comes_up(monkeypatch):
    # The gate is a live condition of the site, not a property of the lease: it
    # has to be able to end mid-lease without the platform asking again.
    sends: list = []
    preview = _Preview(_frame())
    publisher, _ = _publisher(monkeypatch, preview=preview, soc=11.0, sends=sends)
    publisher.request(lease_seconds=600)
    assert publisher.tick() is False

    publisher.agent.last_power = _Reading(45.0)
    assert publisher.tick() is True
    assert len(sends) == 1
    assert publisher.last_reason == ""


# --- what actually gets sent --------------------------------------------


def test_the_same_picture_is_never_sent_twice(monkeypatch):
    """A camera the preview could not reach leaves its last frame standing.

    That is deliberate in `video.py` — a stale picture with a stated age beats
    no picture. Without this check the wall would be shown that one frame every
    half second, for ever, as though it were current.
    """
    sends: list = []
    preview = _Preview(_frame())
    publisher, _ = _publisher(monkeypatch, preview=preview, sends=sends)
    publisher.request(lease_seconds=600)

    assert publisher.tick() is True
    assert publisher.tick() is False
    assert publisher.tick() is False
    assert len(sends) == 1

    preview.last_frame = _frame(datetime(2026, 8, 17, 3, 1, tzinfo=timezone.utc))
    assert publisher.tick() is True
    assert len(sends) == 2


def test_a_failed_send_waits_for_the_next_frame(monkeypatch):
    # Rather than retrying the same upload twice a second at a platform that
    # just refused it. The next capture is a minute away and brings a better
    # picture anyway.
    attempts: list = []

    def refuse(**kwargs):
        attempts.append(kwargs)
        return False, "platform said 503"

    preview = _Preview(_frame())
    publisher, _ = _publisher(monkeypatch, preview=preview)
    monkeypatch.setattr(poster_mod, "send", refuse)
    publisher.request(lease_seconds=600)

    assert publisher.tick() is False
    assert publisher.tick() is False
    assert len(attempts) == 1
    assert publisher.failed == 1
    assert "503" in publisher.last_reason


def test_the_demand_window_covers_the_rest_of_the_lease(monkeypatch):
    """A window of one interval would make every capture a cold start.

    The preview's demand would lapse in the gap between its last capture and
    this thread noticing it, so the camera would be let go and re-opened sixty
    times an hour instead of held warm.
    """
    preview = _Preview(_frame())
    publisher, _ = _publisher(monkeypatch, preview=preview, sends=[])
    publisher.request(lease_seconds=300, interval_s=60)
    publisher.tick()

    interval, window = preview.demands["poster"]
    assert interval == 60
    assert window >= 299


def test_no_credential_means_no_post(monkeypatch):
    sends: list = []
    publisher, _ = _publisher(
        monkeypatch, preview=_Preview(_frame()), sends=sends)
    publisher.secret = ""
    publisher.request(lease_seconds=600)

    assert publisher.tick() is False
    assert sends == []
    assert publisher.last_reason == "not enrolled"


# --- the sender itself ---------------------------------------------------


def test_an_oversized_frame_is_refused_before_the_link(monkeypatch):
    # On the station, so a camera misconfigured to full resolution costs nothing
    # on a metered link at all rather than being discovered on a data bill.
    ok, reason = poster_mod.send(
        url="https://platform.example/media/poster",
        secret="s",
        jpeg=b"x" * (poster_mod.MAX_POSTER_BYTES + 1),
        captured_at="2026-08-17T03:00:00+00:00",
        width=1920,
        height=1080,
    )
    assert ok is False
    assert "too large" in reason


def test_scale_returns_the_original_when_there_is_no_ffmpeg():
    # A CSI-camera box that never needed ffmpeg still posts, just at whatever
    # size the camera gave. The size cap is what keeps that bounded.
    original = b"\xff\xd8jpeg\xff\xd9"
    assert poster_mod.scale(original, ffmpeg=None) is original
