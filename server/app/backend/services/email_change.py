"""Email changes, confirmed by a link sent to the new address.

Changing the address you sign in with is gated two ways: you prove you hold the
account (the endpoint requires your current password) and you prove you control
the new address (this link is sent there and must be opened). Only when both
hold does the address move.

Shape mirrors `password_reset`, and for the same reasons:

**Only the hash is stored.** The token lives in the new mailbox and nowhere
else, so a dump of this table is not a set of live links.

**Single use, and issuing one supersedes the rest.** A typo'd address must not
linger as a second live link, and two outstanding requests are a way for the
wrong one to land and nobody to notice.

**Redemption says no more than "not valid".** The redeemer is unauthenticated
and the distinction between wrong, expired and superseded only helps someone
guessing.

**The uniqueness of the address is re-checked at redemption**, not trusted from
when the link was sent: someone else may have taken it in between.
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import settings
from backend.core.crypto import lookup_hash
from backend.core.email import email_service
from backend.database.models.email_change_token import EmailChangeToken
from backend.database.models.user import User

log = logging.getLogger(__name__)

#: 32 bytes from `secrets`, URL-safe — long enough that guessing is not a threat
#: model, short enough to survive a mail client wrapping the line.
_TOKEN_BYTES = 32


class EmailChangeError(RuntimeError):
    """The token is not usable, or the address is no longer free. Deliberately
    says no more than that to an unauthenticated caller."""


def _now() -> datetime:
    return datetime.now(UTC)


def _ttl() -> timedelta:
    # Reuses the reset link's lifetime: same order of magnitude, same reasoning
    # (long enough to reach a mailbox and act, short enough that a link left in
    # one does not stay live for days).
    return timedelta(hours=settings.password_reset_ttl_hours)


def verify_url(token: str) -> str:
    """The link that goes in the email.

    Token in the fragment, never the query string, so it does not reach the
    reverse proxy's access log — the same reasoning as the reset link. The
    console reads it from `window.location.hash` and clears it.
    """
    base = settings.console_base_url.rstrip("/")
    return f"{base}/verify-email#token={token}"


def normalise(email: str) -> str:
    """The stored/compared form of an address: stripped and lower-cased. Applied
    consistently so a change to `Me@x` matched against a stored `me@x` does not
    read as a different address."""
    return email.strip().lower()


def issue(db: Session, *, user: User, new_email: str) -> tuple[EmailChangeToken, str]:
    """Create a verification token for `new_email`, superseding any request this
    user already has outstanding. Returns the row and the plaintext — never
    stored, and the caller's only chance to put it in an email."""
    now = _now()
    outstanding = db.execute(
        select(EmailChangeToken).where(
            EmailChangeToken.user_id == user.id,
            EmailChangeToken.used_at.is_(None),
            EmailChangeToken.expires_at > now,
        )
    ).scalars().all()
    for row in outstanding:
        # Marked used rather than deleted: "superseded" and "never issued" should
        # not look the same when reading back what happened to an account.
        row.used_at = now

    plaintext = secrets.token_urlsafe(_TOKEN_BYTES)
    token = EmailChangeToken(
        id=uuid.uuid4(),
        user_id=user.id,
        new_email=normalise(new_email),
        token_hash=lookup_hash(plaintext),
        expires_at=now + _ttl(),
    )
    db.add(token)
    return token, plaintext


def send(*, new_email: str, plaintext: str) -> None:
    """Email the verification link to the NEW address. Raises
    EmailNotConfiguredError if SMTP is not set up — left to raise on purpose, so
    a console never reports a change as pending when the mail could not be sent.
    """
    hours = settings.password_reset_ttl_hours
    link = verify_url(plaintext)
    email_service.send(
        to=new_email,
        subject="Confirm your new Percepta email address",
        body_text=(
            "A request was made to change the email address on a Percepta "
            "account to this one.\n\n"
            f"Open this link to confirm the change:\n\n{link}\n\n"
            f"The link works once and expires in {hours} hours. Until it is "
            "opened, the account keeps its current address and sign-in.\n\n"
            "If you were not expecting this, you can ignore this email — nothing "
            "changes unless the link is opened.\n"
        ),
    )


class Redeemed(NamedTuple):
    user: User
    old_email: str
    new_email: str


def redeem(db: Session, *, token_value: str) -> Redeemed:
    """Consume a token and move the account to its new address. Raises
    EmailChangeError if the token is unusable or the address was taken in the
    meantime."""
    now = _now()
    token = db.execute(
        select(EmailChangeToken).where(
            EmailChangeToken.token_hash == lookup_hash(token_value.strip())
        )
    ).scalar_one_or_none()

    # One message for every failure — an unauthenticated caller learns nothing
    # useful from the difference.
    if token is None or token.used_at is not None or token.expires_at <= now:
        raise EmailChangeError(
            "That verification link is not valid. Ask for a new one."
        )

    user = db.get(User, token.user_id)
    if user is None or not user.is_active:
        raise EmailChangeError(
            "That verification link is not valid. Ask for a new one."
        )

    # The address may have been claimed between request and redemption. Checked
    # here, at the moment it actually matters, rather than trusting the check
    # made when the link was sent. The unique constraint on users.email is the
    # backstop; this turns the race into a clear message rather than a 500.
    clash = db.execute(
        select(User).where(
            User.email == token.new_email, User.id != user.id
        )
    ).scalar_one_or_none()
    if clash is not None:
        # Spend the token: this request can never succeed now, and a live link to
        # an address that is taken is just a confusing retry waiting to happen.
        token.used_at = now
        raise EmailChangeError("That address is already in use by another account.")

    old_email = user.email
    user.email = token.new_email
    token.used_at = now
    return Redeemed(user=user, old_email=old_email, new_email=token.new_email)
