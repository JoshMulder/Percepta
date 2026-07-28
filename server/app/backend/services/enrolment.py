"""How a box becomes a station the platform will accept data from.

Implements `contract/enrolment.md`. The lifecycle is: an admin creates the
station record and issues a token, a technician types that token into the box,
the box claims it and receives a credential, and from then on it authenticates
with that credential and renews it before it expires.

Three properties are load-bearing, and each one is a decision rather than an
implementation detail.

**The organisation comes from the record, never from the box.** A token is bound
to one station id at issue time. Nothing the claiming box sends can change which
tenant it lands in - the worst a leaked token achieves is enrolling the wrong
hardware into a station an admin had already decided to create.

**Secrets are stored hashed and are unrecoverable.** The plaintext credential
exists once, in the response to the claim. We cannot reissue the same value, and
that is the point: a platform able to hand out a station's credential is one
whose operator can impersonate a customer's station.

**Failures are indistinguishable.** Unknown, expired and revoked tokens all
return the same thing. Which one it was is free information about the token
space to an attacker, and makes no difference at all to the technician holding
the code.

One deliberate deviation from the contract, and the reasoning. §4 says a retry
from the same station "returns the same credential". We cannot - see above - so
a retry inside the token's lifetime issues a *fresh* credential and revokes the
previous one. It satisfies what that clause is actually for, which is that a
technician who loses signal mid-enrolment must be able to finish without an
admin issuing anything new, and it avoids storing the secret recoverably. The
cost is that an accidental re-claim cuts off a box that had already succeeded;
that requires someone to physically re-enter the code, and the recovery is to
enter a new one.
"""

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.crypto import lookup_hash
from backend.database.models.ground_station import GroundStation
from backend.database.models.station_credential import StationCredential
from backend.database.models.station_enrolment_token import StationEnrolmentToken

log = logging.getLogger(__name__)

#: Token alphabet. Crockford-style: no I, L, O, U, 0 or 1, because this gets
#: read aloud down a phone line to someone on a hillside and then typed. Every
#: removed character is a support call that does not happen.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"

#: 12 characters from a 30-character alphabet is about 58 bits. Grouped
#: XXXX-XXXX-XXXX so it can be read in chunks. Well beyond guessing, especially
#: against a rate limit and a 24-hour life.
_TOKEN_CHARS = 12
_GROUP = 4

#: How long an enrolment token lives. Suits install-on-the-day; a box shipped
#: ahead of its installation needs a longer one, which is an open decision in
#: the contract (§9) rather than something to quietly widen here.
TOKEN_TTL = timedelta(hours=24)

#: Credential lifetime. Long enough that renewal is routine rather than
#: constant, short enough that a credential leaked from a decommissioned box
#: does not stay useful for years.
CREDENTIAL_TTL = timedelta(days=90)

#: How long a superseded credential keeps working after renewal. A station that
#: renews and then loses power mid-swap must not be locked out of a site that is
#: hours away, so the old secret stays valid for a day.
RENEWAL_OVERLAP = timedelta(hours=24)

#: The station should renew at half life. Reported so the box does not have to
#: hardcode a policy the platform owns.
RENEW_AFTER = CREDENTIAL_TTL / 2


class EnrolmentError(Exception):
    """Base for enrolment failures that map to a specific HTTP response."""


class InvalidToken(EnrolmentError):
    """Unknown, expired, revoked - deliberately not distinguished."""


class AlreadyEnrolled(EnrolmentError):
    """The station has a credential from a different token."""


@dataclass(frozen=True)
class IssuedCredential:
    """The one moment the plaintext secret exists outside the box."""

    station: GroundStation
    credential: StationCredential
    secret: str


def generate_token() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(_TOKEN_CHARS))
    return "-".join(
        raw[i : i + _GROUP] for i in range(0, _TOKEN_CHARS, _GROUP)
    )


def normalise_token(value: str) -> str:
    """Accept what a human actually types.

    Lowercase, missing dashes, stray spaces - all fine. This runs before
    hashing, so the stored hash is of the canonical form and a token typed three
    different ways still matches.
    """
    return "".join(c for c in value.upper() if c.isalnum())


def _now() -> datetime:
    return datetime.now(UTC)


def _generate_secret() -> str:
    # 256 bits, URL-safe. Goes in a broker password field, so it must survive
    # being copied through configuration without escaping.
    return secrets.token_urlsafe(32)


# --- issuing -------------------------------------------------------------


def issue_token(
    db: Session,
    *,
    station: GroundStation,
    issued_by_user_id: uuid.UUID | None,
    ttl: timedelta = TOKEN_TTL,
) -> tuple[StationEnrolmentToken, str]:
    """Create an enrolment token for a station. Returns the row and the
    plaintext, which the caller must show once and never store.

    Any previously issued unused token for this station is revoked. Two live
    codes for one station is a way to enrol the wrong box and not find out.
    """
    now = _now()
    existing = db.execute(
        select(StationEnrolmentToken).where(
            StationEnrolmentToken.ground_station_id == station.id,
            StationEnrolmentToken.revoked_at.is_(None),
            StationEnrolmentToken.expires_at > now,
        )
    ).scalars().all()
    for row in existing:
        row.revoked_at = now

    plaintext = generate_token()
    token = StationEnrolmentToken(
        organization_id=station.organization_id,
        ground_station_id=station.id,
        token_hash=lookup_hash(normalise_token(plaintext)),
        expires_at=now + ttl,
        issued_by_user_id=issued_by_user_id,
    )
    db.add(token)
    db.flush()
    return token, plaintext


def revoke_tokens(db: Session, *, station_id: uuid.UUID) -> int:
    """Stop every outstanding code for a station working. Returns how many."""
    now = _now()
    rows = db.execute(
        select(StationEnrolmentToken).where(
            StationEnrolmentToken.ground_station_id == station_id,
            StationEnrolmentToken.revoked_at.is_(None),
            StationEnrolmentToken.expires_at > now,
        )
    ).scalars().all()
    for row in rows:
        row.revoked_at = now
    return len(rows)


# --- claiming ------------------------------------------------------------


def claim(
    db: Session, *, token_value: str, hardware: dict | None
) -> IssuedCredential:
    """Exchange a token for a credential.

    Runs on the privileged session: this is the step that *establishes* which
    org the box belongs to, so there is no org context to scope it with yet -
    the same reason the ingest reads the registry privileged.
    """
    now = _now()
    token = db.execute(
        select(StationEnrolmentToken).where(
            StationEnrolmentToken.token_hash
            == lookup_hash(normalise_token(token_value))
        )
    ).scalar_one_or_none()

    if token is None or token.revoked_at is not None or token.expires_at <= now:
        raise InvalidToken()

    station = db.get(GroundStation, token.ground_station_id)
    if station is None or not station.is_active:
        # The record was deleted or decommissioned after the code was issued.
        raise InvalidToken()

    # Already enrolled by some other token: this code is for a station that is
    # already in service, and re-enrolling it would cut off working hardware.
    if station.enrolled_at is not None and token.claimed_at is None:
        raise AlreadyEnrolled()

    # A retry supersedes whatever the earlier attempt produced - the box that
    # received it either never got the response or is being replaced by this
    # claim. See the module docstring.
    revoke_credentials(
        db, station_id=station.id, reason="superseded-by-enrolment"
    )

    secret = _generate_secret()
    credential = StationCredential(
        organization_id=station.organization_id,
        ground_station_id=station.id,
        kind="bearer",
        secret_hash=lookup_hash(secret),
        expires_at=now + CREDENTIAL_TTL,
    )
    db.add(credential)

    token.claimed_at = token.claimed_at or now
    token.claim_count += 1
    station.enrolled_at = station.enrolled_at or now
    if hardware:
        station.hardware = hardware
    db.flush()

    return IssuedCredential(station=station, credential=credential, secret=secret)


# --- authenticating ------------------------------------------------------


def authenticate(
    db: Session, *, secret: str
) -> tuple[GroundStation, StationCredential] | None:
    """Resolve a presented secret to a station, or None.

    The station id is *derived* from the credential rather than sent alongside
    it. A box that presents a valid secret cannot claim to be a different
    station, because it never says which station it is.
    """
    credential = db.execute(
        select(StationCredential).where(
            StationCredential.secret_hash == lookup_hash(secret)
        )
    ).scalar_one_or_none()
    if credential is None or not is_valid(credential):
        return None

    station = db.get(GroundStation, credential.ground_station_id)
    if station is None or not station.is_active:
        return None

    credential.last_used_at = _now()
    return station, credential


def is_valid(credential: StationCredential, *, at: datetime | None = None) -> bool:
    now = at or _now()
    if credential.revoked_at is not None:
        return False
    if credential.expires_at <= now:
        return False
    if credential.superseded_at is not None and credential.superseded_at <= now:
        return False
    return True


def renew(
    db: Session, *, station: GroundStation, current: StationCredential
) -> IssuedCredential:
    """Issue a fresh credential, leaving the current one valid for the overlap.

    Not revoking the old one immediately is the whole point: the response
    carrying the new secret may never arrive, and a station that has lost its
    only credential at a remote site is a truck roll.
    """
    now = _now()
    secret = _generate_secret()
    replacement = StationCredential(
        organization_id=station.organization_id,
        ground_station_id=station.id,
        kind=current.kind,
        secret_hash=lookup_hash(secret),
        expires_at=now + CREDENTIAL_TTL,
    )
    db.add(replacement)

    overlap_until = now + RENEWAL_OVERLAP
    # Never extend a credential past its own expiry in the name of overlap.
    current.superseded_at = min(overlap_until, current.expires_at)
    db.flush()

    return IssuedCredential(station=station, credential=replacement, secret=secret)


def revoke_credentials(
    db: Session, *, station_id: uuid.UUID, reason: str
) -> list[StationCredential]:
    """Revoke every live credential for a station, immediately and with no
    overlap. Returns the rows revoked, so the caller can tear down whatever the
    broker was told about them."""
    now = _now()
    rows = db.execute(
        select(StationCredential).where(
            StationCredential.ground_station_id == station_id,
            StationCredential.revoked_at.is_(None),
        )
    ).scalars().all()
    live = [row for row in rows if is_valid(row, at=now)]
    for row in rows:
        row.revoked_at = now
        row.revoked_reason = reason
    return live


def has_valid_credential(db: Session, *, station_id: uuid.UUID) -> bool:
    """Whether any credential for this station currently works."""
    rows = db.execute(
        select(StationCredential).where(
            StationCredential.ground_station_id == station_id,
            StationCredential.revoked_at.is_(None),
        )
    ).scalars().all()
    return any(is_valid(row) for row in rows)
