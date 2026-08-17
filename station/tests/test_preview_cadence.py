"""How often the preview actually takes a picture.

THE MECHANISM HAD NO CADENCE. `request()` wrote one float — a deadline — and the
capture gate was "inside the window, and at least MIN_INTERVAL_S since the last
attempt". There was nowhere to say how OFTEN, so a caller that wanted a frame a
minute got one every two seconds: thirty captures, twenty-nine of them thrown
away, each an ffmpeg spawn with an RTSP handshake, a keyframe wait, a decode and
a JPEG encode.

That was tolerable while the only caller was the setup page — a person standing
in front of the box, for a few minutes. It stops being tolerable the moment a
platform asks for periodic posters from every station, permanently, on hardware
whose failure mode is sustained load.

Driven through the `wanted` gate with a fake clock rather than by running the
thread: the thread's job is to call `cycle()` when `wanted` says so, and `wanted`
is where the whole decision lives.
"""

from __future__ import annotations

import gsu.video as video


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _preview(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(video.time, "monotonic", clock)
    preview = video.CameraPreview(agent=None)
    return preview, clock


def test_a_slow_caller_is_served_slowly(monkeypatch):
    """The bug, stated as a test.

    Ask for a frame a minute; do not get one every two seconds.
    """
    preview, clock = _preview(monkeypatch)
    preview.request(interval_s=60, window_s=90)

    assert preview.wanted is True
    preview._last_attempt = clock.t          # the thread just captured

    clock.advance(2.5)                        # past MIN_INTERVAL_S
    assert preview.wanted is False, "captured at the floor, not at the request"

    clock.advance(30.0)                       # 32.5s in
    assert preview.wanted is False

    clock.advance(30.0)                       # 62.5s in
    assert preview.wanted is True


def test_the_setup_page_still_gets_frames_as_fast_as_before(monkeypatch):
    # `request()` with no interval means "as often as you like", which is what
    # the console has always meant. This path must not change: somebody is
    # standing in front of the box aiming a camera.
    preview, clock = _preview(monkeypatch)
    preview.request()

    assert preview.wanted is True
    preview._last_attempt = clock.t
    clock.advance(video.MIN_INTERVAL_S + 0.1)
    assert preview.wanted is True


def test_a_human_at_the_setup_page_is_not_slowed_by_the_wall(monkeypatch):
    """Two callers, different appetites, and the human is not made to wait.

    This is the whole reason demand is per caller. The wall watches every
    station permanently at a frame a minute; if that one number were shared, an
    installer who opens the setup page to aim a camera would see it update once
    a minute and reasonably conclude the camera was broken. One capture serves
    both callers, so the camera runs at the faster of the two and the wall gets
    fresher pictures for nothing.
    """
    preview, clock = _preview(monkeypatch)
    preview.request(interval_s=60, window_s=90, caller="poster")
    preview.request(caller="console")

    preview._last_attempt = clock.t
    clock.advance(video.MIN_INTERVAL_S + 0.1)
    assert preview.wanted is True, "the wall must not slow the setup page down"


def test_the_wall_alone_is_served_at_its_own_slow_rate(monkeypatch):
    """And the converse, which is the original bug.

    The moment the human leaves, their appetite must leave with them. Nothing
    tells the station they closed the tab — the demand simply expires — and the
    camera has to fall back to a frame a minute rather than staying hot.
    """
    preview, clock = _preview(monkeypatch)
    preview.request(interval_s=60, window_s=300, caller="poster")
    preview.request(window_s=10, caller="console")

    preview._last_attempt = clock.t
    clock.advance(20.0)                       # the console's demand lapses
    assert preview.wanted is False, "the console's appetite outlived the console"

    clock.advance(45.0)                       # 65 s since the last capture
    assert preview.wanted is True


def test_releasing_a_demand_stops_it_before_the_window_runs_out(monkeypatch):
    # The station refusing its own poster on low battery cannot afford to wait
    # out a 90-second window: those are the exact seconds the refusal exists to
    # protect.
    preview, clock = _preview(monkeypatch)
    preview.request(interval_s=60, window_s=300, caller="poster")
    assert preview.wanted is True

    preview.release("poster")
    assert preview.wanted is False


def test_two_requests_from_one_caller_are_one_appetite(monkeypatch):
    # The setup page polls every 2.5 s. Each poll is the same watcher saying so
    # again, not a new one — otherwise the dict would grow without bound for as
    # long as anybody left the page open.
    preview, clock = _preview(monkeypatch)
    preview.request(caller="console")
    preview.request(caller="console")
    assert preview.stats()["watchers"] == ["console"]


def test_an_interval_below_the_floor_is_raised_to_it(monkeypatch):
    # A caller may ask for LESS often and never for more. The link and the power
    # budget belong to the site, not to whoever is looking.
    preview, clock = _preview(monkeypatch)
    preview.request(interval_s=0.1)

    preview._last_attempt = clock.t
    clock.advance(0.5)
    assert preview.wanted is False
    clock.advance(video.MIN_INTERVAL_S)
    assert preview.wanted is True


def test_a_lapsed_caller_leaves_nothing_behind(monkeypatch):
    """A finished slow caller must not leave the camera slow for the next one.

    Without expiry per caller, one platform request for a frame a minute would
    make the setup page useless for the following minute — an installer aiming
    a camera and seeing it update once.
    """
    preview, clock = _preview(monkeypatch)
    preview.request(interval_s=60, window_s=10, caller="poster")

    clock.advance(20.0)                       # the poster's demand lapses
    assert preview.wanted is False
    assert preview.stats()["watchers"] == []

    preview.request(caller="console")         # the setup page comes back
    preview._last_attempt = clock.t
    clock.advance(video.MIN_INTERVAL_S + 0.1)
    assert preview.wanted is True


def test_a_long_window_holds_demand_open_between_slow_requests(monkeypatch):
    """A caller asking once a minute needs a window longer than a minute.

    With the default ten-second window it would lapse fifty seconds before the
    next request arrived, and every capture would be a cold start.
    """
    preview, clock = _preview(monkeypatch)
    preview.request(interval_s=60, window_s=90, caller="poster")

    clock.advance(70.0)
    assert preview.stats()["watching"] is True


def test_the_stats_say_what_cadence_is_in_force(monkeypatch):
    # In `stats()` — "what the preview is doing" — rather than in
    # `preview_state()`, which is what the setup page renders. Visible rather
    # than inferred: without it the only way to know how hard the camera is
    # being driven is to watch how often the picture changes.
    preview, clock = _preview(monkeypatch)
    preview.request(interval_s=60, window_s=90)
    assert preview.stats()["interval_s"] == 60.0
