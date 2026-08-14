"""The events ledger: store a batch, acknowledge it, never lose a fact twice.

`contract/transport.md`, *Store and forward*. This is the only station channel
the platform acknowledges, and the only one where losing a message loses
information rather than a second of freshness.

THE ACKNOWLEDGEMENT RULE, AND WHY IT IS NOT THE OBVIOUS ONE
-----------------------------------------------------------
`events.ack {through_seq}` means **the batch is dealt with**, not "the highest
row I stored". The difference is the whole reason this file has a comment
block, because the obvious reading deadlocks.

The contract *requires* the platform to refuse some events — a reserved
`platform.` type, a payload that fails the schema. Read `through_seq` as "the
highest I stored" and a batch whose *first* event is unstorable has no honest
acknowledgement to send: the highest stored seq falls below the batch, the
station's own rule tells it to ignore an ack below the batch it is awaiting,
and it re-sends the same batch for ever with every later event queued behind
it. One malformed event ends that site's history permanently.

Re-sending cannot help, because nothing about a second delivery makes a refused
event acceptable. So refusal is terminal, the cursor moves past it, and **the
platform records what it refused on its own side** — the fact is not lost, it
just stops being the station's problem.

Both independent clean-room builds of this platform hit that deadlock before
the rule was written down. It is not a subtle failure and it is not rare.
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.database.models.station_event import StationEvent

log = logging.getLogger(__name__)

from backend.services import alert_engine  # noqa: E402  (cycle-free at runtime)

#: Caps from `contract/transport.md`. A batch over any of them is a station
#: bug; the events are still taken, because dropping a site's history to
#: punish a formatting error is the wrong trade.
MAX_EVENTS_PER_BATCH = 100
MAX_TYPE_LENGTH = 128

#: Severities the contract defines. An unrecognised one is carried as-is rather
#: than rejected — the vocabulary is open by design so a station may be newer
#: than the platform — but it is not allowed to be absent.
SEVERITIES = frozenset({"info", "warning", "critical"})


def _is_reserved(event_type: str) -> bool:
    """Whether a station is trying to forge a platform event.

    The platform writes its own facts — credential issued, revoked, enrolment
    claimed — into the same operator-visible timeline, so nothing else may
    claim one.

    **Printable ASCII first, and that ordering is the defence.** The obvious
    implementation is NFKC + casefold + strip and then a prefix test, and it
    does not work: NFKC maps a full-width `Ｐ` to `P` but leaves Cyrillic `а`
    exactly where it is, because those are different letters rather than two
    spellings of one, and `strip()` removes whitespace, which a zero-width
    space is not. Both walked straight through a defence written that way, and
    both were named as examples in the paragraph describing it.

    An event type is a machine vocabulary. Nothing legitimate needs a character
    outside ASCII, so refusing them closes the whole class rather than the
    instances somebody thought to enumerate.
    """
    if any(ch < " " or ch > "~" for ch in event_type):
        return True
    folded = unicodedata.normalize("NFKC", event_type).casefold().strip()
    return folded.startswith("platform.")


def _parse_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive timestamp is not an error — it is a station whose clock has no
    # zone, which is exactly the box most likely to have an unsynced one. Read
    # as UTC rather than dropped, and `clock` already says how much to believe.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def accept_batch(
    db: Session,
    *,
    organization_id: uuid.UUID,
    station_id: uuid.UUID,
    payload: dict,
) -> int | None:
    """Store what is storable and return the seq to acknowledge.

    None means there was nothing here to acknowledge — an empty or malformed
    batch, which is not the same as a batch that was refused. A batch whose
    events were all rejected still returns a seq, because those events are
    dealt with and the station must stop re-sending them.
    """
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        return None
    if len(events) > MAX_EVENTS_PER_BATCH:
        # Taken anyway. The cap exists so a station does not build a batch too
        # large to be framed; a station that ignored it has a bug, and refusing
        # the batch would cost a site its history over a counter.
        log.warning("Station %s sent %d events in one batch; the cap is %d.",
                    station_id, len(events), MAX_EVENTS_PER_BATCH)

    received = datetime.now(timezone.utc)
    rows: list[dict] = []
    highest: int | None = None
    refused = 0

    for event in events:
        if not isinstance(event, dict):
            continue
        seq = event.get("seq")
        event_id = event.get("id")
        event_type = event.get("type")
        severity = event.get("severity")
        if (not isinstance(seq, int) or isinstance(seq, bool)
                or not isinstance(event_id, str) or not event_id
                or not isinstance(event_type, str) or not event_type
                or not isinstance(severity, str)):
            # Unstorable, and re-sending will not change that — so it counts
            # toward the cursor exactly as a reserved type does.
            refused += 1
            highest = seq if isinstance(seq, int) and not isinstance(seq, bool) \
                and (highest is None or seq > highest) else highest
            continue

        highest = seq if highest is None or seq > highest else highest

        if _is_reserved(event_type) or len(event_type) > MAX_TYPE_LENGTH:
            refused += 1
            log.warning("Station %s tried to publish a reserved or oversized "
                        "event type %r; refusing it.", station_id, event_type)
            continue

        at = _parse_at(event.get("at")) or received
        clock = event.get("clock")
        rows.append({
            "id": uuid.uuid4(),
            "organization_id": organization_id,
            "ground_station_id": station_id,
            "event_id": event_id[:128],
            "seq": seq,
            "at": at,
            "received_at": received,
            "clock": clock if clock in ("synced", "unsynced") else "synced",
            "type": event_type,
            "severity": severity if severity in SEVERITIES else "info",
            "message": event.get("message"),
            "data": event.get("data") if isinstance(event.get("data"), dict)
            else None,
        })

    if rows:
        # `ON CONFLICT DO NOTHING` is the at-least-once guarantee doing its
        # job. A lost acknowledgement means the station re-sends a batch it
        # already delivered, so a collision here is normal operation — the
        # alternative, checking first, races another worker and is slower.
        db.execute(
            pg_insert(StationEvent).values(rows).on_conflict_do_nothing(
                constraint="uq_station_event_id"
            )
        )

        # The alert engine sees the same batch, in the SAME transaction as the
        # rows that caused it: an alert and its evidence commit together or not
        # at all. It decides for itself what is worth raising — most of these
        # are not, and the refusals are the point (services/alerts/rules.py).
        #
        # Inside the try, and before the commit. An earlier draft put this after
        # `db.commit()`, where the comment above was simply false: the events
        # were already durable, and the alerts were left in a transaction
        # nothing ever committed.
        # A SAVEPOINT, so the two outcomes are both correct: on success the
        # alerts commit with their evidence, and on failure only the alerts are
        # discarded. A plain try/except cannot do that — rolling back the
        # session would take the events with it, and not rolling back would
        # leave a poisoned transaction that fails at commit anyway, which is the
        # same outcome dressed up.
        #
        # Alerting must never cost a station its history. The history is the
        # record; the alert is a convenience built on top of it.
        try:
            with db.begin_nested():
                alert_engine.on_events(
                    db,
                    organization_id=organization_id,
                    station_id=station_id,
                    events=[e for e in events if isinstance(e, dict)],
                )
        except Exception:  # noqa: BLE001
            log.warning(
                "Alert engine failed on a batch from %s; the events are kept.",
                station_id,
                exc_info=True,
            )

        db.commit()

    if refused:
        log.info("Station %s: %d of %d events refused and acknowledged as "
                 "dealt with; re-sending would never make them storable.",
                 station_id, refused, len(events))
    return highest
