"""A small fixed-window rate limiter, backed by Redis.

Exists for the enrolment endpoint, which is unauthenticated by necessity - the
token *is* the authentication - and so is the one place where guessing is worth
an attacker's time. A 58-bit token already makes brute force hopeless; this
makes it uninteresting, and keeps one misconfigured box from hammering the
endpoint on a retry loop.

Fail-open, deliberately. If Redis is unavailable the platform is already in
trouble, and refusing enrolments would strand a technician on site over an
outage in a component that has nothing to do with them. The limit is a
nuisance-reducer, not the security control - that is the token's entropy.
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
