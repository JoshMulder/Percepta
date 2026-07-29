"""Station-facing enrolment. The only endpoints a ground station ever calls.

`contract/enrolment.md` §4. Everything here is initiated by the station, because
Starlink is CGNAT and the platform can never reach inward - including renewal,
which is why a station that stops renewing is a problem the station has to
notice for itself.

These routes are not part of the operator API and take no session cookie. They
authenticate with a token (claim) or a credential (renew), and they are the only
place in the platform where an unauthenticated caller can cause a write.
"""

import logging
import pathlib
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from backend.core import ratelimit
from backend.core.config import settings
from backend.database.session import PrivilegedSessionLocal
from backend.services import broker_acl, enrolment
from backend.services.audit import record

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/enrol", tags=["enrolment"])

#: Claim attempts allowed per source address per window. Generous enough that a
#: technician mistyping a code repeatedly is never blocked, tight enough that
#: the endpoint is not a useful thing to hammer.
CLAIM_LIMIT = 20
CLAIM_WINDOW = 300

RENEW_LIMIT = 30
RENEW_WINDOW = 300


class Hardware(BaseModel):
    """Inventory, explicitly not trust. Nothing here influences what the station
    is allowed to do, which is what makes it safe to accept unverified."""

    model: str | None = None
    serial: str | None = None
    os: str | None = None
    agent_version: str | None = None


class ClaimRequest(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    # PEM, for the mTLS path. Accepted and ignored while credentials are bearer
    # secrets, so the station side can send it from the start and the swap is a
    # platform change only.
    public_key: str | None = Field(default=None, max_length=8192)
    hardware: Hardware | None = None


class CredentialOut(BaseModel):
    type: str
    secret: str
    expires_at: str
    renew_after: str


class BrokerOut(BaseModel):
    url: str
    #: The CA the station pins, per contract/enrolment.md §4. Sent once, at
    #: enrolment, over a connection the station could not yet verify - which is
    #: the standard trust-on-first-use compromise, and is why the enrolment
    #: token is short-lived and single-station. Everything afterwards is
    #: verified against this.
    ca_pem: str | None = None
    telemetry_topic: str
    audio_topic: str
    video_topic: str
    command_topic: str
    username: str


class StationOut(BaseModel):
    name: str
    timezone: str
    latitude: float | None
    longitude: float | None


class EnrolResponse(BaseModel):
    station_id: str
    credential: CredentialOut
    broker: BrokerOut
    station: StationOut
    config_version: int


def _source(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _ca_pem() -> str | None:
    """The CA certificate, or None if this deployment has not generated one.

    None means the station has nothing to verify against and must refuse to
    treat the link as secure. It is not a reason to fall back to plaintext -
    a station that silently downgrades is worse than one that will not start,
    because nobody finds out.
    """
    try:
        return pathlib.Path(settings.tls_ca_file).read_text()
    except Exception:
        log.warning(
            "No CA certificate at %s - stations will be enrolled with nothing "
            "to pin. Run scripts/make_certs.sh.", settings.tls_ca_file,
        )
        return None


def _broker(station_id: uuid.UUID) -> BrokerOut:
    return BrokerOut(
        url=settings.station_broker_url or settings.redis_url,
        ca_pem=_ca_pem(),
        telemetry_topic=f"gsu/{station_id}/telemetry",
        audio_topic=f"gsu/{station_id}/audio",
        video_topic=f"gsu/{station_id}/video",
        command_topic=f"cmd/gsu/{station_id}",
        username=broker_acl.principal(station_id),
    )


def _response(issued: enrolment.IssuedCredential) -> EnrolResponse:
    station = issued.station
    return EnrolResponse(
        station_id=str(station.id),
        credential=CredentialOut(
            type=issued.credential.kind,
            secret=issued.secret,
            expires_at=issued.credential.expires_at.isoformat(),
            # The platform owns the renewal policy and states it, rather than
            # each station hardcoding half of a lifetime it was told.
            renew_after=(
                issued.credential.expires_at - enrolment.CREDENTIAL_TTL
                + enrolment.RENEW_AFTER
            ).isoformat(),
        ),
        broker=_broker(station.id),
        station=StationOut(
            name=station.name,
            timezone=station.timezone,
            latitude=station.latitude,
            longitude=station.longitude,
        ),
        config_version=station.config_version,
    )


@router.post("", response_model=EnrolResponse)
def claim(body: ClaimRequest, request: Request) -> EnrolResponse:
    """Exchange an enrolment token for a credential.

    Unauthenticated: the token is the authentication. Privileged session,
    because this is the step that decides which org the box belongs to and so
    runs before any org context can exist.
    """
    source = _source(request)
    if not ratelimit.check(
        f"enrol:{source}", limit=CLAIM_LIMIT, window_seconds=CLAIM_WINDOW
    ):
        record(
            action="station.enrol.rate_limited",
            ip_address=source,
            target_type="enrolment",
        )
        raise HTTPException(status_code=429, detail="Too many attempts")

    with PrivilegedSessionLocal() as db:
        try:
            issued = enrolment.claim(
                db,
                token_value=body.token,
                hardware=body.hardware.model_dump(exclude_none=True)
                if body.hardware
                else None,
            )
        except enrolment.AlreadyEnrolled:
            db.rollback()
            record(
                action="station.enrol.rejected",
                ip_address=source,
                target_type="enrolment",
                detail={"reason": "already-enrolled"},
            )
            raise HTTPException(
                status_code=409, detail="This station is already set up."
            )
        except enrolment.InvalidToken:
            db.rollback()
            # No station id: we may not have resolved one, and a 404 must not
            # become a way to test whether a station exists.
            record(
                action="station.enrol.rejected",
                ip_address=source,
                target_type="enrolment",
                detail={"reason": "invalid-token"},
            )
            raise HTTPException(
                status_code=404, detail="This code is not valid. Ask for a new one."
            )

        station_id = issued.station.id
        organization_id = issued.station.organization_id
        # Sync from the database rather than from the secret in hand: it
        # recomputes the exact set of hashes that should work, so revocation of
        # the previous credential takes effect in the same call.
        db.flush()
        provisioned = broker_acl.sync_station(db, station_id)
        issued.credential.broker_provisioned = provisioned
        response = _response(issued)
        db.commit()

    record(
        action="station.enrolled",
        organization_id=organization_id,
        ground_station_id=station_id,
        target_type="ground_station",
        target_id=str(station_id),
        ip_address=source,
        detail={
            "hardware": body.hardware.model_dump(exclude_none=True)
            if body.hardware
            else None,
            "broker_provisioned": provisioned,
        },
    )
    if not provisioned:
        log.warning(
            "Station %s enrolled but its broker principal was not created.",
            station_id,
        )
    return response


@router.post("/renew", response_model=EnrolResponse)
def renew(
    request: Request, authorization: str | None = Header(default=None)
) -> EnrolResponse:
    """Issue a fresh credential to a station presenting a current one.

    The station is identified by the credential, not by anything it asserts.
    The previous credential keeps working for the overlap window, so losing this
    response is recoverable rather than terminal.
    """
    source = _source(request)
    if not ratelimit.check(
        f"renew:{source}", limit=RENEW_LIMIT, window_seconds=RENEW_WINDOW
    ):
        raise HTTPException(status_code=429, detail="Too many attempts")

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Credential required")
    secret = authorization.split(" ", 1)[1].strip()

    with PrivilegedSessionLocal() as db:
        found = enrolment.authenticate(db, secret=secret)
        if found is None:
            db.rollback()
            record(
                action="station.renew.rejected",
                ip_address=source,
                target_type="station_credential",
                detail={"reason": "invalid-credential"},
            )
            raise HTTPException(status_code=401, detail="Credential not valid")

        station, current = found
        issued = enrolment.renew(db, station=station, current=current)
        # Recomputed from the database, which already knows the outgoing
        # credential stays valid for the overlap window - so the broker ends up
        # accepting exactly the pair that should work, and nothing older.
        db.flush()
        provisioned = broker_acl.sync_station(db, station.id)
        issued.credential.broker_provisioned = provisioned
        station_id = station.id
        organization_id = station.organization_id
        expires = issued.credential.expires_at
        response = _response(issued)
        db.commit()

    record(
        action="station.credential.renewed",
        organization_id=organization_id,
        ground_station_id=station_id,
        target_type="ground_station",
        target_id=str(station_id),
        ip_address=source,
        detail={
            "expires_at": expires.isoformat(),
            "overlap_hours": enrolment.RENEWAL_OVERLAP.total_seconds() / 3600,
            "broker_provisioned": provisioned,
        },
    )
    return response


class StatusResponse(BaseModel):
    """What a station can learn about itself. Deliberately thin - it is for a
    box confirming it is still trusted, not an API surface."""

    station_id: str
    name: str
    config_version: int
    credential_expires_at: str
    renew_now: bool
    server_time: str


@router.get("/status", response_model=StatusResponse)
def status(authorization: str | None = Header(default=None)) -> StatusResponse:
    """Let a station check its standing, and learn the platform's clock.

    `server_time` is here for a specific failure: a box with no battery-backed
    clock cannot evaluate its own credential expiry, and a station that wrongly
    believes it has expired behaves as badly as one that wrongly believes it has
    not. It is a reference, not an authority - the contract is explicit that the
    platform must never be a station's only clock.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Credential required")
    secret = authorization.split(" ", 1)[1].strip()

    with PrivilegedSessionLocal() as db:
        found = enrolment.authenticate(db, secret=secret)
        if found is None:
            raise HTTPException(status_code=401, detail="Credential not valid")
        station, credential = found
        now = datetime.now(UTC)
        result = StatusResponse(
            station_id=str(station.id),
            name=station.name,
            config_version=station.config_version,
            credential_expires_at=credential.expires_at.isoformat(),
            renew_now=(credential.expires_at - now) < enrolment.RENEW_AFTER,
            server_time=now.isoformat(),
        )
        db.commit()
    return result
