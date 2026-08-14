"""What becomes an alert, what merely bumps one, and what is refused.

The refusals are the substance. An alert engine that raises everything is worse
than none at all: the rail stops being read, and it stops being read silently —
nothing about a rail nobody looks at says it has failed.

The case this suite exists for is the DOUBLE-RAISE. The station reports one
physical fault twice on adjacent lines: a health condition and an event, both
named power.battery, both named uplink.down. An engine that watched both would
open two alerts for one fault and send somebody to a site twice. Only the
condition self-clears, because the matching recovery events are severity `info`
and a raising policy would never look at them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.database.models.platform_alert import PlatformAlert, StationMaintenance
from backend.services import alert_engine
from backend.services.alerts import rules, store


def _alerts(db: Session, station_id) -> list[PlatformAlert]:
    return list(
        db.execute(
            select(PlatformAlert)
            .where(PlatformAlert.ground_station_id == station_id)
            .order_by(PlatformAlert.first_seen_at)
        ).scalars().all()
    )


def _health(*conditions: tuple[str, str]) -> dict:
    return {
        "kind": "health",
        "conditions": [
            {"id": cid, "severity": sev, "detail": ""} for cid, sev in conditions
        ],
    }


class TestTheDispositionRules:
    """Pure policy, no database. The table that prevents the double-raise."""

    def test_a_fault_with_a_condition_twin_is_owned_by_the_condition(self):
        for etype in ("power.battery", "uplink.down", "light.no_draw"):
            assert rules.disposition(etype, "warning") is rules.Disposition.CONDITION_OWNED

    def test_aircraft_and_transcripts_are_never_alerts(self):
        # adsb.proximity is emitted at WARNING on every close contact — 46 of 71
        # warnings on the live fleet in a day. radio.transmission was 291.
        assert rules.disposition("adsb.proximity", "warning") is rules.Disposition.NEVER
        assert rules.disposition("radio.transmission", "info") is rules.Disposition.NEVER

    def test_an_unknown_serious_type_still_raises(self):
        # The station owns this vocabulary and can extend it without a platform
        # release. Refusing the unrecognised would make a new fault type
        # invisible to the command centre until somebody edited a file.
        assert rules.disposition("cooling.fan_stalled", "critical") is rules.Disposition.EVENT_OWNED

    def test_an_unknown_chatty_type_does_not(self):
        # ...but a station cannot make the rail shout by inventing a word.
        assert rules.disposition("some.new.chatter", "info") is rules.Disposition.NEVER

    def test_recoveries_name_what_they_end(self):
        assert rules.closes("uplink.up") == "uplink.down"
        assert rules.closes("power.recovered") == "power.battery"
        assert rules.closes("power.battery") is None


class TestConditionsRaiseAndClearThemselves:
    def test_a_new_condition_opens_one_alert(self, db: Session, org, station):
        alert_engine.on_health(
            db,
            organization_id=org.id,
            station_id=station.id,
            previous=_health(),
            current=_health(("power.battery", "warning")),
        )
        db.commit()
        open_alerts = _alerts(db, station.id)
        assert len(open_alerts) == 1
        assert open_alerts[0].type == "power.battery"
        assert open_alerts[0].source == "condition"

    def test_the_same_condition_next_frame_does_not_open_another(
        self, db: Session, org, station
    ):
        frame = _health(("power.battery", "warning"))
        alert_engine.on_health(
            db, organization_id=org.id, station_id=station.id,
            previous=_health(), current=frame,
        )
        alert_engine.on_health(
            db, organization_id=org.id, station_id=station.id,
            previous=frame, current=frame,
        )
        db.commit()
        assert len(_alerts(db, station.id)) == 1

    def test_a_condition_disappearing_closes_its_alert(
        self, db: Session, org, station
    ):
        frame = _health(("uplink.down", "warning"))
        alert_engine.on_health(
            db, organization_id=org.id, station_id=station.id,
            previous=_health(), current=frame,
        )
        alert_engine.on_health(
            db, organization_id=org.id, station_id=station.id,
            previous=frame, current=_health(),
        )
        db.commit()
        alert = _alerts(db, station.id)[0]
        assert alert.state == "closed"
        # "resolved", not "manual": a fault that clears itself every night is a
        # different maintenance question from one somebody had to go and fix.
        assert alert.closed_reason == "resolved"


class TestTheDoubleRaise:
    def test_the_paired_event_bumps_the_condition_and_opens_nothing(
        self, db: Session, org, station
    ):
        # The station raises the condition...
        alert_engine.on_health(
            db, organization_id=org.id, station_id=station.id,
            previous=_health(), current=_health(("power.battery", "warning")),
        )
        # ...and records the event for the same physical fact.
        alert_engine.on_events(
            db, organization_id=org.id, station_id=station.id,
            events=[{"type": "power.battery", "severity": "warning",
                     "message": "battery low"}],
        )
        db.commit()

        alerts = _alerts(db, station.id)
        assert len(alerts) == 1, "one fault must not become two alerts"
        assert alerts[0].occurrences == 2, "the event is evidence on the alert"

    def test_the_event_alone_does_not_open_a_condition_owned_alert(
        self, db: Session, org, station
    ):
        # No condition raised. The event must not open anything, because nothing
        # would ever close it: the recovery event is severity info.
        alert_engine.on_events(
            db, organization_id=org.id, station_id=station.id,
            events=[{"type": "uplink.down", "severity": "warning"}],
        )
        db.commit()
        assert _alerts(db, station.id) == []

    def test_a_recovery_event_closes_the_condition_it_ends(
        self, db: Session, org, station
    ):
        alert_engine.on_health(
            db, organization_id=org.id, station_id=station.id,
            previous=_health(), current=_health(("uplink.down", "warning")),
        )
        alert_engine.on_events(
            db, organization_id=org.id, station_id=station.id,
            events=[{"type": "uplink.up", "severity": "info"}],
        )
        db.commit()
        assert _alerts(db, station.id)[0].state == "closed"


class TestNoise:
    def test_aircraft_proximity_raises_nothing(self, db: Session, org, station):
        alert_engine.on_events(
            db, organization_id=org.id, station_id=station.id,
            events=[
                {"type": "adsb.proximity", "severity": "warning"} for _ in range(20)
            ],
        )
        db.commit()
        assert _alerts(db, station.id) == []

    def test_a_flapping_fault_is_one_row_with_a_count(
        self, db: Session, org, station
    ):
        for _ in range(50):
            alert_engine.on_events(
                db, organization_id=org.id, station_id=station.id,
                events=[{"type": "video.stream_failed", "severity": "warning"}],
            )
        db.commit()
        alerts = _alerts(db, station.id)
        assert len(alerts) == 1
        assert alerts[0].occurrences == 50


def _user(db: Session, email: str):
    """A real row, because acked_by_user_id is a foreign key.

    The first draft of these tests acked with uuid4() and failed on the
    constraint — which is the constraint doing its job: an alert owned by a user
    who does not exist is an alert nobody can be asked about.
    """
    from backend.auth.password import hash_password
    from backend.database.models.user import User

    user = User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0],
        first_name="Test",
        last_name="Operator",
        password_hash=hash_password("not-used-by-these-tests"),
    )
    db.add(user)
    db.flush()
    return user


class TestOwnership:
    def test_ack_is_a_conditional_update(self, db: Session, org, station):
        alert = store.open_or_touch(
            db, organization_id=org.id, station_id=station.id,
            source="event", dedupe_key="video.stream_failed",
            type="video.stream_failed", severity="warning", title="stream failed",
        )
        db.commit()
        first = _user(db, "first@example.test").id
        second = _user(db, "second@example.test").id
        db.commit()

        store.ack(db, alert_id=alert.id, user_id=first)
        db.commit()
        # The second operator is refused, and told who has it — not silently
        # ignored, and not allowed to take it.
        with pytest.raises(store.AlreadyHeld) as held:
            store.ack(db, alert_id=alert.id, user_id=second)
        assert held.value.holder_id == first

    def test_re_acking_your_own_is_not_a_conflict(self, db: Session, org, station):
        alert = store.open_or_touch(
            db, organization_id=org.id, station_id=station.id,
            source="event", dedupe_key="video.stream_failed",
            type="video.stream_failed", severity="warning", title="stream failed",
        )
        db.commit()
        me = _user(db, "me@example.test").id
        db.commit()
        store.ack(db, alert_id=alert.id, user_id=me)
        db.commit()
        # A double click on a slow link must not read as somebody else taking it.
        assert store.ack(db, alert_id=alert.id, user_id=me).acked_by_user_id == me

    def test_acking_does_not_close(self, db: Session, org, station):
        alert = store.open_or_touch(
            db, organization_id=org.id, station_id=station.id,
            source="event", dedupe_key="video.stream_failed",
            type="video.stream_failed", severity="warning", title="stream failed",
        )
        db.commit()
        store.ack(db, alert_id=alert.id, user_id=_user(db, "ack@example.test").id)
        db.commit()
        # The station keeps its attention colour until the fault is CLOSED.
        # Conflating the two is how a command centre loses a fault.
        assert db.get(PlatformAlert, alert.id).state == "acked"


class TestSuppression:
    def test_a_station_under_maintenance_raises_nothing(
        self, db: Session, org, station
    ):
        now = datetime.now(UTC)
        db.add(StationMaintenance(
            id=uuid.uuid4(),
            organization_id=org.id,
            ground_station_id=station.id,
            from_at=now - timedelta(minutes=1),
            until_at=now + timedelta(hours=2),
            reason="swapping the receiver",
        ))
        db.commit()

        alert_engine.on_health(
            db, organization_id=org.id, station_id=station.id,
            previous=_health(), current=_health(("power.battery", "warning")),
        )
        db.commit()
        # Suppressed at RAISE time: no row at all, so it cannot fill the rail,
        # cannot chime, and cannot be counted as a fault.
        assert _alerts(db, station.id) == []

    def test_a_window_that_has_ended_suppresses_nothing(
        self, db: Session, org, station
    ):
        now = datetime.now(UTC)
        db.add(StationMaintenance(
            id=uuid.uuid4(),
            organization_id=org.id,
            ground_station_id=station.id,
            from_at=now - timedelta(hours=3),
            until_at=now - timedelta(hours=1),
            reason="finished an hour ago",
        ))
        db.commit()
        alert_engine.on_health(
            db, organization_id=org.id, station_id=station.id,
            previous=_health(), current=_health(("power.battery", "warning")),
        )
        db.commit()
        assert len(_alerts(db, station.id)) == 1


class TestDarkStations:
    def test_dark_is_announced_once_however_often_it_is_scanned(
        self, db: Session, org, station
    ):
        # The bug this replaces: an in-process set that every deploy forgot, so
        # every currently-dark station was re-announced as newly dark.
        for _ in range(5):
            alert_engine.on_dark(
                db, organization_id=org.id, station_id=station.id,
                station_name=station.name, minutes=30,
            )
        db.commit()
        alerts = _alerts(db, station.id)
        assert len(alerts) == 1
        assert alerts[0].severity == "critical"

    def test_coming_back_closes_it(self, db: Session, org, station):
        alert_engine.on_dark(
            db, organization_id=org.id, station_id=station.id,
            station_name=station.name, minutes=30,
        )
        alert_engine.on_heard_again(db, station_id=station.id)
        db.commit()
        assert _alerts(db, station.id)[0].state == "closed"

    def test_a_station_that_dies_again_is_announced_again(
        self, db: Session, org, station
    ):
        alert_engine.on_dark(
            db, organization_id=org.id, station_id=station.id,
            station_name=station.name, minutes=30,
        )
        alert_engine.on_heard_again(db, station_id=station.id)
        alert_engine.on_dark(
            db, organization_id=org.id, station_id=station.id,
            station_name=station.name, minutes=20,
        )
        db.commit()
        # The unique index is partial on state <> 'closed', which is what allows
        # a second alert once the first is closed — and forbids one while it is
        # still open.
        assert len(_alerts(db, station.id)) == 2
