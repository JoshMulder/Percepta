"""Who reached into a customer's system, and when.

A command centre that can open a root shell on somebody else's station, proxy
their settings page, listen to their airband and publish the image their hardware
installs must be able to show what it did. Nothing read `audit_logs` before this
file — the table has been written to since migration 0002 and never once read
back by the product.

TWO THINGS MAKE THIS THE MOST DANGEROUS READ IN THE CODEBASE.

`audit_logs` HAS NO ROW-LEVEL SECURITY AT ALL. Not "is exempt": it is absent from
RLS_TABLES in migration 0002, deliberately, because rows are written before any
org context exists — a failed enrolment, a login for an address belonging to
nobody. Every other table in this system fails CLOSED when a query forgets to
scope itself; a policy that matches nothing returns nothing. This one fails OPEN.
A forgotten `organization_id` predicate here does not return an error or an empty
list, it returns every tenant's history to whoever asked.

So the org filter lives in ONE function, `_scoped`, and every route goes through
it. Not because that is tidy, but because the alternative is trusting each future
route to remember something the database will not remind anyone about.

PLATFORM ADMIN, NOT WATCH. `require_platform_admin`, deliberately, where the rest
of ODIN uses `require_odin_watch`. The whole point of that split (auth/platform.py)
is that a watch operator on shift does not carry root — and this table is the
record OF root access. An operator who could read it could see the enrolment
tokens, credential rotations and shell sessions of every customer, which is a
strictly larger capability than watching a wall.

NO TENANT-FACING ROUTE HERE, and that is a decision rather than an omission.
docs/ODIN.md records **DECIDED 2026-08-14: SILENT** — cross-tenant listening is
accountable, not disclosed. An org-admin view of their own audit trail was part
of the "Disclosed" option that was rejected. It remains easy to add if that
decision changes; it should not arrive by accident because a filter happened to
be parameterised.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Select, select, tuple_
from sqlalchemy.orm import Session

from backend.auth.identity import Identity
from backend.auth.platform import require_platform_admin
from backend.database.dependencies import get_db
from backend.database.models.audit_log import AuditLog

router = APIRouter(prefix="/api/odin", tags=["odin"])

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
DEFAULT_WINDOW = timedelta(days=30)

#: Named groups of actions, mapped EXPLICITLY rather than by prefix.
#:
#: A prefix filter is the obvious implementation and it is wrong here. The
#: actions this feature exists to surface — somebody opening a root shell on a
#: customer's station, proxying their settings page — are called
#: `host_shell_open`, `host_shell_close` and `console_open`. They carry no dotted
#: namespace at all, because they predate the convention. `action LIKE 'odin.%'`
#: would return a tidy, plausible, and completely misleading list that omitted
#: every one of them.
#:
#: So the vocabulary is written out. It goes stale when somebody adds an action
#: and forgets this file — which is why an unknown `group` is a 400 naming the
#: groups that exist, and why filtering by exact `action` is always available as
#: the escape hatch.
ACTION_GROUPS: dict[str, tuple[str, ...]] = {
    # The reach-in surfaces. The reason this API exists.
    "reach": (
        "host_shell_open",
        "host_shell_close",
        "console_open",
        "odin.watch.join",
    ),
    # Changes an operator made to somebody else's alerts or maintenance state.
    "odin": (
        "odin.alert.ack",
        "odin.alert.close",
        "odin.station.maintenance",
        "odin.watch.join",
    ),
    # Anything that alters a station's identity, credentials or configuration.
    "station": (
        "station.created",
        "station.deleted",
        "station.config.updated",
        "station.credential.renewed",
        "station.credential.revoked",
        "station.enrolled",
        "station.enrolment_token.issued",
        "station.enrolment_token.revoked",
        "station.radio_presets.updated",
        "station_update",
    ),
    # Refusals. Worth a group of their own: a run of these is what an attempted
    # intrusion looks like, and they are otherwise scattered across three areas.
    "refused": (
        "login_blocked",
        "login_failed",
        "station.enrol.rejected",
        "station.enrol.rate_limited",
        "station.renew.rejected",
        "account.email.rejected",
        "account.password.rejected",
    ),
    # Who can do what, and to whom.
    "access": (
        "organization.grant.updated",
        "organization.member.invited",
        "organization.roles.updated",
        "organization.mfa_required.updated",
        "platform.membership.updated",
        "platform.membership.removed",
    ),
    # What we published that every station then installs.
    "release": ("release.published",),
}


class AuditRow(BaseModel):
    id: str
    #: NULLABLE, and not an oversight. Rows written before any org context
    #: exists — a failed enrolment, a login for an unknown address — have none,
    #: and those are among the most interesting rows in the table.
    organization_id: str | None
    actor_user_id: str | None
    actor_email: str | None
    action: str
    target_type: str | None
    target_id: str | None
    ground_station_id: str | None
    device_id: str | None
    #: Optional. `host_shell_close` writes no IP, and `odin.watch.join` writes
    #: neither IP nor detail — a response model that required them would 500 on
    #: precisely the rows this endpoint was built to show.
    ip_address: str | None
    detail: dict | None
    created_at: str


class AuditPage(BaseModel):
    rows: list[AuditRow]
    next_cursor: str | None
    has_more: bool


def _encode(created_at: datetime, row_id: uuid.UUID) -> str:
    """Opaque, and URL-SAFE, which is not the same thing.

    The obvious cursor is `f"{iso}|{id}"` and it is broken in a way that only
    shows up once a second page is actually requested: an ISO timestamp carries
    its UTC offset as `+00:00`, and `+` in a query string decodes to a SPACE.
    The server then reads back "…52.648565 00:00", cannot parse it, and answers
    400 — so paging works perfectly until the moment there is a second page.

    base64url has no `+` and no `/`, and it also makes the cursor genuinely
    opaque rather than merely described as such: nothing outside these two
    functions can come to depend on its shape.
    """
    raw = f"{created_at.isoformat()}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    # Padding is stripped on the way out, so it is restored on the way in.
    padded = cursor + "=" * (-len(cursor) % 4)
    when, _, row_id = base64.urlsafe_b64decode(padded).decode().partition("|")
    return datetime.fromisoformat(when), uuid.UUID(row_id)


def _scoped(
    statement: Select,
    *,
    organization_id: uuid.UUID | None,
    include_unscoped: bool,
) -> Select:
    """THE choke point. Every read of audit_logs goes through here.

    With no organisation named, a platform administrator sees every row — which
    is the correct answer for the people who run the platform, and is why the
    guard on these routes is admin rather than watch.

    With one named, rows for that org only. `include_unscoped` additionally
    admits the org-less rows, because a failed login for an address that belongs
    to nobody and a rejected enrolment for an unknown station are exactly what
    somebody investigating an organisation wants to see alongside its own
    history — but they are not that organisation's rows, so admitting them is a
    choice the caller makes rather than a default.
    """
    if organization_id is None:
        return statement
    if include_unscoped:
        return statement.where(
            (AuditLog.organization_id == organization_id)
            | (AuditLog.organization_id.is_(None))
        )
    return statement.where(AuditLog.organization_id == organization_id)


@router.get("/audit", response_model=AuditPage)
def odin_audit(
    organization_id: uuid.UUID | None = None,
    ground_station_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    action: str | None = Query(None, description="Exact action string."),
    group: str | None = Query(
        None, description=f"One of: {', '.join(sorted(ACTION_GROUPS))}."
    ),
    since: datetime | None = None,
    until: datetime | None = None,
    include_unscoped: bool = False,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    identity: Identity = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> AuditPage:
    """The audit trail, newest first.

    Same compound cursor as the event browser and for a related reason: audit
    rows are written in bursts (an enrolment writes several in one request) and
    `created_at` is not unique, so a bare timestamp cursor drops or repeats rows.
    """
    if group is not None and group not in ACTION_GROUPS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown group; try one of {', '.join(sorted(ACTION_GROUPS))}",
        )

    capped = max(1, min(MAX_LIMIT, limit))
    floor = since or (datetime.now(UTC) - DEFAULT_WINDOW)

    statement = select(AuditLog).where(AuditLog.created_at >= floor)
    statement = _scoped(
        statement,
        organization_id=organization_id,
        include_unscoped=include_unscoped,
    )

    if ground_station_id is not None:
        statement = statement.where(AuditLog.ground_station_id == ground_station_id)
    if actor_user_id is not None:
        statement = statement.where(AuditLog.actor_user_id == actor_user_id)
    if action is not None:
        statement = statement.where(AuditLog.action == action)
    if group is not None:
        statement = statement.where(AuditLog.action.in_(ACTION_GROUPS[group]))
    if until is not None:
        statement = statement.where(AuditLog.created_at <= until)

    if cursor:
        try:
            cursor_at, cursor_id = _decode(cursor)
        except Exception:
            # Any shape of malformed cursor is one answer: it is a value this
            # server produced, so a client sending a broken one has
            # corrupted it rather than discovered something.
            raise HTTPException(status_code=400, detail="bad cursor")
        # `tuple_()`, NOT a plain Python tuple of columns.
        #
        # `(AuditLog.created_at, AuditLog.id) < (a, b)` is Python's own tuple
        # comparison over SQLAlchemy objects, and it does not build the row-value
        # SQL it looks like it builds. It compiled, the request answered 200, and
        # the second page came back EMPTY — paging that stops after one page and
        # reports no error, which reads as "there was only one page".
        statement = statement.where(
            tuple_(AuditLog.created_at, AuditLog.id) < tuple_(cursor_at, cursor_id)
        )

    statement = statement.order_by(
        AuditLog.created_at.desc(), AuditLog.id.desc()
    ).limit(capped + 1)

    rows = list(db.execute(statement).scalars())
    has_more = len(rows) > capped
    rows = rows[:capped]

    return AuditPage(
        rows=[
            AuditRow(
                id=str(r.id),
                organization_id=str(r.organization_id) if r.organization_id else None,
                actor_user_id=str(r.actor_user_id) if r.actor_user_id else None,
                actor_email=r.actor_email,
                action=r.action,
                target_type=r.target_type,
                target_id=r.target_id,
                ground_station_id=(
                    str(r.ground_station_id) if r.ground_station_id else None
                ),
                device_id=str(r.device_id) if r.device_id else None,
                ip_address=r.ip_address,
                detail=r.detail,
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ],
        next_cursor=_encode(rows[-1].created_at, rows[-1].id) if rows and has_more else None,
        has_more=has_more,
    )


@router.get("/audit/actions", response_model=dict[str, list[str]])
def odin_audit_actions(
    identity: Identity = Depends(require_platform_admin),
) -> dict[str, list[str]]:
    """The group vocabulary, so a client does not hard-code a second copy of it.

    Returned from the same constant the filter uses, so a UI built on this cannot
    offer a group the server does not implement.
    """
    return {name: list(actions) for name, actions in sorted(ACTION_GROUPS.items())}
