"""Host device stats: the /proc and /sys collector behind the Summary page."""

import time

from gsu.system import SystemStats


def test_read_is_empty_before_the_first_sample():
    # A page that loads before the first tick shows no device card rather than
    # a card full of blanks.
    assert SystemStats().read() == {}


def test_cpu_percent_needs_a_baseline():
    # The first sample has nothing to diff against, so it reports every other
    # field but not the busy fraction.
    stats = SystemStats()
    stats.sample()
    assert "cpu_percent" not in stats.read()


def test_sample_reads_the_host():
    # On the Linux test host /proc is real; assert the fields that are always
    # there. Temperature is not asserted — a dev box or CI runner has no
    # thermal zone, and its absence is by design, not a failure.
    stats = SystemStats()
    stats.sample()
    time.sleep(0.05)  # a real delta for the busy fraction
    stats.sample()
    result = stats.read()

    assert result["uptime_s"] >= 0
    assert result["memory"]["total_mb"] > 0
    assert 0.0 <= result["cpu_percent"] <= 100.0
    assert result["load_1m"] >= 0.0


def test_every_field_is_optional(monkeypatch):
    # A locked-down host where nothing is readable produces an empty dict, not
    # an exception — the card just does not render.
    import gsu.system as system

    def unreadable(*_args, **_kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(system.Path, "read_text", unreadable)
    monkeypatch.setattr(system.os, "getloadavg", unreadable)
    stats = SystemStats()
    stats.sample()
    assert stats.read() == {}
