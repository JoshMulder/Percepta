"""Fan-out groups.

READ THIS BEFORE ADDING A FUNCTION TO THIS MODULE.

The isolation guarantee for streams (docs/03-realtime-isolation.md section 2)
is a property of this file:

    There is no primitive that sends to all connections. Every outbound frame
    goes to a group, and a connection can only be in a group it was explicitly
    authorised into.

Authorisation happens once, at subscribe time. After that, group membership
*is* the permission - so the hot path has no auth decision left to get wrong,
the same way row-level security moves the decision out of the query.

That only holds while this module offers no way to reach every connection. Do
not add `broadcast_all`, do not add "iterate the registry and send", do not add
a debug endpoint that dumps to everyone. Remote-Radio's `_broadcast_bytes`
iterates every connected client and is correct there - one station, one org, one
local consumer. The same shape here would be a cross-tenant leak, and it would
look entirely reasonable in review.

Group naming is deliberately explicit about tenancy, so a mistyped group is a
group nobody is in rather than a group everybody is in:

    org:{org_id}:status                        low-rate, org-wide status/alerts
    org:{org_id}:gsu:{station_id}:{stream}     high-rate, one pinned station
"""

import uuid
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.realtime.connection import Connection


def status_group(organization_id: uuid.UUID) -> str:
    return f"org:{organization_id}:status"


def station_group(
    organization_id: uuid.UUID, ground_station_id: uuid.UUID, stream: str
) -> str:
    return f"org:{organization_id}:gsu:{ground_station_id}:{stream}"


def station_group_pattern(
    *, organization_id: uuid.UUID | str = "*",
    ground_station_id: uuid.UUID | str = "*",
    stream: str = "*",
) -> str:
    """A glob over station groups, built by the same rule as the real name.

    For a listener that wants one stream across every station — the power
    recorder — rather than one group. Derived from `station_group` so a change
    to the naming reaches both, which is the thing that has gone wrong here
    before: a pattern written out by hand keeps matching *something* after the
    names move, and matching the wrong set is silent.

    Wildcards are glob, for Redis `PSUBSCRIBE`. Anything left unspecified is
    `*`, so the caller states only what it is pinning.
    """
    return station_group(organization_id, ground_station_id, stream)  # type: ignore[arg-type]


class GroupRegistry:
    """Group name -> the connections authorised into it.

    Deliberately tiny. The only ways out of it are `members()` for one named
    group and the per-connection `groups_of`. There is no "all connections"
    accessor, and adding one would defeat the module.
    """

    def __init__(self) -> None:
        self._members: dict[str, set["Connection"]] = defaultdict(set)
        self._by_connection: dict["Connection", set[str]] = defaultdict(set)

    def join(self, connection: "Connection", group: str) -> None:
        self._members[group].add(connection)
        self._by_connection[connection].add(group)

    def leave(self, connection: "Connection", group: str) -> None:
        self._members.get(group, set()).discard(connection)
        if not self._members.get(group):
            self._members.pop(group, None)
        self._by_connection.get(connection, set()).discard(group)

    def leave_all(self, connection: "Connection") -> None:
        for group in list(self._by_connection.get(connection, set())):
            self.leave(connection, group)
        self._by_connection.pop(connection, None)

    def leave_matching(self, connection: "Connection", prefix: str) -> list[str]:
        """Drop every group of this connection whose name starts with `prefix`.

        Used when switching station: the previous station's subscriptions must
        all go in one operation, or a connection briefly holds two stations at
        once and the one-station-at-a-time property is not actually true.
        """
        dropped = [
            g for g in list(self._by_connection.get(connection, set()))
            if g.startswith(prefix)
        ]
        for group in dropped:
            self.leave(connection, group)
        return dropped

    def members(self, group: str) -> frozenset["Connection"]:
        return frozenset(self._members.get(group, frozenset()))

    def groups_of(self, connection: "Connection") -> frozenset[str]:
        return frozenset(self._by_connection.get(connection, frozenset()))

    def group_count(self) -> int:
        """Diagnostics only - the number of groups, never their contents."""
        return len(self._members)

    def stations_subscribed_to(self, stream: str) -> set[uuid.UUID]:
        """Station ids with at least one live subscriber to `stream`, here.

        Read the warning at the top of this file before deciding whether this
        belongs. It does, and the distinction is the whole reason the warning
        is worded the way it is: this returns **identifiers, never
        connections**. There is still no way to reach a connection that did not
        join a group, and nothing here can be used to send anything anywhere.

        It exists so demand-driven streams can be *renewed* rather than
        tracked. The alternative is hooking subscribe and unsubscribe, which
        misses every way a subscriber leaves without saying so — a closed
        laptop, a dropped link, a revoked session, a browser tab shut while
        nothing was being sent. That last one is not hypothetical: it is the
        bug `api/media.watch_for_close` was written for, where the last viewer
        never left and a station streamed to nobody. Re-asserting a lease from
        who is actually here, and letting silence stop it, has no equivalent
        failure.

        Per-worker, like every other read of this registry. A subscriber on
        another worker renews from that worker, and the station takes the
        latest lease it is told about.
        """
        found: set[uuid.UUID] = set()
        suffix = f":{stream}"
        for group, members in self._members.items():
            if not group.startswith("org:") or not group.endswith(suffix):
                continue
            if not members:
                continue
            parts = group.split(":")
            # org:{org}:gsu:{station}:{stream}
            if len(parts) != 5 or parts[2] != "gsu":
                continue
            try:
                found.add(uuid.UUID(parts[3]))
            except ValueError:
                continue
        return found
