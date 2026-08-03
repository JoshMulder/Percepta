"""Admin side of enrolment: issuing codes, and taking them away.

Behind `config.write` at the station, which is an admin-level capability. This
is deliberately a higher bar than operating the station: someone who can issue
an enrolment code can attach hardware to a customer's org, and someone who can
revoke one can take a site off the air.

Every route here writes an audit row. That is not decoration - "who let that box
onto our platform, and when" is the question this table exists to answer, and it
is asked after something has already gone wrong.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.capabilities import Capability
from backend.auth.dependencies import require_capability
from backend.auth.identity import Identity
from backend.database.dependencies import get_db
from backend.database.models.ground_station import GroundStation
from backend.database.models.station_credential import StationCredential
from backend.database.models.station_enrolment_token import StationEnrolmentToken
from backend.services import broker_acl, enrolment
from backend.services.audit import record

router = APIRouter(prefix="/api/stations/{station_id}/enrolment", tags=["enrolment"])


class IssuedToken(BaseModel):
    """The plaintext appears here once and is never retrievable again.

    **The code, and nothing folded into it.** This used to also offer
    `CODE@host#sha256` — the code with this platform's address and the
    fingerprint of its CA — as one string for an installer to carry, for
    `bootstrap.sh --enrol`.

    Three things retired it. That flag no longer exists: bootstrap takes no
    arguments and asks for the platform's address itself. The address is
    therefore already on the box before a code is ever typed. And the
    fingerprint pins the platform's own CA, which is meaningless once it is
    behind a proxy with a publicly trusted certificate, and worse than
    meaningless when the box pins it and the proxy then answers — that is an
    enrolment refused for a certificate that was correct.

    It also did not fit. A station's `token` field is capped at 64 characters
    by the contract; the combined string is a hundred and three on any real
    host. Somebody handed a string pastes the string, so every paste of it was
    a validation failure that reached the technician as "the box sent
    something the platform could not read."
    """

    token: str
    expires_at: str


class EnrolmentStatus(BaseModel):
    station_id: str
    enrolled: bool
    enrolled_at: str | None
    hardware: dict | None
    config_version: int
    credential_expires_at: str | None
    credential_valid: bool
    broker_provisioned: bool
    token_outstanding: bool
    # Whether that live code has already been used. The difference between
    # "waiting for a technician" and "enrolled, retry window still open" is the
    # thing an admin looking at this page actually wants to know.
    token_claimed: bool
    token_expires_at: str | None


def _station(db: Session, station_id: uuid.UUID) -> GroundStation:
    station = db.get(GroundStation, station_id)
    if station is None:
        # Same 404 the capability check gives, for the same reason: never
        # confirm the existence of hardware the caller may not touch.
        raise HTTPException(status_code=404, detail="Station not available")
    return station


@router.get("", response_model=EnrolmentStatus)
def get_status(
    station_id: uuid.UUID,
    identity: Identity = Depends(require_capability(Capability.CONFIG_WRITE)),
    db: Session = Depends(get_db),
) -> EnrolmentStatus:
    station = _station(db, station_id)

    credentials = db.execute(
        select(StationCredential)
        .where(StationCredential.ground_station_id == station_id)
        .order_by(StationCredential.created_at.desc())
    ).scalars().all()
    live = [c for c in credentials if enrolment.is_valid(c)]
    newest = live[0] if live else (credentials[0] if credentials else None)

    token = db.execute(
        select(StationEnrolmentToken)
        .where(StationEnrolmentToken.ground_station_id == station_id)
        .order_by(StationEnrolmentToken.created_at.desc())
    ).scalars().first()
    token_live = bool(
        token
        and token.revoked_at is None
        and token.expires_at > datetime.now(UTC)
    )

    return EnrolmentStatus(
        station_id=str(station.id),
        enrolled=station.enrolled_at is not None,
        enrolled_at=station.enrolled_at.isoformat() if station.enrolled_at else None,
        hardware=station.hardware,
        config_version=station.config_version,
        credential_expires_at=newest.expires_at.isoformat() if newest else None,
        credential_valid=bool(live),
        broker_provisioned=bool(live and live[0].broker_provisioned),
        token_outstanding=token_live,
        token_claimed=bool(token and token.claimed_at is not None),
        token_expires_at=token.expires_at.isoformat() if token and token_live else None,
    )


@router.post("/token", response_model=IssuedToken, status_code=201)
def issue_token(
    station_id: uuid.UUID,
    request: Request,
    identity: Identity = Depends(require_capability(Capability.CONFIG_WRITE)),
    db: Session = Depends(get_db),
) -> IssuedToken:
    """Issue an enrolment code for a technician to type into the box.

    Any previous unused code for this station stops working. Two live codes for
    one station is a way to enrol the wrong box and not find out.
    """
    station = _station(db, station_id)
    token, plaintext = enrolment.issue_token(
        db, station=station, issued_by_user_id=identity.user_id
    )
    expires_at = token.expires_at
    db.commit()

    record(
        action="station.enrolment_token.issued",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        ground_station_id=station_id,
        target_type="ground_station",
        target_id=str(station_id),
        ip_address=request.client.host if request.client else None,
        detail={"expires_at": expires_at.isoformat()},
    )
    return IssuedToken(token=plaintext, expires_at=expires_at.isoformat())


@router.delete("/token", status_code=200)
def revoke_token(
    station_id: uuid.UUID,
    request: Request,
    identity: Identity = Depends(require_capability(Capability.CONFIG_WRITE)),
    db: Session = Depends(get_db),
) -> dict:
    """Stop outstanding codes working - a code read to the wrong person, say."""
    _station(db, station_id)
    count = enrolment.revoke_tokens(db, station_id=station_id)
    db.commit()

    record(
        action="station.enrolment_token.revoked",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        ground_station_id=station_id,
        target_type="ground_station",
        target_id=str(station_id),
        ip_address=request.client.host if request.client else None,
        detail={"revoked": count},
    )
    return {"revoked": count}


@router.post("/revoke", status_code=200)
def revoke_credentials(
    station_id: uuid.UUID,
    request: Request,
    identity: Identity = Depends(require_capability(Capability.CONFIG_WRITE)),
    db: Session = Depends(get_db),
) -> dict:
    """Cut a station off now.

    Immediate and without overlap, unlike a renewal. The station keeps sensing
    and recording locally - it is cut off, not disabled - and the record, its
    grants and its history all survive, so replacing the hardware is a new code
    rather than a new station.
    """
    station = _station(db, station_id)
    revoked = enrolment.revoke_credentials(
        db, station_id=station_id, reason="admin-revoked"
    )
    enrolment.revoke_tokens(db, station_id=station_id)
    # Cleared so the station shows as never-enrolled and a fresh code is the
    # obvious next step rather than an error.
    station.enrolled_at = None
    db.commit()

    # sync_station with nothing valid deprovisions, and also kills any live
    # connection - see broker_acl.deprovision.
    dropped = broker_acl.deprovision(station_id)

    record(
        action="station.credential.revoked",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        ground_station_id=station_id,
        target_type="ground_station",
        target_id=str(station_id),
        ip_address=request.client.host if request.client else None,
        detail={"credentials": len(revoked), "broker_principal_removed": dropped},
    )
    return {"revoked": len(revoked), "broker_principal_removed": dropped}
