"""Registration and type for an ADS-B contact, by its ICAO address.

A thin authenticated proxy over `services.aircraft_lookup`: the console has a
hex from the ADS-B stream and wants the tail number and model that are not in
that stream. Behind `get_identity` because it is not public, but not scoped to a
station or a capability — an aircraft is not owned by an organisation, and the
hex the console holds already came from a stream it was cleared to see.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.auth.dependencies import get_identity
from backend.auth.identity import Identity
from backend.services import aircraft_lookup

router = APIRouter(prefix="/api/aircraft", tags=["aircraft"])


class AircraftInfo(BaseModel):
    icao: str
    registration: str | None = None
    type_code: str | None = None
    model: str | None = None
    manufacturer: str | None = None
    operator: str | None = None


@router.get("/{icao}", response_model=AircraftInfo)
async def aircraft_info(
    icao: str,
    identity: Identity = Depends(get_identity),
) -> AircraftInfo:
    """The record for a hex, or a shell carrying just the hex when there is none.

    An unknown aircraft is a 200, not a 404: it is a normal outcome the card
    renders, and returning the same shape either way lets the console cache the
    "unknown" and stop asking. The lookup is itself cached, so this stays cheap
    however often a console opens the same contact.
    """
    info = await aircraft_lookup.lookup(icao)
    if info is None:
        return AircraftInfo(icao=(icao or "").strip().lower())
    return AircraftInfo(**info)
