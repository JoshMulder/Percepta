"""Password resets by emailed link.

An admin does not choose a password on somebody's behalf. They cause a link to
be sent, and the person at the other end sets their own. The difference is not
ceremony: a password an admin picked has been known to two people from the
moment it existed, usually travels over chat, and is the one nobody changes.

Shape, and why:

**Only the hash is stored.** The token lives in the recipient's mailbox and
nowhere else, so a dump of this table is not a set of live credentials. Same
treatment as a station enrolment token, for the same reason.

**Single use, and issuing one invalidates the rest.** Unlike an enrolment token
there is no technician-loses-signal case to accommodate, and a link that keeps
working is a spare key left in a mailbox. Two live links for one account is a
way for the wrong one to be used and nobody to notice.

**Redeeming revokes every session that user has.** A reset is what you do when
an account may be in someone else's hands, and leaving their existing sessions
alive defeats the entire exercise.

**Redemption does not say whether a token was wrong or expired.** The caller is
unauthenticated and the distinction is only useful to somebody guessing.
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.password import hash_password, validate_password
from backend.core.config import settings
from backend.core.crypto import lookup_hash
from backend.core.email import email_service
from backend.database.models.password_reset_token import PasswordResetToken
from backend.database.models.user import User
from backend.repositories.auth_session_repository import AuthSessionRepository

log = logging.getLogger(__name__)

#: 32 bytes from `secrets`, URL-safe. Long enough that guessing is not a threat
#: model, short enough to survive a mail client wrapping the line.
_TOKEN_BYTES = 32


class ResetError(RuntimeError):
    """The token is not usable. Deliberately says no more than that."""


def _now() -> datetime:
    return datetime.now(UTC)


def generate_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def reset_url(token: str) -> str:
    return f"{settings.console_base_url.rstrip('/')}/reset-password?token={token}"


def issue(
    db: Session,
    *,
    user: User,
    requested_by: uuid.UUID | None,
    ttl: timedelta | None = None,
) -> tuple[PasswordResetToken, str]:
    """Create a reset token, invalidating any the user already has.

    Returns the row and the plaintext. The plaintext is never stored and is the
    caller's only chance to put it in an email.
    """
    now = _now()
    outstanding = db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
    ).scalars().all()
    for row in outstanding:
        # Marked used rather than deleted: "this link was superseded" and "this
        # link was never issued" should not look the same afterwards.
        row.used_at = now

    plaintext = generate_token()
    token = PasswordResetToken(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=lookup_hash(plaintext),
        expires_at=now + (ttl or timedelta(hours=settings.password_reset_ttl_hours)),
        requested_by_user_id=requested_by,
    )
    db.add(token)
    return token, plaintext


def send(*, user: User, plaintext: str, by_admin: bool) -> None:
    """Email the link. Raises EmailNotConfiguredError if SMTP is not set up.

    Left to raise on purpose. A console that reports a reset as sent when no
    mail server exists leaves somebody waiting on an email that was never going
    to arrive, with no way to tell that from it being in their spam folder.
    """
    hours = settings.password_reset_ttl_hours
    who = (
        "An administrator has started a password reset for your Percepta account."
        if by_admin
        else "A password reset was requested for your Percepta account."
    )
    link = reset_url(plaintext)
    email_service.send(
        to=user.email,
        subject="Reset your Percepta password",
        body_text=(
            f"{who}\n\n"
            f"Open this link to choose a new password:\n\n{link}\n\n"
            f"The link works once and expires in {hours} hours. Signing in again "
            f"afterwards will need the new password on every device.\n\n"
            f"If you were not expecting this, tell whoever administers your "
            f"organisation. The link cannot be used without this email.\n"
        ),
    )


def redeem(db: Session, *, token_value: str, new_password: str) -> User:
    """Consume a token and set the password. Raises ResetError if unusable."""
    now = _now()
    token = db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == lookup_hash(token_value.strip())
        )
    ).scalar_one_or_none()

    # One message for every failure. An unauthenticated caller learns nothing
    # from the difference, and somebody guessing learns quite a lot.
    if token is None or token.used_at is not None or token.expires_at <= now:
        raise ResetError("That reset link is not valid. Ask for a new one.")

    user = db.get(User, token.user_id)
    if user is None or not user.is_active:
        raise ResetError("That reset link is not valid. Ask for a new one.")

    # Validated before the token is spent, so a password the policy rejects
    # leaves the link usable rather than burning it on a typo.
    validate_password(new_password)

    user.password_hash = hash_password(new_password)
    token.used_at = now
    # Everything that was signed in as this user stops being signed in. If the
    # reason for the reset was that somebody else had the account, this is the
    # step that actually removes them.
    AuthSessionRepository(db).revoke_all_for_user(user_id=user.id)
    return user
