"""Platform administration: who may act across organisations.

Kept separate from `auth/authorization.py`, which answers "may this person do
this at that station". This answers a different and larger question - "may this
person see and change organisations at all" - and the two must not blur.

The model is DroneOps'. Platform access is **membership of the platform
organisation**, a real organisation with a fixed id of all zeroes. That means
there is no extra column on `users` and no second permission mechanism that
could drift out of agreement with the first.

God mode is scoped to the **session's active organisation**, not to the person.
A platform admin working inside a customer's organisation is bound by row-level
security to that organisation exactly like its own members; only while their
active org is the platform org do they read across tenants. Without that
distinction, "platform admin" would mean a permanent cross-tenant read on every
request they ever make, which is precisely the property this platform exists to
avoid.
"""

import uuid

from fastapi import Depends, HTTPException

from backend.auth.dependencies import get_identity
from backend.auth.identity import Identity
from backend.database.models.enums import UserRole

#: Fixed so it is recognisable in a database row, a token and an audit entry
#: without a lookup, and so bootstrapping is idempotent.
PLATFORM_ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")
PLATFORM_ORGANIZATION_NAME = "Platform"


#: Platform roles that may WATCH every tenant: read the fleet, hear a radio, see
#: a camera. Deliberately not the same set that may change anything.
_WATCH_ROLES = frozenset({UserRole.ADMIN.value, UserRole.WATCH.value})


def require_platform_admin(
    identity: Identity = Depends(get_identity),
) -> Identity:
    """Route guard for cross-organisation administration.

    403 rather than 404. The station guard hides existence because revealing
    another tenant's hardware is a leak; there is nothing comparable to hide
    here, and a truthful error is more useful than a puzzling one.

    Membership of the platform org is no longer sufficient. It used to be, and
    that made platform membership a single undivided key: the same row that let
    somebody read the fleet also opened a root PTY on any customer's station
    (api/host.py), the station settings proxy (api/console.py), publication of a
    signed image every station will install (api/releases.py), and org and user
    CRUD. That was safe only while the platform org held nobody but the people
    who build the thing. Odin is staffed by operators on shift, and the watch
    they sit must not carry root on a customer's hardware.
    """
    if not identity.is_platform_admin or UserRole.ADMIN.value not in identity.roles:
        raise HTTPException(status_code=403, detail="Platform administrator access required")
    return identity


def require_odin_watch(
    identity: Identity = Depends(get_identity),
) -> Identity:
    """Route guard for the cross-tenant READ surface: the Odin wall.

    Admin is a superset of watch, so a platform admin keeps every view they had.
    A watch operator gets these and nothing else - see the isolation suite in
    tests/test_odin_isolation.py, which asserts both directions.
    """
    if not identity.is_platform_admin or not _WATCH_ROLES.intersection(identity.roles):
        raise HTTPException(status_code=403, detail="Platform watch access required")
    return identity
