from enum import StrEnum


class UserRole(StrEnum):
    """Org-wide roles, held on organization_memberships.roles.

    Kept deliberately small. In DroneOps the role set carries operational
    meaning (pilot, ground_crew, payload_operator) because roles were the whole
    permission model. Here the operational detail lives in per-station
    capabilities instead - see auth/capabilities.py - so a role only answers
    "what are you within this org", not "what may you do to that station".
    """

    ADMIN = "admin"
    #: Odin watch. Held on the PLATFORM organisation's membership only, where it
    #: means "may watch every tenant, may change nothing". It is not a role a
    #: customer org would ever hold: inside a tenant it grants nothing at all,
    #: which is the point - an operator on shift crosses tenant boundaries to
    #: read, and must not be able to reach a station's controls to do it.
    WATCH = "watch"
    """Holds every capability on every station in this org, implicitly. This is
    what DroneOps' require_admin already means, so the inherited checks carry
    across unchanged."""

    OPERATOR = "operator"
    """An ordinary member. Reaches nothing by default - every station is an
    explicit station_grants row."""

    VIEWER = "viewer"
    """As operator, but may never be granted an actuator capability. A ceiling
    on what grants can give them, not a grant in itself."""
