"""What becomes an alert, and — more importantly — what does not.

Alert fatigue is the failure mode that kills tools like this, and it fails
silently: a rail operators have stopped reading gives no signal that it has
stopped working. So the interesting content of this file is the refusals.

THE DOUBLE-RAISE. The station reports one physical fault twice, on adjacent
lines: it raises a health CONDITION and it records an EVENT. Battery low is
`power.battery` the condition and `power.battery` the event; the uplink dropping
is `uplink.down` both ways; the floodlight drawing nothing is `light.no_draw`
both ways. A naive engine that watched both would open two alerts for one fact,
and an operator would drive to a site twice.

Only the condition self-clears. The station raises a condition when a fault
starts and clears it when it stops, so the condition is the only one of the pair
that can ever close itself. The matching recovery EVENTS — power.recovered,
uplink.up, light.recovered — are all severity `info`, so a policy that raised on
warning-or-worse events could never see the recovery that ends them, and the
morning shift would inherit a rail full of faults that fixed themselves at 3am.

Hence three dispositions rather than two:

  CONDITION_OWNED  the condition raises and clears; the paired event is evidence
  EVENT_OWNED      genuinely occurrence-shaped, with no condition twin
  NEVER            not an alert at any severity

An unknown type at warning or critical is EVENT_OWNED, so the policy degrades
gracefully against a vocabulary the STATION owns and can extend without asking
the platform first. An unknown type at info is NEVER: a station cannot make the
rail shout by inventing a word.
"""

from __future__ import annotations

from enum import Enum


class Disposition(Enum):
    #: The station's own health condition is the authority. The event of the
    #: same name bumps occurrences and never opens anything.
    CONDITION_OWNED = "condition"
    #: No condition twin. The event itself is the alert.
    EVENT_OWNED = "event"
    #: Never an alert, whatever severity it carries.
    NEVER = "never"


#: Facts the station reports as BOTH a condition and an event. Verified against
#: the agent: power.battery (agent.py:1916/1921), light.no_draw and
#: light.stuck_on (:1991-2003), uplink.down (:2071/2076).
#:
#: Listed by the condition id, which is also the event type — that they are the
#: same string is what made the double-raise so easy to write by accident.
CONDITION_OWNED = frozenset({
    "power.battery",
    "light.no_draw",
    "light.stuck_on",
    "uplink.down",
    "clock.implausible",
    "credential.renewal_failing",
    "credential.revoked",
    "credential.expiring",
    "enrolment.unclaimed",
    "enrolment.rejected",
    "tls.verify_disabled",
    "tls.handshake_failing",
})

#: Never an alert, at any severity, from any source.
#:
#: adsb.proximity is emitted at WARNING on every close-and-low contact, and on
#: the live fleet that was 46 of 71 warnings in a day. It is a real warning to
#: the tenant watching their own airspace and it is not a fault in anybody's
#: fleet; one busy circuit would otherwise fill the rail with aeroplanes doing
#: exactly what aeroplanes do.
#:
#: radio.transmission is a transcript line. There were 291 in 24 hours.
NEVER = frozenset({
    "adsb.proximity",
    "radio.transmission",
    # Recoveries. They CLOSE alerts (see close_key_for) and must never open one.
    "power.recovered",
    "uplink.up",
    "light.recovered",
})

#: Recovery events, and the fact each one ends. The station says a fault is over
#: with an info-severity event, so these are read for their closing meaning even
#: though they can never raise.
RECOVERS: dict[str, str] = {
    "power.recovered": "power.battery",
    "uplink.up": "uplink.down",
    "light.recovered": "light.no_draw",
}

#: Severities that can open an alert from an EVENT. Info events are recorded in
#: the station's timeline and are not, by themselves, anything to act on.
RAISING_SEVERITIES = frozenset({"warning", "critical"})


def disposition(event_type: str, severity: str) -> Disposition:
    """How this event type should be treated."""
    if event_type in NEVER:
        return Disposition.NEVER
    if event_type in CONDITION_OWNED:
        return Disposition.CONDITION_OWNED
    if severity in RAISING_SEVERITIES:
        # Unknown, and serious enough to say so. The station owns this
        # vocabulary and may extend it without a platform release; refusing
        # everything unrecognised would mean a new fault type is silently
        # invisible to the command centre until somebody edits this file.
        return Disposition.EVENT_OWNED
    return Disposition.NEVER


def closes(event_type: str) -> str | None:
    """The condition this event reports the end of, if any."""
    return RECOVERS.get(event_type)
