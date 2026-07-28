"""Give each station a broker principal that can only reach its own channels.

Enrolment decides *who* a box is. This is what makes that identity mean
something at the transport: a station authenticates to the broker as itself and
is pinned to `gsu/{id}/…` and `cmd/gsu/{id}`, so a compromised unit cannot
publish as another org's hardware or listen to commands meant for it.

Redis today, MQTT in production. The shape is deliberately the same in both -
one principal per station, channel patterns derived from the station id - so
this becomes a different client library rather than a different design.

READ THIS BEFORE BELIEVING THE STATIONS ARE ISOLATED. Creating a restricted user
does not restrict anyone who does not use it. Redis' `default` user is still
open on the development stack, and the platform's own connections use it, so a
process that simply does not authenticate has the run of the broker. Provisioning
here is real and correct, and it enforces nothing until `default` is locked down
(`requirepass`, or `ACL SETUSER default off`) and the platform's own components
are given credentials. That is a deployment change, tracked in
`docs/04-production-readiness.md`, not a code change here.

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


def provision(station_id: uuid.UUID | str, secret: str) -> bool:
    """Create or update the station's principal, adding this secret.

    Adds rather than replaces: a renewed credential must work before the old
    one stops, so for the overlap window the principal accepts both.
    """
    user = principal(station_id)
    try:
        client = _client()
        client.execute_command(
            "ACL", "SETUSER", user,
            "on",
            "resetkeys",
            "resetchannels",
            *_channels(station_id),
            *_ALLOWED_COMMANDS,
            f">{secret}",
        )
        return True
    except Exception:
        log.exception(
            "Could not provision broker principal %s. The credential is issued "
            "and valid; the broker does not know about it yet.", user,
        )
        return False


def drop_secret(station_id: uuid.UUID | str, secret: str) -> bool:
    """Remove one secret from a principal, leaving any others working."""
    try:
        _client().execute_command(
            "ACL", "SETUSER", principal(station_id), f"<{secret}"
        )
        return True
    except Exception:
        log.exception(
            "Could not remove a secret from broker principal %s.",
            principal(station_id),
        )
        return False


def deprovision(station_id: uuid.UUID | str) -> bool:
    """Delete the principal outright. Used on revocation and decommissioning.

    Redis closes the connections of a deleted user, so a station that is
    currently connected is cut off rather than left running until it happens to
    reconnect. That is the behaviour revocation is supposed to have.
    """
    user = principal(station_id)
    try:
        _client().execute_command("ACL", "DELUSER", user)
        return True
    except Exception:
        log.exception("Could not remove broker principal %s.", user)
        return False


def exists(station_id: uuid.UUID | str) -> bool:
    try:
        return _client().execute_command(
            "ACL", "GETUSER", principal(station_id)
        ) is not None
    except Exception:
        return False
