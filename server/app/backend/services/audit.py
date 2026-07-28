"""Append-only record of who did what.

Shared by every path that needs to write one, so the rules live in a single
place rather than being re-decided per caller.

Two of those rules matter. It always writes on the **privileged** session,
because `audit_logs` sits outside RLS (migration 0002) and some of the most
important rows - a failed enrolment, a login for an unknown address - happen
before any org context exists. And a failure to write **never** propagates: an
audit row is evidence about an action, not permission for it, so losing the
evidence must not also lose the action. It is logged loudly instead.

Never put a secret in `detail`. Token and credential values are hashed
everywhere else precisely so they cannot be recovered; writing one here would
undo that in the one table nobody ever deletes from.
"""

import logging
import uuid

from backend.database.models.audit_log import AuditLog
from backend.database.session import PrivilegedSessionLocal

log = logging.getLogger(__name__)


def record(
    *,
    action: str,
    organization_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_email: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    ground_station_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    detail: dict | None = None,
) -> None:
    try:
        with PrivilegedSessionLocal() as db:
            db.add(
                AuditLog(
                    organization_id=organization_id,
                    actor_user_id=actor_user_id,
                    actor_email=actor_email,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    ground_station_id=ground_station_id,
                    device_id=device_id,
                    ip_address=ip_address,
                    detail=detail,
                )
            )
            db.commit()
    except Exception:
        log.exception("Failed to write audit row for %s.", action)
