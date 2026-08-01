"""Small fixed-window rate limiting and failure counting, backed by Redis.

Two primitives, for two different jobs.

`check` is a plain per-call limit. It was written for the enrolment endpoint,
which is unauthenticated by necessity - the token *is* the authentication - and
so is one of the places where guessing is worth an attacker's time. A 58-bit
token already makes brute force hopeless; this makes it uninteresting, and
keeps one misconfigured box from hammering the endpoint on a retry loop.

`note_failure` / `forget` count only the attempts that *failed*, which is what
a password needs: a limit that counts successes throttles somebody working
normally, and one that never forgets punishes them for a typo an hour ago.

Fail-open, deliberately, in both. If Redis is unavailable the platform is
already in trouble, and refusing logins or enrolments would lock every operator
out of a security console over an outage in a component neither depends on.
These are nuisance-reducers stacked on top of the real controls - the token's
entropy, bcrypt, and MFA - not substitutes for them.
"""

import logging

import redis

from backend.core.config import settings

log = logging.getLogger(__name__)


def check(key: str, *, limit: int, window_seconds: int) -> bool:
    """True if this call is within the limit.

    Fixed window rather than sliding: a burst straddling a boundary can reach
    twice the limit, which does not matter for the thing this protects.
    """
    try:
        client = redis.Redis.from_url(settings.redis_url)
        full_key = f"ratelimit:{key}"
        count = client.incr(full_key)
        if count == 1:
            client.expire(full_key, window_seconds)
        return count <= limit
    except Exception:
        log.warning("Rate limit check failed for %s; allowing.", key, exc_info=True)
        return True


def failures(key: str) -> int:
    """How many failures have been recorded against this key in the window."""
    try:
        client = redis.Redis.from_url(settings.redis_url)
        return int(client.get(f"failures:{key}") or 0)
    except Exception:
        log.warning("Failure count unavailable for %s; allowing.", key, exc_info=True)
        return 0


def note_failure(key: str, *, window_seconds: int) -> int:
    """Record one failure and return the new count.

    The window is refreshed on every failure rather than only on the first, so
    a run of attempts paced to sit either side of a boundary does not reset the
    count. That is the opposite of `check`, where a fixed window is fine
    because the thing being limited is call volume rather than persistence.
    """
    try:
        client = redis.Redis.from_url(settings.redis_url)
        full_key = f"failures:{key}"
        count = client.incr(full_key)
        client.expire(full_key, window_seconds)
        return int(count)
    except Exception:
        log.warning("Could not record a failure for %s.", key, exc_info=True)
        return 0


def forget(key: str) -> None:
    """Clear a failure count, on the success that proves it was noise."""
    try:
        redis.Redis.from_url(settings.redis_url).delete(f"failures:{key}")
    except Exception:
        log.warning("Could not clear failures for %s.", key, exc_info=True)
