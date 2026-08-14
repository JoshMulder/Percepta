"""The read-only ceiling on a cross-tenant watch.

Everything an Odin operator may do to somebody else's station, and nothing more.
This is the first code to enforce the ceiling that docs/03-realtime-isolation.md
section 9 has described in prose since before there was anything to enforce it
on.

ODIN_READ is a strict subset of READ_CAPABILITIES and contains NO ACTUATOR,
ever. Not light.control, not radio.control, not config.write, not
station.update, and certainly not radio.transmit. An operator on shift watches
every organisation at once; the one thing that must never follow from "I can see
your site" is "I can change it".

It is a separate constant rather than a filter over READ_CAPABILITIES because a
capability added to that set later would silently widen this one. A cross-tenant
grant should have to be typed out deliberately, by somebody who has thought
about it, in this file.

MEDIA_REVIEW is deliberately absent, and it is the interesting omission: it
grants access to a station's stored recordings, which is a different act from
watching a site live. Reaching into a customer's archive is a decision worth
making on its own rather than one that arrives free with a watch position.

This module does NOT touch capabilities_for or visible_station_ids. Both of
those carry post-mortems in their own source about exactly the kind of widening
that would be tempting here, and a fourth cross-tenant side channel built to the
template of the three that already work is the safer shape than a special case
threaded through the mechanism every tenant depends on.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.capabilities import Capability
from backend.database.models.ground_station import GroundStation

#: What a watch position may do to a station in someone else's organisation.
ODIN_READ = frozenset({
    Capability.STATION_VIEW,
    Capability.TELEMETRY_VIEW,
    Capability.RADIO_LISTEN,
    Capability.VIDEO_VIEW,
})


def odin_capabilities_for(db: Session, *, station_id: uuid.UUID) -> frozenset:
    """The capabilities a watch connection holds at this station.

    Takes no user and no organisation, and that is the point: the caller has
    already been established as platform watch staff, and from there the answer
    is the same for every station and every operator. There is nothing per-user
    to get wrong here, which is why the set can be a constant.

    Returns EMPTY for a station that is not active. A tenant deactivating a
    station is the lever by which they stop a cross-tenant listen, so it has to
    be checked here rather than assumed from the roster the wall was drawn from
    — the roster is up to thirty seconds old, and this is not.
    """
    active = db.execute(
        select(GroundStation.is_active).where(GroundStation.id == station_id)
    ).scalar_one_or_none()
    if not active:
        return frozenset()
    return ODIN_READ


def odin_capabilities_for_all(
    db: Session, *, station_ids: list[uuid.UUID]
) -> dict[uuid.UUID, frozenset]:
    """The same answer for a whole guard set, in ONE statement.

    The hub's revalidation sweep is serial across every connection once a
    minute; asking per station would multiply that by the size of each
    operator's guard list, for a question whose answer is one row each.
    """
    if not station_ids:
        return {}
    rows = db.execute(
        select(GroundStation.id).where(
            GroundStation.id.in_(station_ids),
            GroundStation.is_active.is_(True),
        )
    ).scalars().all()
    live = set(rows)
    return {sid: (ODIN_READ if sid in live else frozenset()) for sid in station_ids}
