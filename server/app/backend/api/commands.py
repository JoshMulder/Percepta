"""Operator commands to ground station hardware.

Every command here has a physical effect at a remote, unattended site, so each
one is capability-checked, audited with the identity that issued it, and then
published to the station rather than applied locally. The API never pretends a
command succeeded: the station reports its own state back on the telemetry
stream, and that is what the console renders.

Commands go out on a per-station Redis channel. The onboard computer is the only
subscriber; the console has no route to it (topology rule 8).

There is deliberately no exclusive lease. Tuning contends for one piece of
hardware across every viewer of a station, so an earlier design gated it behind a
holder with a timeout; that was dropped as over-engineering for how the radio is
actually used, since a station sits on one frequency almost all the time. Anyone
with radio.control can tune whenever they like, and the audit log is what makes
"who moved it" answerable after the fact.

Transmit will be different when it arrives - two transmitters keying the same
channel is not a UX problem - but it is ungrantable until then.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth.capabilities import Capability
from backend.auth.dependencies import require_capability
from backend.auth.identity import Identity
from backend.database.dependencies import get_db
from backend.realtime.bus import command_channel, publish_sync
from backend.services.audit import record

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/stations/{station_id}", tags=["commands"])

# Airband, matching the receiver's own limits (Remote-Radio's MIN_FREQ/MAX_FREQ).
MIN_HZ = 108_000_000
MAX_HZ = 137_000_000


class TuneRequest(BaseModel):
    freq_hz: int = Field(ge=MIN_HZ, le=MAX_HZ)


class SquelchRequest(BaseModel):
    db: float = Field(ge=-110, le=-10)


class AutoSquelchRequest(BaseModel):
    on: bool


class GainRequest(BaseModel):
    # "auto" or a tuner gain in dB. Remote-Radio's own warning applies: auto
    # near a strong broadcast transmitter desenses the tuner, and a stronger
    # signal can then read *lower* on the meter.
    gain: str | float


class SpectrumRequest(BaseModel):
    on: bool = True


class PpmRequest(BaseModel):
    ppm: int = Field(ge=-1000, le=1000)


class MonitorRequest(BaseModel):
    on: bool


class LightRequest(BaseModel):
    on: bool


def _audit(
    *,
    request: Request,
    identity: Identity,
    station_id: uuid.UUID,
    action: str,
    detail: dict,
) -> None:
    """Append-only record of who told which hardware to do what.

    This is the record that matters if an incident is ever reviewed. See
    services/audit.py for why writing it can never block the command.
    """
    record(
        action=action,
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="ground_station",
        target_id=str(station_id),
        ground_station_id=station_id,
        ip_address=request.client.host if request.client else None,
        detail=detail,
    )


def _dispatch(station_id: uuid.UUID, command: dict) -> None:
    if not publish_sync(command_channel(station_id), command):
        # The station never received it. Saying so beats a silent no-op that
        # leaves an operator believing the floodlight is on.
        raise HTTPException(
            status_code=503, detail="Could not reach the station right now"
        )


@router.post("/radio/tune", status_code=202)
def tune(
    station_id: uuid.UUID,
    body: TuneRequest,
    request: Request,
    identity: Identity = Depends(require_capability(Capability.RADIO_CONTROL)),
    db: Session = Depends(get_db),
) -> dict:
    """Retune the receiver.

    202, not 200: the station has been told, and the frequency it actually
    reaches arrives on the telemetry stream. Tuning changes what *every*
    listener on this station hears, which is why it needs radio.control rather
    than radio.listen.
    """
    # Snap to the nearest kHz, as Remote-Radio does before sending.
    freq_hz = round(body.freq_hz / 1000) * 1000
    _dispatch(station_id, {"kind": "radio.tune", "freq_hz": freq_hz})
    _audit(
        request=request, identity=identity, station_id=station_id,
        action="radio_tune", detail={"freq_hz": freq_hz},
    )
    return {"accepted": True, "freq_hz": freq_hz}


@router.post("/radio/squelch", status_code=202)
def squelch(
    station_id: uuid.UUID,
    body: SquelchRequest,
    request: Request,
    identity: Identity = Depends(require_capability(Capability.RADIO_CONTROL)),
    db: Session = Depends(get_db),
) -> dict:
    _dispatch(station_id, {"kind": "radio.squelch", "db": body.db})
    _audit(
        request=request, identity=identity, station_id=station_id,
        action="radio_squelch", detail={"db": body.db},
    )
    return {"accepted": True}


@router.post("/radio/auto-squelch", status_code=202)
def auto_squelch(
    station_id: uuid.UUID,
    body: AutoSquelchRequest,
    request: Request,
    identity: Identity = Depends(require_capability(Capability.RADIO_CONTROL)),
    db: Session = Depends(get_db),
) -> dict:
    _dispatch(station_id, {"kind": "radio.auto_squelch", "on": body.on})
    _audit(
        request=request, identity=identity, station_id=station_id,
        action="radio_auto_squelch", detail={"on": body.on},
    )
    return {"accepted": True}


@router.post("/radio/monitor", status_code=202)
def monitor(
    station_id: uuid.UUID,
    body: MonitorRequest,
    request: Request,
    identity: Identity = Depends(require_capability(Capability.RADIO_CONTROL)),
    db: Session = Depends(get_db),
) -> dict:
    """Momentary squelch defeat - the MON button on a handheld.

    Holds the gate open so an operator can hear the channel's noise: to set an
    audio level against it, or to check a channel really is quiet rather than
    squelched shut on a weak signal.

    Note this is station-wide, like everything else about the receiver: there is
    one gate, so defeating it pushes hiss to every listener. That is true of the
    physical radio too, which is why it is a momentary control rather than a
    setting - it is expected to be held, not left on.

    Audited, unlike the other momentary controls. This used to say it was not,
    on the grounds that it changes nothing outliving the press. That was wrong
    in the one direction that matters: a held gate reports squelch_open, so
    audio flows continuously up a metered link, and nothing on this side ever
    releases it - the console holding it can close, crash or be signed out. The
    station now releases it for itself after five minutes
    (`radio/receiver.MONITOR_MAX_S`), and this row is what answers "who opened
    it" for the five minutes before that.
    """
    _dispatch(station_id, {"kind": "radio.monitor", "on": body.on})
    _audit(
        request=request, identity=identity, station_id=station_id,
        action="radio_monitor", detail={"on": body.on},
    )
    return {"accepted": True}


@router.post("/radio/gain", status_code=202)
def gain(
    station_id: uuid.UUID,
    body: GainRequest,
    request: Request,
    identity: Identity = Depends(require_capability(Capability.CONFIG_WRITE)),
    db: Session = Depends(get_db),
) -> dict:
    """RF gain.

    Behind config.write rather than radio.control: gain is set once for a site
    based on its RF environment, and getting it wrong quietly degrades every
    listener's reception rather than producing an obvious symptom. That is a
    configuration decision, not an operating one.
    """
    value = body.gain
    if isinstance(value, str) and value != "auto":
        raise HTTPException(status_code=422, detail="gain must be 'auto' or a number")
    _dispatch(station_id, {"kind": "radio.gain", "gain": value})
    _audit(
        request=request, identity=identity, station_id=station_id,
        action="radio_gain", detail={"gain": value},
    )
    return {"accepted": True}


@router.post("/radio/ppm", status_code=202)
def ppm(
    station_id: uuid.UUID,
    body: PpmRequest,
    request: Request,
    identity: Identity = Depends(require_capability(Capability.CONFIG_WRITE)),
    db: Session = Depends(get_db),
) -> dict:
    """Tuner frequency correction. Site configuration - a given dongle has a
    given crystal error, and it does not change between shifts."""
    _dispatch(station_id, {"kind": "radio.ppm", "ppm": body.ppm})
    _audit(
        request=request, identity=identity, station_id=station_id,
        action="radio_ppm", detail={"ppm": body.ppm},
    )
    return {"accepted": True}


@router.post("/radio/spectrum", status_code=202)
def spectrum(
    station_id: uuid.UUID,
    body: SpectrumRequest,
    identity: Identity = Depends(require_capability(Capability.RADIO_LISTEN)),
    db: Session = Depends(get_db),
) -> dict:
    """Ask a station to include its spectrum in radio telemetry, or stop.

    Demand-driven because the array is around 150 MB a day at the radio
    stream's rate, on a link that is metered and shared with video, for a
    display that is open for minutes at commissioning. The console re-asks
    while the page is open and simply stops when it is closed; the station's
    window lapses on its own, so a console that crashes costs nothing.

    Behind radio.listen rather than config.write: this changes what is sent to
    the person asking, not what the receiver does. Nothing about the radio is
    reconfigured and no other viewer is affected.

    Not audited. It is a subscription to a diagnostic, several times a minute
    while a settings page is open, and recording it would bury the entries that
    matter — the ones where somebody changed something.
    """
    _dispatch(station_id, {"kind": "radio.spectrum", "on": body.on})
    return {"accepted": True}


@router.post("/light", status_code=202)
def light(
    station_id: uuid.UUID,
    body: LightRequest,
    request: Request,
    identity: Identity = Depends(require_capability(Capability.LIGHT_CONTROL)),
    db: Session = Depends(get_db),
) -> dict:
    _dispatch(station_id, {"kind": "light.set", "on": body.on})
    _audit(
        request=request, identity=identity, station_id=station_id,
        action="light_set", detail={"on": body.on},
    )
    return {"accepted": True}
