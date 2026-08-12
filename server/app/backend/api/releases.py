"""The station-image release catalog.

A platform admin publishes each signed release here — the image, its immutable
digest and a human tag — so the console can offer one-click "update to latest"
without an operator ever pasting a digest. The station's updater still
cosign-verifies the digest against its pinned keys before running it, so this
catalog only chooses which signed image the fleet is offered and is never the
trust anchor for whether an image is signed.

Split by who may do what: publishing and listing the whole catalog are platform
admin only; reading "the latest tag" (for the update-available pill) is open to
any authenticated user, since it is the same version string a station already
reports running — and the digest is withheld from that view.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.dependencies import get_identity
from backend.auth.identity import Identity
from backend.auth.platform import require_platform_admin
from backend.database.dependencies import get_db
from backend.database.models.release import Release
from backend.database.models.user import User
from backend.services.audit import record

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/releases", tags=["releases"])


class ReleaseIn(BaseModel):
    image: str = Field(min_length=1, max_length=512)
    # The immutable pin, validated to the same shape the station enforces, so a
    # mistyped digest is a clear 422 here rather than a rejected update later.
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tag: str = Field(min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)


class ReleaseOut(BaseModel):
    id: str
    image: str
    digest: str
    tag: str
    notes: str | None
    published_at: str
    published_by: str | None


class LatestRelease(BaseModel):
    # All null when nothing has been published — the console then shows no pill.
    tag: str | None = None
    image: str | None = None
    notes: str | None = None
    published_at: str | None = None


@router.get("/latest", response_model=LatestRelease)
def latest_release(
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> LatestRelease:
    """The most recently published release, for the update-available pill.

    Any authenticated user may read it: it is the same version string a station
    already reports running, and knowing a release exists grants nothing. The
    digest is deliberately withheld — the one-click update resolves it
    server-side (api/commands.py) so an operator never handles it.
    """
    r = db.execute(
        select(Release).order_by(Release.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    if r is None:
        return LatestRelease()
    return LatestRelease(
        tag=r.tag,
        image=r.image,
        notes=r.notes,
        published_at=r.created_at.isoformat(),
    )


@router.get("", response_model=list[ReleaseOut])
def list_releases(
    identity: Identity = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> list[ReleaseOut]:
    """The whole catalog, newest first. Platform admins only — publishing and
    auditing releases is platform work."""
    rows = db.execute(
        select(Release).order_by(Release.created_at.desc())
    ).scalars().all()
    wanted = {r.published_by_user_id for r in rows if r.published_by_user_id}
    names: dict[uuid.UUID, str] = {}
    if wanted:
        for user in db.execute(select(User).where(User.id.in_(wanted))).scalars():
            names[user.id] = user.display_name
    return [
        ReleaseOut(
            id=str(r.id),
            image=r.image,
            digest=r.digest,
            tag=r.tag,
            notes=r.notes,
            published_at=r.created_at.isoformat(),
            published_by=names.get(r.published_by_user_id),
        )
        for r in rows
    ]


@router.post("", response_model=ReleaseOut, status_code=201)
def publish_release(
    body: ReleaseIn,
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> ReleaseOut:
    """Record a signed release, making it the new 'latest'. Platform admins only.

    This does not sign or push an image — the release pipeline (cosign + the
    registry) does that. It records which signed digest the fleet should be
    offered next; the station verifies the signature before running it.
    """
    user = db.get(User, identity.user_id)
    release = Release(
        image=body.image.strip(),
        digest=body.digest.strip(),
        tag=body.tag.strip(),
        notes=(body.notes.strip() or None) if body.notes else None,
        published_by_user_id=identity.user_id,
    )
    db.add(release)
    db.flush()
    # created_at is a client-side default (models/common.py), so it is populated
    # by the flush and safe to read before the commit.
    out = ReleaseOut(
        id=str(release.id),
        image=release.image,
        digest=release.digest,
        tag=release.tag,
        notes=release.notes,
        published_at=release.created_at.isoformat(),
        published_by=user.display_name if user else None,
    )
    release_id = release.id
    db.commit()

    record(
        action="release.published",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        actor_email=user.email if user else "",
        target_type="release",
        target_id=str(release_id),
        ip_address=request.client.host if request.client else None,
        detail={"image": body.image, "digest": body.digest, "tag": body.tag},
    )
    return out
