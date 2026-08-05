"""Per-station capabilities.

A capability is what a user may *do* on one ground station. It is separate from
the org-wide roles inherited from DroneOps (organization_memberships.roles):
roles say what you are within an org, capabilities say what you may do at a
specific station.

Org admins hold every capability on every station in their own org, which is
what DroneOps' existing require_admin already means. Every other user gets
capabilities only from an explicit station_grants row naming the station - there
is no org-wide wildcard grant. See docs/03-realtime-isolation.md section 3.
"""

from enum import StrEnum


class Capability(StrEnum):
    # --- Read ---------------------------------------------------------------
    STATION_VIEW = "station.view"
    """See the station exists, its status summary, and its alerts. This is the
    capability the org status channel is scoped by, so it is the one capability
    that carries across stations the user is not currently viewing."""

    TELEMETRY_VIEW = "telemetry.view"
    VIDEO_VIEW = "video.view"
    RADIO_LISTEN = "radio.listen"
    MEDIA_REVIEW = "media.review"

    # --- Actuate ------------------------------------------------------------
    # Everything below causes a physical effect at a remote, unattended site.
    VIDEO_PTZ = "video.ptz"
    RADIO_CONTROL = "radio.control"
    """Retune / squelch. Deliberately separate from RADIO_LISTEN because there is
    one dongle per station, so retuning changes what every listener on that
    station hears - which is a bigger grant than being able to listen.

    Deliberately NOT exclusive. An earlier design required a lease so only one
    operator could tune at a time; that was dropped as over-engineering for how
    the radio is actually used - a station sits on one frequency almost all the
    time, so contention is rare and the ceremony would cost more than it saves.
    Anyone holding this capability can tune, whenever they like."""

    LIGHT_CONTROL = "light.control"
    CONFIG_WRITE = "config.write"

    STATION_UPDATE = "station.update"
    """Push a signed software update to the station (system.update). Its own
    capability rather than CONFIG_WRITE because it is the most consequential grant
    on the platform: it replaces the code running at an unattended site, across
    the fleet. The station only runs images whose signature it verifies (station
    DECISIONS.md item 48), so this cannot deploy arbitrary code — but it can move
    a box to any *released* version, so it is granted sparingly and every use is
    audited. Held by org admins like every capability; grant it to no one else
    lightly."""

    RADIO_TRANSMIT = "radio.transmit"
    """Reserved. Never granted - the current hardware is receive-only and
    NullTransmitter refuses every PTT.

    BEFORE THIS IS EVER MADE GRANTABLE, read docs/05-radio-integration.md. A
    stuck PTT jams an aeronautical frequency across its whole coverage area, and
    this platform's sites are unattended and on a link that drops routinely - so
    "the operator's connection went away" is the normal case here, not the
    exceptional one. Transmit must fail *released* on link loss, be watchdogged
    in hardware, and be time-limited at the station independently of the cloud.

    It exists from day one so the permission
    check and the certification gate are built and tested before a certified
    transceiver is ever wired in; at that point the change is
    hardware and a grant, not a redesign."""


#: Capabilities that only read. A platform admin acting cross-org is limited to
#: these - see docs/03-realtime-isolation.md section 9.
READ_CAPABILITIES = frozenset({
    Capability.STATION_VIEW,
    Capability.TELEMETRY_VIEW,
    Capability.VIDEO_VIEW,
    Capability.RADIO_LISTEN,
    Capability.MEDIA_REVIEW,
})

#: Not grantable through the API under current scope. Enforced at the grant
#: boundary so it cannot be handed out by mistake before the hardware and the
#: operator licensing exist to justify it.
UNGRANTABLE_CAPABILITIES = frozenset({
    Capability.RADIO_TRANSMIT,
})

GRANTABLE_CAPABILITIES = frozenset(Capability) - UNGRANTABLE_CAPABILITIES


# ON CONTENTION, which used to be a set and two helpers nothing called.
#
# VIDEO_PTZ, RADIO_CONTROL and RADIO_TRANSMIT are the capabilities only one
# operator can usefully drive at once, and there is deliberately no lease
# mechanism: radio tuning was left unlocked on purpose, and PTZ is left the
# same way for consistency until real use shows otherwise — two operators
# fighting over a camera is visible and self-correcting in a way a silent lock
# is not.
#
# RADIO_TRANSMIT is the one that will genuinely need exclusivity, because two
# transmitters keying the same channel is not a UX problem. It is ungrantable
# today (above), so the question is deferred rather than open. Revisit when the
# certified hardware arrives — and note that `is_contended` existed for two
# years, enforced nothing, and said so in its own docstring. A set is not a
# mechanism.
