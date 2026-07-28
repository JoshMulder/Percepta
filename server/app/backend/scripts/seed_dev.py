"""Create a development org, users and ground stations.

    docker compose exec app python -m backend.scripts.seed_dev

Idempotent: re-running leaves existing rows alone. Creates two organisations so
tenant isolation is visible in the UI rather than only in tests, and three users
with deliberately different access so the capability-driven console can be seen
doing its job:

    admin@percepta.local     admin of Northern Grid - every station, everything
    operator@percepta.local  granted two stations, no camera control on one
    viewer@percepta.local    read-only ceiling, even where granted more

Passwords are printed once at the end. This is development seeding and says so;
it has no place in a production bring-up.
"""

import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from backend.auth.capabilities import Capability
from backend.auth.password import hash_password
from backend.database.models.device import Device, DeviceKind
from backend.database.models.enums import UserRole
from backend.database.models.ground_station import GroundStation
from backend.database.models.organization import Organization
from backend.database.models.organization_membership import OrganizationMembership
from backend.database.models.station_grant import StationGrant
from backend.database.models.user import User
from backend.database.session import PrivilegedSessionLocal

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed")

PASSWORD = "percepta-dev-2026"

STATIONS = [
    ("Kaikoura Ridge", "Pacific/Auckland", -42.4004, 173.6800),
    ("Rakaia Gorge", "Pacific/Auckland", -43.5321, 171.6900),
    ("Mount Cook Approach", "Pacific/Auckland", -43.7340, 170.0960),
]

DEVICES = [
    (DeviceKind.CAMERA, "cam-north", "North PTZ"),
    (DeviceKind.RADIO, "airband", "Airband receiver"),
    (DeviceKind.ADSB, "adsb", "ADS-B receiver"),
    (DeviceKind.WEATHER, "weather", "Weather station"),
    (DeviceKind.LIGHT, "flood-1", "Floodlight"),
    (DeviceKind.POWER, "solar", "Solar array"),
    (DeviceKind.LINK, "starlink", "Starlink terminal"),
]


def get_or_create_org(db, name: str) -> Organization:
    org = db.execute(
        select(Organization).where(Organization.name == name)
    ).scalar_one_or_none()
    if org is None:
        org = Organization(id=uuid.uuid4(), name=name)
        db.add(org)
        db.flush()
        log.info("  created organisation %s", name)
    return org


def get_or_create_user(db, email: str, display: str) -> User:
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        first, _, last = display.partition(" ")
        user = User(
            id=uuid.uuid4(),
            email=email,
            display_name=display,
            first_name=first or display,
            last_name=last or None,
            password_hash=hash_password(PASSWORD),
        )
        db.add(user)
        db.flush()
        log.info("  created user %s", email)
    return user


def ensure_membership(db, user: User, org: Organization, roles: list[str]) -> None:
    existing = db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.organization_id == org.id,
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            OrganizationMembership(
                id=uuid.uuid4(),
                user_id=user.id,
                organization_id=org.id,
                roles=roles,
            )
        )
        db.flush()


def ensure_station(db, org: Organization, name, tz, lat, lon) -> GroundStation:
    station = db.execute(
        select(GroundStation).where(
            GroundStation.organization_id == org.id, GroundStation.name == name
        )
    ).scalar_one_or_none()
    if station is None:
        station = GroundStation(
            id=uuid.uuid4(),
            organization_id=org.id,
            name=name,
            timezone=tz,
            latitude=lat,
            longitude=lon,
            enrolled_at=datetime.now(UTC),
            # Staggered so the console shows a mix of online and stale, which is
            # what a real fleet on intermittent backhaul looks like.
            last_seen_at=datetime.now(UTC) - timedelta(seconds=10),
        )
        db.add(station)
        db.flush()
        log.info("  created station %s", name)
        for kind, slug, label in DEVICES:
            db.add(
                Device(
                    id=uuid.uuid4(),
                    organization_id=org.id,
                    ground_station_id=station.id,
                    kind=kind.value,
                    slug=slug,
                    name=label,
                )
            )
        db.flush()
    return station


def ensure_grant(db, org, user, station, capabilities: list[str]) -> None:
    existing = db.execute(
        select(StationGrant).where(
            StationGrant.user_id == user.id,
            StationGrant.ground_station_id == station.id,
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            StationGrant(
                id=uuid.uuid4(),
                organization_id=org.id,
                user_id=user.id,
                ground_station_id=station.id,
                capabilities=capabilities,
            )
        )
        db.flush()


def main() -> int:
    with PrivilegedSessionLocal() as db:
        log.info("Seeding development data.")

        northern = get_or_create_org(db, "Northern Grid")
        # A second org exists so isolation is visible in the UI, not only in
        # tests: logging in as its admin must show none of Northern Grid's
        # stations.
        southern = get_or_create_org(db, "Southern Watch")

        admin = get_or_create_user(db, "admin@percepta.local", "Ada Admin")
        operator = get_or_create_user(db, "operator@percepta.local", "Otto Operator")
        viewer = get_or_create_user(db, "viewer@percepta.local", "Vera Viewer")
        other = get_or_create_user(db, "other@percepta.local", "Otis Other")

        ensure_membership(db, admin, northern, [UserRole.ADMIN.value])
        ensure_membership(db, operator, northern, [UserRole.OPERATOR.value])
        ensure_membership(db, viewer, northern, [UserRole.VIEWER.value])
        ensure_membership(db, other, southern, [UserRole.ADMIN.value])

        stations = [ensure_station(db, northern, *s) for s in STATIONS]
        ensure_station(db, southern, "Fiordland North", "Pacific/Auckland",
                       -45.4167, 167.7167)

        # Operator: full-ish on the first station, deliberately thinner on the
        # second, and no grant at all on the third - so the console can be seen
        # rendering three different states for one user.
        ensure_grant(db, northern, operator, stations[0], [
            Capability.STATION_VIEW.value,
            Capability.TELEMETRY_VIEW.value,
            Capability.VIDEO_VIEW.value,
            Capability.VIDEO_PTZ.value,
            Capability.RADIO_LISTEN.value,
            Capability.RADIO_CONTROL.value,
            Capability.LIGHT_CONTROL.value,
            Capability.MEDIA_REVIEW.value,
        ])
        ensure_grant(db, northern, operator, stations[1], [
            Capability.STATION_VIEW.value,
            Capability.TELEMETRY_VIEW.value,
            Capability.RADIO_LISTEN.value,
        ])

        # Viewer is granted actuator capabilities it must never actually get -
        # the ceiling should strip them, and being able to see that in the UI is
        # the point.
        ensure_grant(db, northern, viewer, stations[0], [
            Capability.STATION_VIEW.value,
            Capability.TELEMETRY_VIEW.value,
            Capability.VIDEO_VIEW.value,
            Capability.VIDEO_PTZ.value,
            Capability.LIGHT_CONTROL.value,
        ])

        db.commit()

    log.info("\nDone. Development accounts (password: %s)\n", PASSWORD)
    log.info("  admin@percepta.local     Northern Grid admin - all 3 stations")
    log.info("  operator@percepta.local  2 stations, different access on each")
    log.info("  viewer@percepta.local    read-only ceiling on station 1")
    log.info("  other@percepta.local     Southern Watch - must see none of the above")
    return 0


if __name__ == "__main__":
    sys.exit(main())
