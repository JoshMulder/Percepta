"""Everything the station keeps locally, because the link is not a dependency.

`station/README.md`: the box must keep sensing, recording and locally alerting
with no connectivity at all, and reconcile when it returns. That splits cleanly
into three things with three different lifetimes, and conflating them is how
stations end up replaying yesterday into a live console:

**Telemetry is not stored.** It is current state, not a ledger. A frame that
could not be sent is dropped and the next one is along in a second. This module
deliberately has no telemetry queue.

**Events are stored.** "An aircraft came within 6 km at 400 m" is a fact about
the world that stays true whether or not anyone heard it, and it is exactly what
an operator asks about after an outage. They are written here, survive a reboot,
and are marked unsynced — see `pending_events`, and the note in
CONTRACT-QUESTIONS.md, because **the contract currently has no channel to send
them on**. Nothing is invented here to fill that gap; the events accumulate,
bounded, and the reconciliation is one call away when a channel exists.

**Audio is recorded.** Squelch-open audio is written to WAV alongside being
streamed, so a transmission during an outage is not simply gone. Retention is
bounded by both age and bytes, because a remote box with a full disk is a site
visit.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import wave
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("gsu.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    severity  TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT '',
    synced_at TEXT
);
CREATE INDEX IF NOT EXISTS events_pending ON events (synced_at, id);
"""

#: A single transmission is seconds long; a segment that spans several is easier
#: to review than hundreds of fragments, and one that runs forever is a file
#: nobody can open.
MAX_SEGMENT_SECONDS = 120.0


@dataclass(frozen=True)
class Event:
    id: int
    at: datetime
    kind: str
    severity: str
    detail: str
    synced: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "at": self.at.isoformat(),
            "kind": self.kind,
            "severity": self.severity,
            "detail": self.detail,
            "synced": self.synced,
        }


class LocalStore:
    def __init__(self, db_path: Path, recordings_dir: Path) -> None:
        self.db_path = db_path
        self.recordings_dir = recordings_dir
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False: the setup page reads events on its own
        # thread. Every access is under the lock.
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

        self._wave: wave.Wave_write | None = None
        self._segment_started = 0.0
        self._segment_path: Path | None = None

    def close(self) -> None:
        self.close_segment()
        with self._lock:
            self._db.close()

    # --- events ---------------------------------------------------------

    def record_event(self, kind: str, severity: str, detail: str = "") -> int:
        at = datetime.now(UTC).isoformat()
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO events (at, kind, severity, detail) VALUES (?, ?, ?, ?)",
                (at, kind, severity, detail),
            )
            self._db.commit()
        log.info("event %s [%s] %s", kind, severity, detail)
        return int(cursor.lastrowid or 0)

    def recent_events(self, limit: int = 50) -> list[Event]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, at, kind, severity, detail, synced_at FROM events "
                "ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def pending_events(self, limit: int = 200) -> list[Event]:
        """Events not yet acknowledged by the platform.

        Nothing calls this to *send* anything today: there is no event channel
        in the contract. It exists so the reconciliation is a wiring job rather
        than a redesign when there is one.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT id, at, kind, severity, detail, synced_at FROM events "
                "WHERE synced_at IS NULL ORDER BY id LIMIT ?", (limit,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def mark_synced(self, ids: list[int]) -> None:
        if not ids:
            return
        at = datetime.now(UTC).isoformat()
        with self._lock:
            self._db.executemany(
                "UPDATE events SET synced_at = ? WHERE id = ?",
                [(at, id) for id in ids],
            )
            self._db.commit()

    @staticmethod
    def _row(row) -> Event:
        return Event(
            id=row[0],
            at=datetime.fromisoformat(row[1]),
            kind=row[2],
            severity=row[3],
            detail=row[4],
            synced=row[5] is not None,
        )

    # --- audio recording ------------------------------------------------

    def write_audio(self, pcm: bytes, rate: int, label: str = "") -> None:
        """Append squelch-open audio to the current segment, opening one if
        needed. Called on the same tick the audio is published, so what was
        recorded and what was heard cannot diverge."""
        now = time.monotonic()
        if self._wave is not None and now - self._segment_started > MAX_SEGMENT_SECONDS:
            self.close_segment()
        if self._wave is None:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            suffix = f"-{label}" if label else ""
            self._segment_path = self.recordings_dir / f"{stamp}{suffix}.wav"
            handle = wave.open(str(self._segment_path), "wb")
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            self._wave = handle
            self._segment_started = now
        try:
            self._wave.writeframes(pcm)
        except (OSError, ValueError) as exc:
            # A full or failing disk must not stop the station transmitting
            # telemetry. Recording is the thing that degrades.
            log.warning("Could not write audio segment: %s", exc)
            self.close_segment()

    def close_segment(self) -> None:
        if self._wave is not None:
            try:
                self._wave.close()
            except Exception:  # noqa: BLE001
                pass
            self._wave = None
            self._segment_path = None

    # --- retention ------------------------------------------------------

    def prune(self, audio_hours: float, audio_mb: float, event_days: float) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=event_days)
        with self._lock:
            self._db.execute(
                "DELETE FROM events WHERE at < ? AND synced_at IS NOT NULL",
                (cutoff.isoformat(),),
            )
            self._db.commit()

        files = sorted(
            (path for path in self.recordings_dir.glob("*.wav") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
        age_cutoff = time.time() - audio_hours * 3600
        keep: list[Path] = []
        for path in files:
            if path is self._segment_path:
                keep.append(path)
                continue
            if path.stat().st_mtime < age_cutoff:
                path.unlink(missing_ok=True)
            else:
                keep.append(path)

        # Oldest first until the total is under budget.
        budget = audio_mb * 1024 * 1024
        total = sum(path.stat().st_size for path in keep if path.exists())
        for path in keep:
            if total <= budget:
                break
            if path == self._segment_path:
                continue
            try:
                total -= path.stat().st_size
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def stats(self) -> dict:
        with self._lock:
            total, pending = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(synced_at IS NULL), 0) FROM events"
            ).fetchone()
        files = [path for path in self.recordings_dir.glob("*.wav") if path.is_file()]
        return {
            "events": int(total or 0),
            "events_pending": int(pending or 0),
            "recordings": len(files),
            "recordings_mb": round(sum(f.stat().st_size for f in files) / 1e6, 1),
        }
