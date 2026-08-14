"""The projection both fleet paths share, and the drift it exists to stop.

Two things are pinned here.

THE KEY NAMES. `worst_condition` was read as `code`/`name`/`message` for the
whole life of the field, and a condition on the wire carries none of those — so
every station on the fleet view reported no worst condition, for ever, and it
looked exactly like a healthy fleet. A test that asserts the projection produces
SOMETHING would have passed throughout; this one asserts it produces the
station's own identifier for the condition.

THE TWO PATHS AGREE. The wall reads the pushed digest when its socket is up and
the polled endpoint when it is not, and prefers the digest. A field present in
one and missing from the other is not a gap, it is a wall that shows LESS while
the live feed is healthy — which is the exact opposite of what an operator would
infer. Both now call one function, and the last test here is what keeps that
true when somebody adds the next field to whichever path they happened to open.
"""

from __future__ import annotations

from backend.services.station_vitals import (
    project_health,
    project_power,
    worst_condition_of,
)


# --------------------------------------------------------- the worst condition


def _condition(id_: str, severity: str) -> dict:
    """A condition exactly as station/gsu/health.py puts it on the wire."""
    return {
        "id": id_,
        "severity": severity,
        "detail": "a human sentence nobody should put on a tile",
        "since": "2026-08-14T03:00:00+00:00",
    }


def test_worst_condition_is_named_by_the_stations_own_id() -> None:
    name, count = worst_condition_of([_condition("disk.low", "warning")])
    assert name == "disk.low"
    assert count == 1


def test_worst_condition_does_not_fall_back_to_the_detail_sentence() -> None:
    # `detail` is a log line. A tile has room for a name, and putting a sentence
    # there is how a wall stops being scannable.
    name, _ = worst_condition_of([_condition("disk.low", "warning")])
    assert "sentence" not in (name or "")


def test_the_worst_severity_wins() -> None:
    name, count = worst_condition_of([
        _condition("clock.drift", "info"),
        _condition("power.battery", "critical"),
        _condition("disk.low", "warning"),
    ])
    assert name == "power.battery"
    assert count == 3


def test_an_unknown_severity_never_outranks_a_known_critical() -> None:
    # Ranking an unfamiliar word above critical would let a station bury its own
    # worst news by inventing a severity.
    name, _ = worst_condition_of([
        _condition("power.battery", "critical"),
        _condition("mystery", "spicy"),
    ])
    assert name == "power.battery"


def test_no_conditions_is_none_and_zero() -> None:
    assert worst_condition_of([]) == (None, 0)
    assert worst_condition_of(None) == (None, 0)


# ------------------------------------------------------------ health and power


def _health_frame() -> dict:
    return {
        "status": "warning",
        "conditions": [_condition("uplink.down", "warning")],
        "uplink": {"connected": False, "offline_seconds": 412.0},
        "devices": [
            {"slot": "radio", "status": "ok"},
            {"slot": "power", "status": "ok", "simulated": True},
            {"slot": "camera", "status": "degraded"},
        ],
        "software": {"running_version": "v0.2.2"},
        "agent_version": "v0.2.1",
    }


def test_health_projection_carries_the_three_fields_the_digest_had_lost() -> None:
    # The regression this file exists for.
    v = project_health(_health_frame())
    assert v["worst_condition"] == "uplink.down"
    assert v["uplink_offline_seconds"] == 412.0
    assert v["running_version"] == "v0.2.2"


def test_agent_version_is_the_running_version_fallback() -> None:
    # A station predating the software block still reports one, and "unknown
    # version" on a wall is a question somebody has to answer by hand.
    frame = _health_frame()
    del frame["software"]
    assert project_health(frame)["running_version"] == "v0.2.1"


def test_simulated_slots_are_sorted() -> None:
    frame = _health_frame()
    frame["devices"] = [
        {"slot": "weather", "status": "ok", "simulated": True},
        {"slot": "adsb", "status": "ok", "simulated": True},
    ]
    # Unsorted, a tile appears to change when the station merely enumerated its
    # hardware in a different order.
    assert project_health(frame)["simulated_slots"] == ["adsb", "weather"]


def test_a_malformed_frame_costs_only_the_fields_it_malformed() -> None:
    v = project_health({"status": "ok", "conditions": "not a list", "devices": 7})
    assert v["health"] == "ok"
    assert v["worst_condition"] is None
    assert v["condition_count"] == 0
    assert "slots" not in v


def test_on_battery_is_none_when_the_station_reports_neither_source() -> None:
    # Not False. An off-grid site has nothing to say here, and guessing would put
    # a battery warning on every such station in the fleet.
    assert "on_battery" not in project_power({"soc_pct": 74.0})
    assert project_power({"soc_pct": 74.0, "mains_w": 0})["on_battery"] is True
    assert project_power({"soc_pct": 74.0, "mains_w": 120})["on_battery"] is False


# ------------------------------------------------- the two paths cannot drift


def test_every_projected_key_is_a_field_the_wall_can_render() -> None:
    """The projection and the polled response model agree.

    Both fleet paths now share `project_health`/`project_power`, so the digest
    cannot silently carry less than the poll. What is still possible is the
    projection growing a key the response model does not declare — which
    reaches the pushed feed (a hand-built dict) and is dropped from the polled
    one (a Pydantic model), reintroducing the same asymmetry from the other end.
    """
    from backend.api.platform import FleetStation

    projected = set(project_health(_health_frame())) | set(
        project_power({"soc_pct": 74.0, "load_w": 12.0, "mains_w": 0})
    )
    declared = set(FleetStation.model_fields)
    assert projected <= declared, (
        f"projected but undeclared: {sorted(projected - declared)}"
    )
