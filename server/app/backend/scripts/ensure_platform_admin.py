"""Create the platform organisation and its first admin, from the environment.

Runs on every start-up, before uvicorn. Idempotent: it creates what is missing
and leaves everything else alone, so restarting a running deployment does
nothing.

This exists because there was previously no way to create a first administrator
except running the development seed - which hardcodes a password that is
committed to the repository - or inserting a row by hand. Both are wrong for a
real bring-up, and the second is undocumented.

The model is DroneOps', and the reasoning carries across intact.

**The platform organisation is a real organisation with a fixed id** of all
zeroes. Platform access is membership of it, which means it needs no separate
permission table and no new column on `users` - the existing membership and
role machinery answers "is this person a platform admin" without a second
mechanism that could disagree with the first.

**God mode is scoped to the session's active organisation, not to the person.**
A platform admin is only a superuser while their active org *is* the platform
org. Working inside a customer's organisation they see exactly what that
organisation's own members see, and row-level security binds them to it. That
distinction is what stops "platform admin" becoming a permanent cross-tenant
read on every request they ever make.

**The password is never written to the database in plain form and never logged.**
If PLATFORM_ADMIN_PASSWORD is unset, the account is created without a password
and cannot be signed in to until one is set - a deliberately inert account beats
a guessable one.
"""

import logging

from sqlalchemy import select

from backend.auth.password import PasswordError, hash_password
from backend.core.config import settings
from backend.database.models.enums import UserRole
from backend.database.models.organization import Organization
from backend.database.models.organization_membership import OrganizationMembership
from backend.database.models.user import User
from backend.database.session import PrivilegedSessionLocal

logger = logging.getLogger("startup")

from backend.auth.platform import (  # noqa: E402
    PLATFORM_ORGANIZATION_ID,
    PLATFORM_ORGANIZATION_NAME,
)


def ensure_platform_admin() -> None:
    email = (settings.platform_admin_email or "").strip().lower()
    if not email:
        logger.info(
            "PLATFORM_ADMIN_EMAIL is unset - no platform administrator will be "
            "created. Set it to bootstrap one."
        )
        return

    # Privileged session: this runs before any request, so there is no org
    # context, and it is the code that establishes the first one.
    with PrivilegedSessionLocal() as db:
        org = db.get(Organization, PLATFORM_ORGANIZATION_ID)
        if org is None:
            org = Organization(
                id=PLATFORM_ORGANIZATION_ID, name=PLATFORM_ORGANIZATION_NAME
            )
            db.add(org)
            db.flush()
            logger.info("Created the platform organisation.")

        user = db.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()

        created = False
        if user is None:
            password_hash = None
            if settings.platform_admin_password:
                try:
                    password_hash = hash_password(settings.platform_admin_password)
                except PasswordError as exc:
                    logger.error(
                        "PLATFORM_ADMIN_PASSWORD rejected: %s The account will be "
                        "created without a password and cannot be signed in to.",
                        exc,
                    )
            else:
                logger.warning(
                    "PLATFORM_ADMIN_PASSWORD is unset. Creating %s with no "
                    "password - it cannot be signed in to until one is set.",
                    email,
                )
            user = User(
                email=email,
                display_name=settings.platform_admin_name or email,
                password_hash=password_hash,
                is_active=True,
            )
            db.add(user)
            db.flush()
            created = True

        membership = db.execute(
            select(OrganizationMembership).where(
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.organization_id == PLATFORM_ORGANIZATION_ID,
            )
        ).scalar_one_or_none()
        if membership is None:
            db.add(
                OrganizationMembership(
                    user_id=user.id,
                    organization_id=PLATFORM_ORGANIZATION_ID,
                    roles=[UserRole.ADMIN.value],
                )
            )
            logger.info("Granted %s platform administrator access.", email)
        elif UserRole.ADMIN.value not in (membership.roles or []):
            membership.roles = sorted({*(membership.roles or []), UserRole.ADMIN.value})

        db.commit()

    # Never log the password, and never log a hash either - both belong only in
    # the row that was just written.
    if created:
        logger.info("Created platform administrator %s.", email)
    else:
        logger.info(
            "Platform administrator %s already exists; left its password alone. "
            "Changing PLATFORM_ADMIN_PASSWORD does not reset it - use the "
            "console, so the change is audited.",
            email,
        )
