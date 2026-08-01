"""Exercise the enrolment lifecycle end to end and report what it actually did.

    docker compose exec app python -m backend.scripts.verify_enrolment

Checks the properties `contract/enrolment.md` promises, against a running stack,
rather than asserting them in a docstring. Read-write: it enrols and revokes a
throwaway station of its own and cleans up after itself, so it is safe to run
against a development stack but not against production.
"""

import asyncio
import json
import logging
import pathlib
import ssl
import sys
import time
import uuid

import httpx
import redis
import websockets
from sqlalchemy import select

from backend.core.config import settings
from backend.database.models.ground_station import GroundStation
from backend.database.models.organization import Organization
from backend.database.session import PrivilegedSessionLocal
from backend.realtime.bus import url_without_credentials
from backend.services import broker_acl, enrolment

logging.basicConfig(level=logging.WARNING, format="%(message)s")

BASE = settings.simulator_enrol_url
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'' if ok else f'  ({detail})'}")
    if not ok:
        failures.append(label)


async def main() -> int:
    with PrivilegedSessionLocal() as db:
        template = db.execute(
            select(GroundStation).where(GroundStation.is_active.is_(True))
        ).scalars().first()
        if template is None:
            print("No stations. Run seed_dev first.")
            return 1
        station = GroundStation(
            organization_id=template.organization_id,
            name="Enrolment verification (temporary)",
            timezone="UTC",
            latitude=-43.5,
            longitude=172.6,
        )
        db.add(station)
        db.commit()
        station_id = station.id
        organization_id = station.organization_id

    # Verified against the platform's own CA rather than with verification
    # turned off. This script exists to prove the enrolment path behaves; a
    # version of it that skipped TLS verification would be proving something
    # slightly different from what a real station does.
    client = httpx.AsyncClient(
        timeout=15.0,
        verify=settings.tls_ca_file
        if pathlib.Path(settings.tls_ca_file).exists()
        else True,
    )
    try:
        print(f"\nVerifying enrolment against {BASE}\n")

        print("1. Claiming")
        with PrivilegedSessionLocal() as db:
            row = db.get(GroundStation, station_id)
            _, token = enrolment.issue_token(
                db, station=row, issued_by_user_id=None
            )
            db.commit()

        check(
            "token is readable aloud",
            len(token) == 14 and token.count("-") == 2,
            f"got {token!r}",
        )

        bad = await client.post(f"{BASE}/api/enrol", json={"token": "ZZZZ-ZZZZ-ZZZZ"})
        check("unknown token is refused", bad.status_code == 404, str(bad.status_code))

        response = await client.post(
            f"{BASE}/api/enrol",
            json={"token": token.lower().replace("-", " "),
                  "hardware": {"model": "verify", "serial": "0001"}},
        )
        # Deliberately mangled above - lowercased, dashes replaced with spaces.
        # If normalisation were broken this claim would 404.
        check(
            "claim succeeds, even typed casually",
            response.status_code == 200,
            response.text[:120],
        )
        if response.status_code != 200:
            return 1
        enrolled = response.json()
        secret = enrolled["credential"]["secret"]

        check(
            "station id comes from the platform, not the box",
            enrolled["station_id"] == str(station_id),
            enrolled["station_id"],
        )
        check(
            "response names the station's own channels",
            enrolled["broker"]["telemetry_topic"] == f"gsu/{station_id}/telemetry"
            and enrolled["broker"]["command_topic"] == f"cmd/gsu/{station_id}",
            str(enrolled["broker"]),
        )
        # The station is told its own tenant's name and nothing else about the
        # platform's tenancy. It is echoed back so whoever is standing at the
        # box can see which organisation the code they pasted enrolled it into
        # — the mistake being a contractor commissioning into the previous
        # customer's org and nobody noticing until data lands in the wrong
        # console. What must not appear is any *other* tenant.
        with PrivilegedSessionLocal() as db:
            names = db.execute(select(Organization.id, Organization.name)).all()
        own = next((n for i, n in names if i == organization_id), None)
        others = [n for i, n in names if i != organization_id and n]
        check(
            "the station is told which organisation it joined",
            enrolled["station"]["organization"] == own,
            f"{enrolled['station']['organization']!r}, expected {own!r}",
        )
        leaked = [n for n in others if n.lower() in str(enrolled).lower()]
        check(
            "and is told nothing about any other tenant",
            not leaked,
            f"response mentions {leaked}",
        )

        print("\n2. Broker principal")
        acl = redis.Redis.from_url(settings.redis_url)
        try:
            user = acl.execute_command(
                "ACL", "GETUSER", broker_acl.principal(station_id)
            )
            check("principal exists", user is not None)
        except Exception as exc:
            check("principal exists", False, str(exc))

        # Authenticate as the station and prove the pin holds.
        try:
            # Credential-free URL: redis-py lets the URL override these
            # kwargs, so the platform's own password would be used instead of
            # the station's. See bus.url_without_credentials.
            as_station = redis.Redis.from_url(
                url_without_credentials(settings.redis_url),
                username=broker_acl.principal(station_id),
                password=secret,
                ssl_ca_certs=settings.tls_ca_file
                if settings.redis_url.startswith("rediss://")
                else None,
            )
            as_station.publish(f"gsu/{station_id}/telemetry", "{}")
            check("station may publish on its own channel", True)
        except Exception as exc:
            check("station may publish on its own channel", False, str(exc))

        other = uuid.uuid4()
        try:
            as_station.publish(f"gsu/{other}/telemetry", "{}")
            check(
                "station may NOT publish as another station", False,
                "the broker allowed it",
            )
        except redis.exceptions.NoPermissionError:
            check("station may NOT publish as another station", True)
        except Exception as exc:
            check("station may NOT publish as another station", False, str(exc))

        try:
            as_station.set("anything", "1")
            check("station may NOT touch the keyspace", False, "the broker allowed it")
        except redis.exceptions.NoPermissionError:
            check("station may NOT touch the keyspace", True)
        except Exception as exc:
            check("station may NOT touch the keyspace", False, str(exc))

        print("\n3. Re-claiming and rejection")
        with PrivilegedSessionLocal() as db:
            row = db.get(GroundStation, station_id)
            _, second = enrolment.issue_token(db, station=row, issued_by_user_id=None)
            db.commit()
        conflict = await client.post(f"{BASE}/api/enrol", json={"token": second})
        check(
            "a fresh token against an enrolled station is refused",
            conflict.status_code == 409,
            str(conflict.status_code),
        )

        print("\n4. Renewal and overlap")
        status = await client.get(
            f"{BASE}/api/enrol/status", headers={"Authorization": f"Bearer {secret}"}
        )
        check("status accepts the credential", status.status_code == 200,
              str(status.status_code))
        check(
            "status carries a server clock",
            "server_time" in status.json() if status.status_code == 200 else False,
        )

        renewed = await client.post(
            f"{BASE}/api/enrol/renew", headers={"Authorization": f"Bearer {secret}"}
        )
        check("renew succeeds", renewed.status_code == 200, renewed.text[:120])
        new_secret = renewed.json()["credential"]["secret"] if renewed.status_code == 200 else ""
        check("renewal issues a different secret", new_secret and new_secret != secret)

        still = await client.get(
            f"{BASE}/api/enrol/status", headers={"Authorization": f"Bearer {secret}"}
        )
        check(
            "the old credential still works during the overlap",
            still.status_code == 200,
            str(still.status_code),
        )
        fresh = await client.get(
            f"{BASE}/api/enrol/status",
            headers={"Authorization": f"Bearer {new_secret}"},
        )
        check("the new credential works", fresh.status_code == 200, str(fresh.status_code))

        junk = await client.get(
            f"{BASE}/api/enrol/status", headers={"Authorization": "Bearer nonsense"}
        )
        check("a bogus credential is refused", junk.status_code == 401,
              str(junk.status_code))

        print("\n5. Revocation")
        # The case the contract is explicit about, and the one that is easy to
        # get wrong: `contract/transport.md` promises revoking a credential
        # stops a station's data within about thirty seconds "whether or not
        # the broker noticed". The relay authenticates once, when the socket
        # opens, so without a re-check a revoked station keeps publishing until
        # something else drops its connection — which on a healthy link is
        # never. Opened here while the credential is still good, so that a
        # socket which closes has been closed *by the revocation*.
        relay = await websockets.connect(
            BASE.replace("https://", "wss://").replace("http://", "ws://") + "/broker",
            additional_headers={"Authorization": f"Bearer {new_secret}"},
            ssl=ssl.create_default_context(cafile=settings.tls_ca_file)
            if BASE.startswith("https") and pathlib.Path(settings.tls_ca_file).exists()
            else None,
        )
        # Proving it works first is what stops this being vacuous: a socket
        # that never carried anything would also "close on revocation".
        listener = redis.Redis.from_url(settings.redis_url).pubsub()
        listener.subscribe(f"gsu/{station_id}/telemetry")
        await relay.send(json.dumps({
            "topic": f"gsu/{station_id}/telemetry",
            "payload": {"kind": "power", "available": False},
        }))
        # Looped rather than a single `get_message`: the first message on a
        # fresh pubsub is the subscribe confirmation, and `get_message` with
        # `ignore_subscribe_messages` returns None for it rather than reading
        # past it — so the one-shot form reports "nothing arrived" every time.
        def _await_frame(deadline: float = 5.0):
            end = time.monotonic() + deadline
            while time.monotonic() < end:
                message = listener.get_message(
                    ignore_subscribe_messages=True, timeout=end - time.monotonic()
                )
                if message is not None:
                    return message
            return None

        arrived = await asyncio.to_thread(_await_frame)
        check("a live station publishes through the relay", arrived is not None,
              "nothing reached Redis")

        with PrivilegedSessionLocal() as db:
            enrolment.revoke_credentials(
                db, station_id=station_id, reason="verification"
            )
            db.commit()
        broker_acl.deprovision(station_id)

        # Nothing above reaches the relay socket: `deprovision` kills Redis
        # clients authenticated as the station's principal, and this station
        # never was one — it came in over 443. If this closes, the watcher
        # closed it.
        closed = True
        try:
            await asyncio.wait_for(relay.wait_closed(), timeout=30.0)
        except TimeoutError:
            closed = False
            await relay.close()
        check("revocation closes a socket that is already open", closed,
              "still open 30s after revocation")
        listener.close()

        after = await client.get(
            f"{BASE}/api/enrol/status",
            headers={"Authorization": f"Bearer {new_secret}"},
        )
        check("a revoked credential stops working", after.status_code == 401,
              str(after.status_code))
        check(
            "the broker principal is gone",
            not broker_acl.exists(station_id),
        )
        with PrivilegedSessionLocal() as db:
            check(
                "the ingest would now drop this station",
                not enrolment.has_valid_credential(db, station_id=station_id),
            )

        return 1 if failures else 0
    finally:
        await client.aclose()
        broker_acl.deprovision(station_id)
        with PrivilegedSessionLocal() as db:
            row = db.get(GroundStation, station_id)
            if row is not None:
                db.delete(row)
                db.commit()


if __name__ == "__main__":
    code = asyncio.run(main())
    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
    else:
        print("All checks passed - enrolment behaves as the contract specifies.")
    sys.exit(code)
