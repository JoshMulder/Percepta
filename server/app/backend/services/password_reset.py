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
from typing import NamedTuple

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


def reset_url(token: str, *, invite: bool = False) -> str:
    """The link that goes in the email.

    **The token is in the fragment, not the query string.** A fragment is never
    sent to the server, so it does not reach the reverse proxy's access log —
    and the console is served by this same application with an `html=True`
    fallback, so `/reset-password?token=…` was a real HTTP request and a real
    log line on every open. Single use and twelve hours bounded the damage, but
    the set of people who can read a proxy log is much wider than the set who
    can read the recipient's mailbox.

    The console reads it with `window.location.hash` and clears it. `invite`
    stays a query parameter deliberately: it only changes the wording on the
    page, and there is no reason to hide it.
    """
    base = settings.console_base_url.rstrip("/")
    query = "?invite=1" if invite else ""
    return f"{base}/reset-password{query}#token={token}"


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


class Redeemed(NamedTuple):
    """The outcome of a redemption, including what the caller still has to do.

    `revoked_sessions` is returned rather than acted on here because the push
    that closes a live socket is not transactional and this function is: an
    announcement sent before the commit is one a rollback cannot take back.
    Named in the return type so it is hard to walk past — the reset path went
    without it for exactly as long as it was invisible.
    """

    user: User
    revoked_sessions: list[uuid.UUID]


def redeem(db: Session, *, token_value: str, new_password: str) -> Redeemed:
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
    revoked = AuthSessionRepository(db).revoke_all_for_user(user_id=user.id)
    return Redeemed(user=user, revoked_sessions=revoked)


def send_invitation(
    *, user: User, plaintext: str, organization_name: str, inviter: str
) -> None:
    """Email a new member the link that sets their first password.

    The same machinery as a reset, and for the same reason: the person who will
    use the account is the only one who should ever know its password. An
    invitation that carries a password an administrator chose is a password two
    people know before it has been used once.
    """
    hours = settings.password_reset_ttl_hours
    link = reset_url(plaintext, invite=True)
    email_service.send(
        to=user.email,
        subject=f"You have been added to {organization_name} on Percepta",
        body_text=(
            f"{inviter} has added you to {organization_name} on Percepta.\n\n"
            f"Choose a password to finish setting up your account:\n\n{link}\n\n"
            f"The link works once and expires in {hours} hours. If it expires, "
            f"ask an administrator to send another.\n\n"
            f"If you were not expecting this, you can ignore this email - the "
            f"account cannot be used until a password is set.\n"
        ),
    )


def send_added_to_organization(
    *, user: User, organization_name: str, inviter: str
) -> None:
    """Tell somebody who already has an account that they are now in another
    organisation. No link, and nothing they have to act on.

    Exists so that `invite_member` sends *something* either way. Sending only
    to new accounts made the response's `invitation_sent` a reliable answer to
    "does this address already have an account", which is the question the
    endpoint's own docstring says it must not answer — an org admin could
    learn whether an address belongs to a user in somebody else's tenancy.

    Deliberately not a set-password link. An existing account already has a
    password, and mailing its owner a reset link at an org admin's request
    would hand that admin a way to take over an account in another tenancy.
    That control is the reason the two cases differed in the first place; this
    keeps it and removes the signal.
    """
    email_service.send(
        to=user.email,
        subject=f"You have been added to {organization_name} on Percepta",
        body_text=(
            f"{inviter} has added you to {organization_name} on Percepta.\n\n"
            f"Sign in with your existing password and you will see it in the "
            f"organisation switcher. Nothing else is needed.\n\n"
            f"If you were not expecting this, tell whoever administers your "
            f"organisation.\n"
        ),
    )
