"""Where a station's channel names come from. One definition, five consumers.

`contract/transport.md` names three channels per station:

    gsu/{station_id}/telemetry     station -> platform
    gsu/{station_id}/audio         station -> platform
    cmd/gsu/{station_id}           platform -> station

They were built independently in five production places — what the station is
*told* at enrolment, what the Redis ACL *grants*, what the relay *accepts*,
what the ingest *subscribes to*, and `bus.command_channel` — plus the
simulator, the conformance checker and two verification scripts.

**They agreed. That is not the same as being safe.** Retiring the video channel
in `23e705a` meant editing eight places to remove one name, and it still left
`video_topic` behind in the enrolment response, where it sat as a required
field with no supplier until it was found by audit. That is what a fact stated
nine times costs.

The asymmetry is what makes it worth a module rather than a comment. If the
ACL and the enrolment response ever disagree, the station is *told* a topic it
is not *granted*: the publish fails with `NoPermissionError`, the station
reports `topic_refused` and carries on, and what an operator sees is a box
that enrolled perfectly and publishes nothing. Nothing crashes and nothing is
logged as an error at either end. That is `CONTRACT-QUESTIONS` item 17, and it
is the failure this module exists to make impossible.

Derived, never chosen — the same rule as `principal` in `broker_acl`, which is
the model this follows.
"""

from __future__ import annotations

import uuid

#: A station id, in either of the forms callers hold one in.
StationId = uuid.UUID | str


def telemetry(station_id: StationId) -> str:
    return f"gsu/{station_id}/telemetry"


def audio(station_id: StationId) -> str:
    return f"gsu/{station_id}/audio"


def command(station_id: StationId) -> str:
    """Platform -> station.

    Slash-separated to match `contract/transport.md`, and because the relay
    carries it as a topic path rather than a Redis key. Redis does not care;
    the contract does.
    """
    return f"cmd/gsu/{station_id}"


def events(station_id: StationId) -> str:
    """Station -> platform, and the only channel here that is a ledger.

    Everything else on this list is current state and may be dropped. An event
    has no newer version, so this one is acknowledged and the station keeps
    re-sending until it is.
    """
    return f"gsu/{station_id}/events"


#: Relay stream code -> the internal channel it lands on. **This is the whole
#: translation between the wire and the platform's fan-out**, and it exists in
#: one place for the reason the rest of this module does.
#:
#: Contract 2.0 took the names off the wire: a station sends a one-letter code
#: and cannot name a channel, a tenant or itself. So the asymmetry this module
#: was written to prevent — the station being *told* a name it was not
#: *granted* — is now structurally impossible on the station's side. What
#: remains is the platform's own two-sided mistake: a channel published here
#: and not subscribed to by the ingest, which is still silent at both ends.
_STREAMS = {
    "t": telemetry,
    "a": audio,
    "e": events,
}


def channel_for_stream(stream: str, station_id: StationId) -> str | None:
    """Where a stream code's payload goes, or None if a station may not send it.

    None is a refusal, not an error: the station is told on the socket and the
    socket stays up. A station silently dropping everything it publishes looks
    exactly like a station with nothing to say, and this is the fault most
    likely to be a misconfiguration rather than an attack.
    """
    builder = _STREAMS.get(stream)
    return builder(station_id) if builder else None


def published_by_station(station_id: StationId) -> frozenset[str]:
    """Everything a station may publish to, and nothing else.

    No longer what `/broker` compares against — under 2.0 the relay maps a
    stream code through `channel_for_stream` and there is no name to compare —
    but still what the Redis ACL grants, and still the answer to "what can this
    station reach".
    """
    return frozenset({telemetry(station_id), audio(station_id),
                      events(station_id)})


def granted_to_station(station_id: StationId) -> tuple[str, ...]:
    """All three, in the `&channel` form a Redis ACL wants.

    Includes the command channel because Redis channel patterns do not
    distinguish publish from subscribe, so granting a station the ability to
    *receive* its commands also lets it publish onto that channel. It can
    issue commands to itself and to nothing else, which is not worth a second
    mechanism to prevent. Directional ACLs should be written that way when the
    transport moves.
    """
    return tuple(
        f"&{name}" for name in (
            telemetry(station_id), audio(station_id), events(station_id),
            command(station_id),
        )
    )


def subscribed_by_platform() -> tuple[str, ...]:
    """The glob patterns the ingest listens on, across every station.

    Built from the same functions with `*` for the id, so a change to the
    naming reaches the listener as well as the publishers. A pattern written
    out by hand keeps matching *something* after names move, and matching the
    wrong set is silent — no error, no data.
    """
    return (telemetry("*"), audio("*"), events("*"))
