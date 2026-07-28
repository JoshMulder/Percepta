"""Time, and the specific way a wrong clock strands a remote site.

`contract/enrolment.md` §6: a station with a wrong clock cannot authenticate,
and if it believes its credential has already expired it cannot renew either.
That is a site visit, hours away, for a bad number.

Two rules follow, and both are implemented here rather than assumed:

1. **Refuse to enrol with an implausible clock, and say so.** A box with no
   battery-backed clock boots in 1970 or at its filesystem's build date. Binding
   a credential to a lifetime measured from that is how a station enrols
   successfully and is dead by morning.
2. **The platform is never the only clock authority.** `/api/enrol/status`
   returns `server_time` and it is a *reference*: worth comparing against, worth
   alarming on, never worth silently adopting. A station that took its time from
   the platform would have no idea it was wrong the moment the platform was
   unreachable, which is exactly when it matters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

#: No credible station clock reads earlier than the release it is running. A
#: value before this means the clock has reset, not that time has moved.
NOT_BEFORE = datetime(2026, 1, 1, tzinfo=UTC)

#: And none reads a decade ahead. Both bounds are wide on purpose: this test
#: exists to catch a reset clock, not to police drift.
MAX_AHEAD = timedelta(days=3652)

#: Skew beyond this against the platform's reference clock is reported as a
#: health condition. Well inside any credential lifetime, so it is a warning
#: long before it is an outage.
SKEW_WARN = timedelta(minutes=5)


def now() -> datetime:
    return datetime.now(UTC)


def implausible_reason(at: datetime | None = None) -> str | None:
    """Why this clock cannot be trusted, or None if it can."""
    at = at or now()
    if at < NOT_BEFORE:
        return (
            f"the clock reads {at.isoformat()}, before this software existed. "
            "Time has not synchronised yet."
        )
    if at > NOT_BEFORE + MAX_AHEAD:
        return f"the clock reads {at.isoformat()}, implausibly far ahead."
    return None


def is_plausible(at: datetime | None = None) -> bool:
    return implausible_reason(at) is None


def parse(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from the platform, tolerating `Z` and a
    missing timezone. A naive timestamp is treated as UTC — the platform sends
    UTC, and guessing local here would be worse than assuming."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def skew(server_time: str | datetime | None, at: datetime | None = None) -> timedelta | None:
    """How far this clock is from the platform's, positive if we are ahead."""
    reference = parse(server_time) if isinstance(server_time, str) else server_time
    if reference is None:
        return None
    return (at or now()) - reference


class ClockImplausible(RuntimeError):
    """Raised rather than enrolling. The technician needs to see this."""
