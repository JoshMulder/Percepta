"""Getting events to the platform, which is the one thing here that is a ledger.

`contract/transport.md`, *Store and forward*. Every other channel this station
publishes is current state: a newer telemetry frame replaces the last within a
second, so the transport is explicitly allowed to drop them and does. An event
has no newer version — a transmission recorded at 03:12, a proximity alert, a
credential renewal that failed — and losing it loses the fact.

So this channel alone is acknowledged, and everything below follows from that:
one batch in flight, re-send until acknowledged, and nothing deleted until the
platform says it has it.

THE TWO RULES THAT ARE NOT OBVIOUS
----------------------------------
**An acknowledgement applies only to the batch currently awaiting one.** One
batch is in flight at a time, so there is exactly one ack this station can be
expecting; anything else is a duplicate, a straggler from a previous
connection, or from before a store rebuild. This is the rule that matters, and
it needs no extra field precisely because the one-batch discipline already
identifies the batch.

**The clamp is the weaker companion and is not sufficient alone.** Ignoring an
ack above the highest seq ever published catches the obvious stale one and
misses the dangerous one: the counter is durable and independent of the rows
(see `store.py`), so after a store rebuild a pre-rebuild `through_seq` lands
*inside* the fresh range and the clamp does not fire. The batch rule is what
protects the data.
"""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("gsu.events")

#: Batch caps, all three from the contract's timings table. A batch must
#: satisfy every one of them, and a single event over the per-event cap is
#: dropped rather than allowed to wedge the channel: it would be too large to
#: ever fit a batch, so re-sending it for ever is the alternative.
MAX_EVENTS = 100
MAX_BATCH_BYTES = 128 * 1024
MAX_EVENT_BYTES = 8 * 1024

#: How long to wait for an acknowledgement before assuming it is lost, and how
#: far the wait grows. A lost ack costs a re-send, which is harmless because
#: events carry a stable id the platform deduplicates on — so this can be
#: patient rather than eager.
ACK_WAIT_S = 30.0
MAX_BACKOFF_S = 900.0


class EventSender:
    """Delivers events, one batch at a time, until the platform has them."""

    def __init__(self, store, publish) -> None:
        #: `publish(stream, payload) -> bool`, from the transport.
        self._store = store
        self._publish = publish
        #: The highest seq in the batch in flight, or None if none is.
        self._awaiting: int | None = None
        self._sent_at = 0.0
        self._backoff = ACK_WAIT_S
        #: The highest seq this station has ever put on the wire. The clamp
        #: above compares against this rather than against what is in the
        #: store, because the store is emptied by pruning and the wire is not.
        self._high_water = 0
        self.delivered = 0
        self.dropped = 0

    # --- sending ---------------------------------------------------------

    def pump(self) -> None:
        """Send a batch if one is due. Called from the sensing loop.

        Cheap when there is nothing to do: one integer comparison in the common
        case, and a small query when a batch is genuinely ready.
        """
        now = time.monotonic()
        if self._awaiting is not None:
            if now - self._sent_at < self._backoff:
                return
            # No acknowledgement in time. The batch is rebuilt rather than
            # replayed from memory: pruning or an eviction may have changed
            # what is outstanding, and re-sending rows that no longer exist
            # would ask the platform to acknowledge a seq this station can no
            # longer account for.
            log.info("No acknowledgement for seq %d in %.0fs; re-sending.",
                     self._awaiting, self._backoff)
            self._backoff = min(self._backoff * 2, MAX_BACKOFF_S)

        batch = self._build()
        if batch is None:
            return
        events, last_seq = batch
        if not self._publish("e", {"events": events}):
            # The link is down. Nothing is marked, nothing is lost, and the
            # next pump tries again — which is the whole reason events are
            # buffered rather than published optimistically.
            return
        self._awaiting = last_seq
        self._high_water = max(self._high_water, last_seq)
        self._sent_at = now

    def _build(self) -> tuple[list[dict], int] | None:
        """The next batch, under all three caps, or None if there is nothing."""
        pending = self._store.unsent_events(limit=MAX_EVENTS)
        if not pending:
            self._awaiting = None
            self._backoff = ACK_WAIT_S
            return None

        events: list[dict] = []
        total = 0
        for event in pending:
            encoded = len(json.dumps(event, separators=(",", ":")))
            if encoded > MAX_EVENT_BYTES:
                # Too large to fit any batch, so re-sending it would wedge the
                # channel for ever and every later event behind it. Dropped
                # loudly, and counted, because a station silently discarding
                # its own history is the failure this file exists to prevent.
                log.error("Event seq %s is %d bytes, over the %d byte cap; "
                          "dropping it rather than wedging the channel.",
                          event.get("seq"), encoded, MAX_EVENT_BYTES)
                self._store.mark_sent_through(int(event["seq"]))
                self.dropped += 1
                continue
            if events and total + encoded > MAX_BATCH_BYTES:
                break
            events.append(event)
            total += encoded

        if not events:
            return None
        return events, int(events[-1]["seq"])

    # --- receiving -------------------------------------------------------

    def on_ack(self, through_seq) -> None:
        """`events.ack` arrived. Delete up to it, if it is the one we expect."""
        if not isinstance(through_seq, int) or isinstance(through_seq, bool):
            return
        if self._awaiting is None:
            log.debug("Ignoring events.ack %d: nothing is in flight.",
                      through_seq)
            return
        if through_seq > self._high_water:
            # The clamp. An ack for a seq never published cannot be honest, and
            # the ack is the one irreversible thing the platform can do here.
            log.warning("Ignoring events.ack %d: above the highest seq ever "
                        "published (%d).", through_seq, self._high_water)
            return
        if through_seq != self._awaiting:
            # Not the batch in flight — a duplicate, a straggler from a
            # previous connection, or from before a store rebuild.
            log.info("Ignoring events.ack %d: awaiting %d.",
                     through_seq, self._awaiting)
            return

        marked = self._store.mark_sent_through(through_seq)
        self.delivered += marked
        self._awaiting = None
        self._backoff = ACK_WAIT_S
        log.info("Platform acknowledged %d event(s) through seq %d.",
                 marked, through_seq)

    # --- what health telemetry reports ------------------------------------

    @property
    def pending(self) -> int:
        return self._store.unsent_count()
