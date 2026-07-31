"""Admin side of enrolment: issuing codes, and taking them away.

Behind `config.write` at the station, which is an admin-level capability. This
is deliberately a higher bar than operating the station: someone who can issue
an enrolment code can attach hardware to a customer's org, and someone who can
revoke one can take a site off the air.

Every route here writes an audit row. That is not decoration - "who let that box
onto our platform, and when" is the question this table exists to answer, and it
is asked after something has already gone wrong.
"""

import hashlib
import ssl
import uuid
from pathlib import Path
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

    `bootstrap` is the same code with the two other facts a station needs
    folded in: where this platform is, and which CA to pin. All three had to
    reach the box anyway, by three routes, and the one that was easiest to skip
    was the fingerprint — which is the one that decides whether the code is
    typed into the real platform or into whatever answered.
    """

    token: str
    expires_at: str
    #: `CODE@host#sha256`, for `bootstrap.sh --enrol`.
    bootstrap: str


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


#: Where the CA lives inside the app container — the same file `/ca.crt`
#: serves. Absent in a deployment behind a publicly trusted certificate, and
#: then there is no fingerprint to carry and nothing to pin.
CA_PATH = Path("/certs/ca.crt")


def _ca_fingerprint() -> str:
    """The SHA-256 of the pinned CA, lowercase and unpunctuated.

    Read per call rather than cached: a CA rotation is rare but it is exactly
    the moment a stale value would send a technician to site with a fingerprint
    that no longer matches, and this is not a hot path.
    """
    if not CA_PATH.is_file():
        return ""
    der = ssl.PEM_cert_to_DER_cert(CA_PATH.read_text())
    return hashlib.sha256(der).hexdigest()


def _bootstrap_string(plaintext: str, request: Request) -> str:
    """`CODE@host#fingerprint` — one thing for an installer to carry.

    The host comes from the request, so it is whatever name the operator
    actually reached this platform by. Deriving it from configuration instead
    produced a string that was right in the deployment it was written in and
    wrong behind every proxy.
    """
    host = request.headers.get("host", "").split(":")[0]
    fingerprint = _ca_fingerprint()
    out = plaintext
    if host:
        out += f"@{host}"
    if fingerprint:
        out += f"#{fingerprint}"
    return out


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
    return IssuedToken(
        token=plaintext,
        expires_at=expires_at.isoformat(),
        bootstrap=_bootstrap_string(plaintext, request),
    )


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
