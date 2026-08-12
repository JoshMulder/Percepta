"""What a user can change about themselves.

Nothing here touches permissions. A user may rename themselves and change their
own password; what they can *reach* is decided by an admin, in
`api/organization.py`. Keeping those apart is deliberate - it means no
self-service route can widen access, so this file never needs to be audited for
privilege escalation.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.dependencies import get_identity
from backend.auth.identity import Identity
from backend.auth.password import PasswordError, hash_password, verify_password
from backend.core.email import EmailNotConfiguredError
from backend.database.dependencies import get_db
from backend.database.models.auth_session import AuthSession
from backend.database.models.common import utcnow
from backend.database.models.user import User
from backend.database.session import PrivilegedSessionLocal
from backend.realtime.revocation import revoke_session
from backend.services import email_change
from backend.services.audit import record

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/account", tags=["account"])

class ProfileUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)


class PasswordChange(BaseModel):
    current_password: str
    # Deliberately unconstrained here. auth/password.py owns the policy - a
    # minimum length and a 72-*byte* bcrypt ceiling - and duplicating it as
    # pydantic character limits would disagree with it on any multibyte
    # password, turning a clear 400 into a 500.
    new_password: str


class EmailChangeRequest(BaseModel):
    # A light check only — the real proof the address is valid and reachable is
    # that the verification link sent to it gets opened. Kept permissive so a
    # legitimate but unusual address is not refused by an over-strict regex.
    new_email: str = Field(min_length=3, max_length=320)
    current_password: str


class ProfileResponse(BaseModel):
    user_id: str
    email: str
    display_name: str


@router.patch("/profile", response_model=ProfileResponse)
def update_profile(
    body: ProfileUpdate,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> ProfileResponse:
    """Rename yourself.

    The email address is deliberately not editable here. It is the login
    identifier and appears in audit rows as free text so those rows survive the
    account being deleted; letting a user change it would quietly rewrite who
    an old audit entry appears to be about.
    """
    user = db.get(User, identity.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Account not found")

    previous = user.display_name
    user.display_name = body.display_name.strip()
    # Read back before committing. `users` happens to sit outside RLS so a
    # post-commit refresh would work here, but relying on that couples this
    # endpoint to a table exclusion it has no reason to know about - and the
    # same shape raises ObjectDeletedError on every org-scoped table.
    result = ProfileResponse(
        user_id=str(user.id), email=user.email, display_name=user.display_name
    )
    email = user.email
    db.commit()

    record(
        action="account.profile.updated",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        actor_email=email,
        target_type="user",
        target_id=result.user_id,
        ip_address=request.client.host if request.client else None,
        detail={"from": previous, "to": result.display_name},
    )
    return result


@router.post("/password", status_code=200)
def change_password(
    body: PasswordChange,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> dict:
    """Change your own password, and end every other session.

    The current password is required even though the caller is already
    authenticated. That is what makes this resistant to a borrowed session - an
    unattended desk, a stolen cookie - rather than merely to a stranger.

    Ending other sessions is the point of the operation as much as the new
    password is: someone changing their password because they think it is
    compromised has gained nothing if the attacker's existing login keeps
    working. Revocation is pushed as well as written, because a WebSocket makes
    no further HTTP requests and would otherwise stream on for hours - see
    docs/03-realtime-isolation.md section 6.

    This session is deliberately kept alive. Signing the user out of the tab
    they are currently typing in is a worse experience for no security gain;
    they have just proved they hold the current password.
    """
    user = db.get(User, identity.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Account not found")

    if not verify_password(body.current_password, user.password_hash):
        record(
            action="account.password.rejected",
            organization_id=identity.organization_id,
            actor_user_id=identity.user_id,
            actor_email=user.email,
            ip_address=request.client.host if request.client else None,
            detail={"reason": "current-password-wrong"},
        )
        raise HTTPException(status_code=400, detail="Current password is not correct")

    if body.new_password == body.current_password:
        raise HTTPException(
            status_code=400, detail="The new password must be different"
        )

    try:
        user.password_hash = hash_password(body.new_password)
    except PasswordError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user_email = user.email
    db.commit()

    # Privileged: auth_sessions sits outside RLS (migration 0002), because the
    # auth flow reads it before an org context exists.
    with PrivilegedSessionLocal() as priv:
        rows = priv.execute(
            select(AuthSession).where(
                AuthSession.user_id == identity.user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.id != identity.session_id,
            )
        ).scalars().all()
        # Read the ids out before committing. commit() expires every loaded
        # attribute, and the session closes on the way out of this block, so
        # touching row.id afterwards would raise DetachedInstanceError.
        ended_ids = [row.id for row in rows]
        for row in rows:
            row.revoked_at = utcnow()
        priv.commit()

    for session_id in ended_ids:
        # Push each one, so any socket still held open on it closes now rather
        # than at the next revalidation sweep.
        revoke_session(session_id)
    ended = len(ended_ids)

    record(
        action="account.password.changed",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        actor_email=user_email,
        target_type="user",
        target_id=str(identity.user_id),
        ip_address=request.client.host if request.client else None,
        detail={"other_sessions_ended": ended},
    )
    return {"other_sessions_ended": ended}


@router.post("/email", status_code=202)
def request_email_change(
    body: EmailChangeRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> dict:
    """Start a self-service email change.

    Verify the current password, then send a confirmation link to the NEW
    address. Nothing about the account moves here — the address changes only when
    that link is opened (POST /api/auth/email-change/redeem), which is the proof
    that the person controls the new mailbox.

    The current password is required for the same reason the password change
    requires it: the email is the sign-in identifier, so moving it is exactly
    what a borrowed session — an unattended desk, a stolen cookie — would be used
    for.
    """
    user = db.get(User, identity.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Account not found")

    if not verify_password(body.current_password, user.password_hash):
        record(
            action="account.email.rejected",
            organization_id=identity.organization_id,
            actor_user_id=identity.user_id,
            actor_email=user.email,
            ip_address=request.client.host if request.client else None,
            detail={"reason": "current-password-wrong"},
        )
        raise HTTPException(status_code=400, detail="Current password is not correct")

    new_email = email_change.normalise(body.new_email)
    if "@" not in new_email or "." not in new_email.split("@")[-1]:
        raise HTTPException(
            status_code=400, detail="That does not look like an email address"
        )
    if new_email == email_change.normalise(user.email):
        raise HTTPException(status_code=400, detail="That is already your email address")

    # Refuse early if the address is plainly taken. Not the authoritative check —
    # the redeem re-checks at the moment it lands — but a clearer 'no' now than a
    # link that will fail when opened.
    taken = db.execute(select(User).where(User.email == new_email)).scalar_one_or_none()
    if taken is not None:
        raise HTTPException(status_code=409, detail="That address is already in use")

    token, plaintext = email_change.issue(db, user=user, new_email=new_email)
    old_email = user.email
    db.commit()

    try:
        email_change.send(new_email=new_email, plaintext=plaintext)
    except EmailNotConfiguredError as exc:
        raise HTTPException(
            status_code=503,
            detail="Email is not configured, so the verification link could not be sent.",
        ) from exc

    record(
        action="account.email.requested",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        actor_email=old_email,
        target_type="user",
        target_id=str(identity.user_id),
        ip_address=request.client.host if request.client else None,
        detail={"from": old_email, "to": new_email},
    )
    return {"sent_to": new_email}
