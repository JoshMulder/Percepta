"""The local store's transcript lifecycle: clearing them by hand, and pruning
them by age. Transcripts have no sync channel, so — unlike other events — they
are pruned unconditionally by age and would otherwise grow without bound."""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gsu.store import TRANSCRIPT_KIND, LocalStore


class TranscriptStoreTests(unittest.TestCase):
    def _store(self, directory):
        store = LocalStore(Path(directory) / "store.db", Path(directory) / "rec")
        self.addCleanup(store.close)
        return store

    def _insert_dated(self, store, kind, detail, age_days):
        at = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
        with store._lock:
            store._db.execute(
                "INSERT INTO events (at, kind, severity, detail) VALUES (?, ?, ?, ?)",
                (at, kind, "info", detail),
            )
            store._db.commit()

    def test_clear_transcripts_deletes_only_transcripts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            store.record_event(TRANSCRIPT_KIND, "info", "cleared to land")
            store.record_event(TRANSCRIPT_KIND, "info", "roger")
            store.record_event("aircraft.alert", "warning", "traffic")
            removed = store.clear_transcripts()
            kinds = [event.kind for event in store.recent_events(50)]
        self.assertEqual(removed, 2)
        self.assertEqual(kinds, ["aircraft.alert"])

    def test_clearing_does_not_rewind_the_seq_counter(self):
        # Deleting rows must not let the station reuse a sequence number — the
        # counter lives in its own table. The next event's seq is still above
        # every one used before the clear.
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            store.record_event(TRANSCRIPT_KIND, "info", "one")
            store.record_event(TRANSCRIPT_KIND, "info", "two")
            store.clear_transcripts()
            store.record_event(TRANSCRIPT_KIND, "info", "three")
            seqs = [event["seq"] for event in store.unsent_events(50)]
        self.assertEqual(seqs, [3])  # not 1 — the counter did not go back

    def test_prune_removes_transcripts_older_than_the_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            self._insert_dated(store, TRANSCRIPT_KIND, "ancient", age_days=40)
            store.record_event(TRANSCRIPT_KIND, "info", "fresh")
            store.prune(audio_hours=24, audio_mb=200, event_days=30,
                        transcript_days=30)
            details = [event.detail for event in store.recent_events(50)]
        self.assertIn("fresh", details)
        self.assertNotIn("ancient", details)

    def test_prune_keeps_transcripts_when_retention_is_zero(self):
        # Zero means "keep until cleared by hand" — the age prune does nothing.
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            self._insert_dated(store, TRANSCRIPT_KIND, "ancient", age_days=400)
            store.prune(24, 200, 30, transcript_days=0)
            details = [event.detail for event in store.recent_events(50)]
        self.assertIn("ancient", details)


if __name__ == "__main__":
    unittest.main()
