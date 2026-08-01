"""What the box knows about itself after enrolment, and how it keeps it.

The credential is the station's whole identity: it decides which tenant the
platform files its data under. `contract/enrolment.md` §3 says the private half
is generated or held on the box and never leaves it, stored "in the OS keystore
or a permissions-restricted file". There is no answer yet to which compute
platform this runs on (§9.1) and therefore none to whether a hardware keystore
exists, so this is the file, with the permissions, and the seam for a keystore
is `CredentialStore` — swap the implementation, not its callers.

The whole enrolment response is persisted, not just the secret. The broker
address, the station's own name, timezone and position are all things the
station needs while the platform is unreachable, which is the case this design
is built around.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import clock


@dataclass(frozen=True)
class Credential:
    type: str
    secret: str
    expires_at: datetime
    renew_after: datetime

    def due_for_renewal(self, at: datetime | None = None) -> bool:
        # The platform states this rather than each station hardcoding half a
        # lifetime it was told (contract/enrolment.md §4).
        return (at or clock.now()) >= self.renew_after

    def expired(self, at: datetime | None = None) -> bool:
        return (at or clock.now()) >= self.expires_at

    def seconds_remaining(self, at: datetime | None = None) -> float:
        return (self.expires_at - (at or clock.now())).total_seconds()


@dataclass(frozen=True)
class Broker:
    url: str
    username: str
    telemetry_topic: str
    audio_topic: str
    command_topic: str
    #: Pinned CA for the production broker. `contract/enrolment.md` §11: not
    #: sent yet because the development broker has no TLS. Expect it; do not
    #: require it.
    ca_pem: str | None = None
    #: How to verify the broker: `"pinned"` against `ca_pem`, or `"system"`
    #: when the platform is behind a publicly trusted certificate and has said
    #: so. None means an older platform that stated neither, which `tls.py`
    #: treats as "pinned with nothing to pin" — i.e. it refuses. Defaulted here
    #: rather than required so a credential stored before this field existed
    #: still loads.
    ca_mode: str | None = None
    #: Where the live H.264 goes, if the platform names it. Derived from the
    #: API's host when it does not — see `transport/stream.py`.
    media_url: str | None = None


@dataclass(frozen=True)
class Site:
    """What the platform told this box it is, at the moment it enrolled.

    Name and position are settled at enrolment and frozen afterwards: a station
    that needs a different position has physically moved, and a box that has
    moved is recommissioned rather than edited. So this is not a snapshot to be
    kept in sync — it is the answer, and the setup page shows it read-only.
    """

    name: str
    timezone: str
    latitude: float | None
    longitude: float | None
    #: Metres. Part of the position, settled and frozen with it. The ADS-B
    #: barometric correction is computed from this and refuses without it
    #: rather than assuming sea level, which would put every corrected
    #: altitude out by the height of the site.
    elevation_m: float | None = None
    #: The tenant this box now belongs to, echoed back by the platform so the
    #: person who pasted the code can confirm they enrolled it into the right
    #: one. A code carries no visible clue whose it is.
    organization: str | None = None
    #: Where this is, in words, derived by the platform from the coordinates.
    #: Lets somebody at the site check the position they were given is the site
    #: they are standing at, which a pair of decimals does not.
    locality: str | None = None


@dataclass(frozen=True)
class Enrolment:
    station_id: str
    credential: Credential
    broker: Broker
    site: Site
    config_version: int
    enrolled_at: datetime

    @classmethod
    def from_response(cls, body: dict, at: datetime | None = None) -> Enrolment:
        credential = body["credential"]
        broker = body["broker"]
        station = body.get("station") or {}
        expires = clock.parse(credential.get("expires_at"))
        renew = clock.parse(credential.get("renew_after")) or expires
        if expires is None:
            raise ValueError("enrolment response carried no credential expiry")
        return cls(
            station_id=str(body["station_id"]),
            credential=Credential(
                type=credential.get("type", "bearer"),
                secret=credential["secret"],
                expires_at=expires,
                renew_after=renew,
            ),
            broker=Broker(
                url=broker["url"],
                username=broker["username"],
                telemetry_topic=broker["telemetry_topic"],
                audio_topic=broker["audio_topic"],
                command_topic=broker["command_topic"],
                ca_pem=broker.get("ca_pem"),
                ca_mode=broker.get("ca_mode"),
                media_url=broker.get("media_url"),
            ),
            site=Site(
                name=station.get("name") or "unnamed station",
                timezone=station.get("timezone") or "UTC",
                latitude=station.get("latitude"),
                longitude=station.get("longitude"),
                elevation_m=station.get("elevation_m"),
                organization=station.get("organization"),
                locality=station.get("locality"),
            ),
            config_version=int(body.get("config_version", 0)),
            enrolled_at=at or clock.now(),
        )

    def to_json(self) -> str:
        data = asdict(self)
        data["credential"]["expires_at"] = self.credential.expires_at.isoformat()
        data["credential"]["renew_after"] = self.credential.renew_after.isoformat()
        data["enrolled_at"] = self.enrolled_at.isoformat()
        return json.dumps(data, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> Enrolment:
        data = json.loads(raw)
        credential = data["credential"]
        return cls(
            station_id=data["station_id"],
            credential=Credential(
                type=credential["type"],
                secret=credential["secret"],
                expires_at=clock.parse(credential["expires_at"]),
                renew_after=clock.parse(credential["renew_after"]),
            ),
            broker=Broker(**data["broker"]),
            site=Site(**data["site"]),
            config_version=int(data.get("config_version", 0)),
            enrolled_at=clock.parse(data.get("enrolled_at")) or clock.now(),
        )

    def with_credential(self, other: Enrolment) -> Enrolment:
        """A renewal returns a whole response; keep the new everything but stay
        the same station. A box holding a valid secret still cannot assert which
        station it is (contract/enrolment.md §4), so a renewal that came back
        naming a different station id is a bug worth refusing loudly."""
        if other.station_id != self.station_id:
            raise ValueError(
                f"renewal returned station {other.station_id}, not {self.station_id}"
            )
        return other


class CredentialStore:
    """A 0600 file in a 0700 directory. The seam for a hardware keystore."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def mtime(self) -> float | None:
        """When the stored credential last changed, or None if there is none.

        Lets a running agent notice that it has been re-enrolled by something
        else — a technician on the console, or `gsu enrol` over SSH — rather
        than sitting with a dead secret until somebody restarts it.
        """
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def load(self) -> Enrolment | None:
        try:
            return Enrolment.from_json(self.path.read_text())
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError) as exc:
            # A corrupt credential is not recoverable by guessing, and silently
            # re-enrolling would be worse: it needs a technician with a code.
            raise ValueError(f"stored credential at {self.path} is unreadable: {exc}")

    def save(self, enrolment: Enrolment) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        tmp = self.path.with_suffix(".tmp")
        # Written 0600 from the start rather than chmod-ed afterwards: the
        # window in between is small and completely avoidable.
        handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w") as file:
            file.write(enrolment.to_json())
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
