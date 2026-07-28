"""Give each station a broker principal that can only reach its own channels.

Enrolment decides *who* a box is. This is what makes that identity mean
something at the transport: a station authenticates to the broker as itself and
is pinned to `gsu/{id}/…` and `cmd/gsu/{id}`, so a compromised unit cannot
publish as another org's hardware or listen to commands meant for it.

Redis today, MQTT in production. The shape is deliberately the same in both -
one principal per station, channel patterns derived from the station id - so
this becomes a different client library rather than a different design.

The broker requires authentication. `REDIS_PASSWORD` sets `requirepass`, so the
`default` user is no longer open and an unauthenticated client is refused
outright - which is what makes the per-station pinning below an actual boundary
rather than a description of one.

**Principals are rebuilt from the database, never from a remembered plaintext.**
Redis accepts an already-SHA-256-hashed password as `#<hex>`, and
`station_credentials.secret_hash` is exactly that hash. So the platform can
reconstruct the exact set of passwords a station should accept without ever
holding one, which buys three things at once: ACL users survive a Redis restart
because `sync_all` rebuilds them at start-up, revocation is precise because the
set is recomputed from what is currently valid, and the platform still cannot
hand out a station's credential.

Everything in this module is fail-soft. A broker that is unreachable must not
block an enrolment: the technician is on site, the credential is already issued
and audited, and `station_credentials.broker_provisioned` records which rows
still need the broker told about them.
"""

import logging
import uuid

import redis

from backend.core.config import settings

log = logging.getLogger(__name__)

#: Commands a station needs and nothing more. No keyspace access at all - a
#: station has no business reading or writing Redis keys, only pub/sub.
_ALLOWED_COMMANDS = [
    "-@all",
    "+publish",
    "+subscribe",
    "+psubscribe",
    "+unsubscribe",
    "+punsubscribe",
    "+ping",
    "+auth",
    "+hello",
    "+quit",
    "+client|setname",
]


def principal(station_id: uuid.UUID | str) -> str:
    """The broker username for a station. Derived from the id, never chosen."""
    return f"gsu:{station_id}"


def _channels(station_id: uuid.UUID | str) -> list[str]:
    """Exactly the channels this contract gives the station.

    Redis channel patterns do not distinguish publish from subscribe, so this
    also lets a station publish onto its own command channel - it can issue
    commands to itself and to nothing else, which is not worth a second
    mechanism to prevent. MQTT ACLs are directional and should be written that
    way when the transport moves.
    """
    return [
        f"&gsu/{station_id}/telemetry",
        f"&gsu/{station_id}/audio",
        f"&cmd/gsu/{station_id}",
    ]


def _client() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url)


def provision_hashes(station_id: uuid.UUID | str, hashes: list[str]) -> bool:
    """Make the station's principal accept exactly these SHA-256 password
    hashes, and no others.

    `resetpass` first, so this is a replacement rather than an addition. It used
    to add, which meant a revoked credential kept authenticating forever: the
    platform stopped trusting it and the broker did not.
    """
    user = principal(station_id)
    if not hashes:
        # No valid credential means no principal, not a principal with no way
        # in - an account that exists and accepts nothing is a puzzle later.
        return deprovision(station_id)
    try:
        _client().execute_command(
            "ACL", "SETUSER", user,
            "on",
            "resetkeys",
            "resetchannels",
            "resetpass",
            *_channels(station_id),
            *_ALLOWED_COMMANDS,
            # `#<hex>` is an already-hashed password. The platform never holds
            # the plaintext, and does not need to.
            *(f"#{h}" for h in hashes),
        )
        return True
    except Exception:
        log.exception(
            "Could not provision broker principal %s. Its credential is valid; "
            "the broker does not know about it yet.", user,
        )
        return False


def sync_station(db, station_id: uuid.UUID) -> bool:
    """Rebuild one station's principal from what the database currently
    considers valid. Idempotent, and the only correct way to apply a change -
    issue, renewal, revocation and expiry all reduce to "recompute the set"."""
    from backend.services.enrolment import valid_credential_hashes

    return provision_hashes(station_id, valid_credential_hashes(db, station_id=station_id))


def sync_all(db) -> int:
    """Rebuild every station's principal. Run at start-up, because Redis holds
    ACL users in memory and a broker restart would otherwise silently lock out
    every station until each happened to re-enrol. Returns how many were
    provisioned."""
    from sqlalchemy import select

    from backend.database.models.ground_station import GroundStation

    done = 0
    stations = db.execute(
        select(GroundStation.id).where(GroundStation.is_active.is_(True))
    ).scalars().all()
    for station_id in stations:
        if sync_station(db, station_id):
            done += 1
    return done


def deprovision(station_id: uuid.UUID | str) -> bool:
    """Delete the principal outright. Used on revocation and decommissioning.

    Also kills any connection currently authenticated as it. This docstring used
    to assert that Redis closes those connections by itself; the station team
    tested it and it does not - an ACL change binds at authentication time, so a
    station that never reconnects keeps publishing on a withdrawn password
    indefinitely. Revocation that waits for the other side to reconnect is not
    revocation, so the kill is explicit.
    """
    user = principal(station_id)
    ok = True
    try:
        client = _client()
        client.execute_command("ACL", "DELUSER", user)
    except Exception:
        log.exception("Could not remove broker principal %s.", user)
        ok = False

    try:
        # Separate call, and failure here is not failure of the revocation: the
        # principal is already gone, so the worst case is one live connection
        # lasting until it next reconnects.
        _client().execute_command("CLIENT", "KILL", "USER", user)
    except Exception:
        # Redis raises when no client matches, which is the common case.
        log.debug("No live connections to kill for %s.", user, exc_info=True)
    return ok


def exists(station_id: uuid.UUID | str) -> bool:
    try:
        return _client().execute_command(
            "ACL", "GETUSER", principal(station_id)
        ) is not None
    except Exception:
        return False
