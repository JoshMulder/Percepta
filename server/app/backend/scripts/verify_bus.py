"""Prove the cross-worker path: two hubs, one Redis, real pub/sub.

    docker compose exec app python -m backend.scripts.verify_bus

verify_realtime covers authorisation within a single worker. This covers what
happens between workers, which is where the interesting failure modes are: a
frame reaching a worker that should never have received it, a frame not reaching
a worker that should, and a revocation that fails to arrive.

Two Hub instances stand in for two uvicorn workers. They share the database and
the Redis instance, exactly as real workers would. Connections use a stub socket
because nothing here touches the wire - delivery is observed on the connection's
send queue, which is precisely where fan-out ends.
"""

import asyncio
import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from backend.auth.capabilities import Capability
from backend.auth.identity import Identity
from backend.database.models.enums import UserRole
from backend.database.session import PrivilegedSessionLocal
from backend.realtime.connection import Connection
from backend.realtime.groups import station_group
from backend.realtime.hub import Hub
from backend.realtime.revocation import (
    grants_changed,
    revoke_session,
    station_changed,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("verify_bus")

ORG_A, ORG_B = uuid.uuid4(), uuid.uuid4()
ST_A1, ST_B1 = uuid.uuid4(), uuid.uuid4()
U_A, U_B = uuid.uuid4(), uuid.uuid4()
SESS = {U_A: uuid.uuid4(), U_B: uuid.uuid4()}

# Redis round trips are sub-millisecond on a loopback connection; this is
# generous so a slow container does not produce a false failure.
SETTLE = 0.5

_failures: list[str] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    if passed:
        log.info("  PASS  %s", label)
    else:
        _failures.append(label)
        log.error("  FAIL  %s%s", label, f"  ({detail})" if detail else "")


class StubSocket:
    """Stands in for a WebSocket. Fan-out ends at the send queue, so nothing
    below that needs to exist for these cases."""

    client_state = None

    async def send_json(self, _message: dict) -> None:  # pragma: no cover
        pass


def seed() -> None:
    now = datetime.now(UTC)
    with PrivilegedSessionLocal() as db:
        for org, n in ((ORG_A, "bus-A"), (ORG_B, "bus-B")):
            db.execute(
                text("INSERT INTO organizations (id,created_at,updated_at,name,"
                     "mfa_required,is_active) VALUES (:i,:t,:t,:n,false,true)"),
                {"i": org, "t": now, "n": f"{n}-{org.hex[:8]}"},
            )
        for uid, org in ((U_A, ORG_A), (U_B, ORG_B)):
            db.execute(
                text("INSERT INTO users (id,created_at,updated_at,email,"
                     "display_name,is_active,mfa_enabled) "
                     "VALUES (:i,:t,:t,:e,:d,true,false)"),
                {"i": uid, "t": now, "e": f"{uid.hex[:8]}@bus.test", "d": "u"},
            )
            db.execute(
                text("INSERT INTO organization_memberships (id,created_at,"
                     "updated_at,user_id,organization_id,roles) "
                     "VALUES (:i,:t,:t,:u,:o,:r)"),
                {"i": uuid.uuid4(), "t": now, "u": uid, "o": org,
                 "r": [UserRole.ADMIN.value]},
            )
            db.execute(
                text("INSERT INTO auth_sessions (id,created_at,updated_at,"
                     "user_id,organization_id,expires_at) "
                     "VALUES (:i,:t,:t,:u,:o,:e)"),
                {"i": SESS[uid], "t": now, "u": uid, "o": org,
                 "e": now + timedelta(hours=6)},
            )
        for st, org, name in ((ST_A1, ORG_A, "A1"), (ST_B1, ORG_B, "B1")):
            db.execute(
                text("INSERT INTO ground_stations (id,created_at,updated_at,"
                     "organization_id,name,timezone,is_active) "
                     "VALUES (:i,:t,:t,:o,:n,'UTC',true)"),
                {"i": st, "t": now, "o": org, "n": name},
            )
        db.commit()


def cleanup() -> None:
    with PrivilegedSessionLocal() as db:
        for tbl in ("station_grants", "ground_stations", "organization_memberships"):
            db.execute(text(f"DELETE FROM {tbl} WHERE organization_id IN (:a,:b)"),
                       {"a": ORG_A, "b": ORG_B})
        db.execute(text("DELETE FROM auth_sessions WHERE user_id IN (:a,:b)"),
                   {"a": U_A, "b": U_B})
        db.execute(text("DELETE FROM users WHERE id IN (:a,:b)"),
                   {"a": U_A, "b": U_B})
        db.execute(text("DELETE FROM organizations WHERE id IN (:a,:b)"),
                   {"a": ORG_A, "b": ORG_B})
        db.commit()


def make_connection(user_id: uuid.UUID, org: uuid.UUID) -> Connection:
    identity = Identity(
        user_id=user_id,
        organization_id=org,
        session_id=SESS[user_id],
        roles=(UserRole.ADMIN.value,),
        is_platform_admin=False,
    )
    return Connection(ws=StubSocket(), identity=identity)


def drain(conn: Connection) -> list[dict]:
    out = []
    while not conn.send_queue.empty():
        out.append(conn.send_queue.get_nowait())
    return out


async def run() -> None:
    worker1, worker2 = Hub(), Hub()
    await worker1.start()
    await worker2.start()

    check("both workers connected to the bus",
          worker1.bus is not None and worker2.bus is not None,
          "bus disabled or Redis unreachable")
    if worker1.bus is None or worker2.bus is None:
        return

    try:
        # A connection on each worker, both in org A on the same station.
        conn1 = make_connection(U_A, ORG_A)
        conn2 = make_connection(U_A, ORG_A)
        # A third in org B, to prove the negative.
        conn_b = make_connection(U_B, ORG_B)

        await worker1.register(conn1)
        await worker2.register(conn2)
        await worker2.register(conn_b)

        await worker1.select_station(conn1, ST_A1)
        await worker2.select_station(conn2, ST_A1)
        await worker2.select_station(conn_b, ST_B1)

        await worker1.subscribe(conn1, "telemetry")
        await worker2.subscribe(conn2, "telemetry")
        await worker2.subscribe(conn_b, "telemetry")
        await asyncio.sleep(SETTLE)
        for c in (conn1, conn2, conn_b):
            drain(c)

        log.info("\n1. Cross-worker fan-out")
        await worker1.publish_station(ORG_A, ST_A1, "telemetry", {"v": 42})
        await asyncio.sleep(SETTLE)

        got1, got2, gotb = drain(conn1), drain(conn2), drain(conn_b)
        check("publisher's own worker delivers exactly once",
              len([m for m in got1 if m.get("type") == "event"]) == 1,
              f"got {len(got1)} frames - a double-delivery would show here")
        check("the other worker receives it too",
              any(m.get("type") == "event" and m["payload"]["v"] == 42
                  for m in got2),
              "cross-worker fan-out did not arrive")
        check("the other org's worker connection receives nothing",
              not any(m.get("type") == "event" for m in gotb),
              "CROSS-TENANT LEAK")

        log.info("\n2. Channel scoping")
        # worker1 holds no org B subscriber, so it must not even be subscribed
        # to org B's channel - the data should never reach that process.
        b_channel = f"rt:g:{station_group(ORG_B, ST_B1, 'telemetry')}"
        check("a worker is not subscribed to a group it has no member of",
              b_channel not in worker1.bus._subscribed,
              f"worker1 subscribed to {b_channel}")
        check("a worker is subscribed to a group it does serve",
              f"rt:g:{station_group(ORG_A, ST_A1, 'telemetry')}"
              in worker1.bus._subscribed)

        log.info("\n3. Revocation push - grants changed")
        with PrivilegedSessionLocal() as db:
            db.execute(
                text("UPDATE ground_stations SET is_active = false WHERE id = :i"),
                {"i": ST_A1},
            )
            db.commit()
        station_changed(ST_A1)
        await asyncio.sleep(SETTLE)

        check("deactivating a station unpins it on every worker",
              conn1.station_id is None and conn2.station_id is None,
              f"w1={conn1.station_id} w2={conn2.station_id}")
        check("its group is emptied so nothing more is delivered",
              worker1.deliver_local(
                  station_group(ORG_A, ST_A1, "telemetry"),
                  worker1.station_message(ST_A1, "telemetry", {"v": 1}),
              ) == 0)
        check("the other org is untouched", conn_b.station_id == ST_B1)

        log.info("\n4. Revocation push - session ended")
        drain(conn_b)
        revoke_session(SESS[U_B])
        await asyncio.sleep(SETTLE)
        check("the ended session's connection is closed", conn_b.closed)
        check("it was told why",
              any(m.get("type") == "revoked" for m in drain(conn_b)))
        check("connections on other sessions are unaffected",
              not conn1.closed and not conn2.closed)

        log.info("\n5. Unrelated revocation is a no-op")
        grants_changed(uuid.uuid4())
        await asyncio.sleep(SETTLE)
        check("an event for an unknown user closes nothing",
              not conn1.closed and not conn2.closed)
    finally:
        await worker1.stop()
        await worker2.stop()


def main() -> int:
    seed()
    try:
        asyncio.run(run())
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
