"""Drive real WebSocket connections against the running server.

    docker compose exec app python -m backend.scripts.verify_realtime

Uses FastAPI's TestClient so it exercises the actual endpoint, hub and group
registry in-process - the same code paths a browser hits, with no HTTP server
needed. Seeds two orgs, connects as several users, and asserts the isolation
properties the design claims.

The cases that matter most are 5 and 6: a second org's user must not receive a
frame published to the first org's group, and two tabs belonging to one user
must be able to hold different stations at once.
"""

import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.auth.capabilities import Capability
from backend.auth.cookies import ACCESS_COOKIE_NAME
from backend.auth.security import create_access_token
from backend.database.models.enums import UserRole
from backend.database.session import PrivilegedSessionLocal
from backend.main import app
from backend.realtime.groups import station_group
from backend.realtime.hub import hub

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("verify_rt")

ORG_A, ORG_B = uuid.uuid4(), uuid.uuid4()
ST_A1, ST_A2, ST_B1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
U_A, U_B, U_LIMITED = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
SESS: dict[uuid.UUID, uuid.UUID] = {}

_failures: list[str] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    if passed:
        log.info("  PASS  %s", label)
    else:
        _failures.append(label)
        log.error("  FAIL  %s%s", label, f"  ({detail})" if detail else "")


def seed() -> None:
    now = datetime.now(UTC)
    with PrivilegedSessionLocal() as db:
        for org, n in ((ORG_A, "rt-A"), (ORG_B, "rt-B")):
            db.execute(
                text("INSERT INTO organizations (id,created_at,updated_at,name,"
                     "mfa_required,is_active) VALUES (:i,:t,:t,:n,false,true)"),
                {"i": org, "t": now, "n": f"{n}-{org.hex[:8]}"},
            )
        for uid, label, org, roles in (
            (U_A, "a-admin", ORG_A, [UserRole.ADMIN.value]),
            (U_B, "b-admin", ORG_B, [UserRole.ADMIN.value]),
            (U_LIMITED, "a-limited", ORG_A, [UserRole.OPERATOR.value]),
        ):
            db.execute(
                text("INSERT INTO users (id,created_at,updated_at,email,"
                     "display_name,is_active,mfa_enabled) "
                     "VALUES (:i,:t,:t,:e,:d,true,false)"),
                {"i": uid, "t": now, "e": f"{uid.hex[:8]}@rt.test", "d": label},
            )
            db.execute(
                text("INSERT INTO organization_memberships (id,created_at,"
                     "updated_at,user_id,organization_id,roles) "
                     "VALUES (:i,:t,:t,:u,:o,:r)"),
                {"i": uuid.uuid4(), "t": now, "u": uid, "o": org, "r": roles},
            )
            sid = uuid.uuid4()
            SESS[uid] = sid
            db.execute(
                text("INSERT INTO auth_sessions (id,created_at,updated_at,"
                     "user_id,organization_id,expires_at) "
                     "VALUES (:i,:t,:t,:u,:o,:e)"),
                {"i": sid, "t": now, "u": uid, "o": org,
                 "e": now + timedelta(hours=6)},
            )
        for st, org, name in ((ST_A1, ORG_A, "A1"), (ST_A2, ORG_A, "A2"),
                              (ST_B1, ORG_B, "B1")):
            db.execute(
                text("INSERT INTO ground_stations (id,created_at,updated_at,"
                     "organization_id,name,timezone,is_active) "
                     "VALUES (:i,:t,:t,:o,:n,'UTC',true)"),
                {"i": st, "t": now, "o": org, "n": name},
            )
        # Limited user: station.view + telemetry on A1 only. No video, no audio.
        db.execute(
            text("INSERT INTO station_grants (id,created_at,updated_at,"
                 "organization_id,user_id,ground_station_id,capabilities) "
                 "VALUES (:i,:t,:t,:o,:u,:s,:c)"),
            {"i": uuid.uuid4(), "t": now, "o": ORG_A, "u": U_LIMITED, "s": ST_A1,
             "c": [Capability.STATION_VIEW.value, Capability.TELEMETRY_VIEW.value]},
        )
        db.commit()


def cleanup() -> None:
    with PrivilegedSessionLocal() as db:
        for tbl in ("station_grants", "ground_stations", "organization_memberships"):
            db.execute(text(f"DELETE FROM {tbl} WHERE organization_id IN (:a,:b)"),
                       {"a": ORG_A, "b": ORG_B})
        db.execute(text("DELETE FROM auth_sessions WHERE user_id IN (:a,:b,:c)"),
                   {"a": U_A, "b": U_B, "c": U_LIMITED})
        db.execute(text("DELETE FROM users WHERE id IN (:a,:b,:c)"),
                   {"a": U_A, "b": U_B, "c": U_LIMITED})
        db.execute(text("DELETE FROM organizations WHERE id IN (:a,:b)"),
                   {"a": ORG_A, "b": ORG_B})
        db.commit()


def token_for(user_id: uuid.UUID, org: uuid.UUID) -> str:
    return create_access_token(
        user_id=user_id, organization_id=org, session_id=SESS[user_id]
    )


def connect(client: TestClient, user_id: uuid.UUID, org: uuid.UUID):
    client.cookies.set(ACCESS_COOKIE_NAME, token_for(user_id, org))
    return client.websocket_connect("/ws")


def publish_local(org, station, stream, payload) -> int:
    """Local delivery only.

    These cases are about who is authorised into a group, which is a
    per-worker property. The cross-worker path has its own suite
    (verify_bus) - mixing the two here would make a failure ambiguous
    between an authorisation bug and a Redis round-trip that had not
    landed yet.
    """
    return hub.deliver_local(
        station_group(org, station, stream),
        hub.station_message(station, stream, payload),
    )


def drain_until(ws, kind: str, limit: int = 10) -> dict | None:
    for _ in range(limit):
        msg = ws.receive_json()
        if msg.get("type") == kind:
            return msg
    return None


def main() -> int:
    seed()
    client = TestClient(app)
    try:
        log.info("\n1. Authentication")
        client.cookies.clear()
        with client.websocket_connect("/ws") as ws:
            try:
                ws.receive_json()
                check("unauthenticated connection is refused", False, "got a frame")
            except Exception:
                check("unauthenticated connection is refused", True)

        log.info("\n2. Hello and visible stations")
        with connect(client, U_A, ORG_A) as ws:
            hello = drain_until(ws, "hello")
            check("hello names the pinned org", hello
                  and hello["organization_id"] == str(ORG_A))
            check("admin sees both of its org's stations",
                  hello and set(hello["stations"]) == {str(ST_A1), str(ST_A2)},
                  f"got {hello and hello['stations']}")

        log.info("\n3. Station selection")
        with connect(client, U_A, ORG_A) as ws:
            drain_until(ws, "hello")
            ws.send_json({"type": "select_station",
                          "ground_station_id": str(ST_B1)})
            err = drain_until(ws, "error")
            check("cannot select another org's station",
                  err and err["code"] == "not_available")

            ws.send_json({"type": "select_station",
                          "ground_station_id": str(ST_A1)})
            sel = drain_until(ws, "station_selected")
            check("can select its own station", sel is not None)

        log.info("\n4. Capability-gated subscription")
        with connect(client, U_LIMITED, ORG_A) as ws:
            drain_until(ws, "hello")
            ws.send_json({"type": "select_station",
                          "ground_station_id": str(ST_A1)})
            sel = drain_until(ws, "station_selected")
            check("limited user gets only its granted capabilities",
                  sel and set(sel["capabilities"]) ==
                  {Capability.STATION_VIEW.value, Capability.TELEMETRY_VIEW.value},
                  f"got {sel and sel['capabilities']}")

            ws.send_json({"type": "subscribe", "stream": "telemetry"})
            check("granted stream is accepted",
                  drain_until(ws, "subscribed") is not None)

            ws.send_json({"type": "subscribe", "stream": "video"})
            err = drain_until(ws, "error")
            check("ungranted stream is refused",
                  err and err["code"] == "not_permitted")

            ws.send_json({"type": "subscribe", "stream": "wiretap"})
            err = drain_until(ws, "error")
            check("unknown stream is refused", err and err["code"] == "not_permitted")

        log.info("\n5. Cross-org fan-out isolation")
        with connect(client, U_A, ORG_A) as ws_a, connect(client, U_B, ORG_B) as ws_b:
            drain_until(ws_a, "hello")
            drain_until(ws_b, "hello")
            ws_a.send_json({"type": "select_station",
                            "ground_station_id": str(ST_A1)})
            drain_until(ws_a, "station_selected")
            ws_a.send_json({"type": "subscribe", "stream": "telemetry"})
            drain_until(ws_a, "subscribed")

            ws_b.send_json({"type": "select_station",
                            "ground_station_id": str(ST_B1)})
            drain_until(ws_b, "station_selected")
            ws_b.send_json({"type": "subscribe", "stream": "telemetry"})
            drain_until(ws_b, "subscribed")

            delivered = publish_local(ORG_A, ST_A1, "telemetry",
                                            {"secret": "org-A-only"})
            check("publish reaches exactly the one subscriber", delivered == 1,
                  f"delivered to {delivered}")

            got_a = drain_until(ws_a, "event")
            check("org A receives its own event",
                  got_a and got_a["payload"]["secret"] == "org-A-only")

            ws_b.send_json({"type": "ping"})
            reply = ws_b.receive_json()
            check("org B received no cross-org frame", reply.get("type") == "pong",
                  f"got {reply.get('type')} - CROSS-TENANT LEAK")

        log.info("\n6. One station per connection, several tabs per user")
        with connect(client, U_A, ORG_A) as tab1, connect(client, U_A, ORG_A) as tab2:
            drain_until(tab1, "hello")
            drain_until(tab2, "hello")
            tab1.send_json({"type": "select_station",
                            "ground_station_id": str(ST_A1)})
            drain_until(tab1, "station_selected")
            tab1.send_json({"type": "subscribe", "stream": "telemetry"})
            drain_until(tab1, "subscribed")

            tab2.send_json({"type": "select_station",
                            "ground_station_id": str(ST_A2)})
            drain_until(tab2, "station_selected")
            tab2.send_json({"type": "subscribe", "stream": "telemetry"})
            drain_until(tab2, "subscribed")

            check("tab on station A1 still receives A1",
                  publish_local(ORG_A, ST_A1, "telemetry", {"n": 1}) == 1)
            check("tab on station A2 receives A2 independently",
                  publish_local(ORG_A, ST_A2, "telemetry", {"n": 2}) == 1)

            log.info("\n7. Switching station drops the previous subscriptions")
            tab1.send_json({"type": "select_station",
                            "ground_station_id": str(ST_A2)})
            drain_until(tab1, "station_selected")
            check("old station's group is empty after switching",
                  publish_local(ORG_A, ST_A1, "telemetry", {"n": 3}) == 0,
                  "still subscribed to the station it left")
            check("new station is not auto-subscribed",
                  publish_local(ORG_A, ST_A2, "telemetry", {"n": 4}) == 1,
                  "switching should not inherit subscriptions")

        log.info("\n8. Cleanup on disconnect")
        check("no connections remain registered", hub.connection_count() == 0,
              f"{hub.connection_count()} left")
    finally:
        cleanup()

    log.info("")
    if _failures:
        log.error("FAILED: %d check(s): %s", len(_failures), _failures)
        return 1
    log.info("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
