import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api";
import type { FleetStation, OdinTranscript } from "../types";

/**
 * What was said on the guarded channels, in one column.
 *
 * POLLED, and the delay is real rather than a shortcut. A transcript is produced
 * on the station AFTER an over ends — the agent transcribes, writes a
 * `radio.transmission` event, and that event travels the ordinary telemetry path
 * on its own schedule, seconds behind the audio and sometimes much more on a
 * busy box. Streaming it down the audio socket would imply a synchrony that does
 * not exist; a feed that visibly lags is honest about what it is.
 *
 * IT IS A COMPANION, NOT A SUBSTITUTE. Transcription is off on most stations and
 * imperfect on the rest — NZ callsigns in particular are what the models get
 * wrong (see the ATC-model investigation) — so this exists to help an operator
 * catch up on a channel they were not listening to, and never to replace having
 * heard it. Nothing on the wall alerts from it.
 *
 * EMPTY IS THE NORMAL STATE and must not look like a fault. Most stations have
 * transcription off entirely.
 */

const POLL_MS = 10_000;

export function TranscriptFeed({
  guarded,
  stations,
  onSelectStation,
}: {
  /** The guard set, so the feed follows the strip. Also the authorisation
   *  boundary the server enforces — it will not return a station that is not
   *  active, whatever is asked for. */
  guarded: string[];
  stations: FleetStation[];
  /** Clicking a line opens that site. The path from "I read something odd" to
   *  "I can see that site" is the reason to have the feed on the wall at all,
   *  and it should not go via finding the tile by hand. */
  onSelectStation?: (stationId: string) => void;
}) {
  const [rows, setRows] = useState<OdinTranscript[]>([]);
  const [failed, setFailed] = useState(false);
  const listRef = useRef<HTMLDivElement | null>(null);

  const names = useMemo(() => {
    const out: Record<string, string> = {};
    for (const s of stations) out[s.id] = s.name;
    return out;
  }, [stations]);

  // Joined into a string so the effect re-runs when the SET changes rather than
  // on every render that happens to rebuild the array.
  const key = guarded.join(",");

  useEffect(() => {
    if (guarded.length === 0) {
      setRows([]);
      return;
    }
    let cancelled = false;
    const read = () => {
      api
        .odinTranscripts(key)
        .then((r) => {
          if (cancelled) return;
          setRows(r);
          setFailed(false);
        })
        .catch(() => {
          if (!cancelled) setFailed(true);
        });
    };
    read();
    const id = window.setInterval(read, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
    // `key` is the set; `guarded.length` only guards the empty case.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  if (guarded.length === 0) {
    return (
      <div className="odin-tx">
        <div className="odin-tx-head">TRANSCRIPT</div>
        <div className="odin-rail-empty">Guard a channel to see transcripts</div>
      </div>
    );
  }

  return (
    <div className="odin-tx">
      <div className="odin-tx-head">
        TRANSCRIPT
        {failed && <span className="odin-watch-note warn">feed unavailable</span>}
      </div>
      <div className="odin-tx-list" ref={listRef}>
        {rows.length === 0 ? (
          // Not an error, and it is the common case: transcription is off on
          // most stations.
          <div className="odin-rail-empty">Nothing transcribed</div>
        ) : (
          rows.map((r, i) => (
            <button
              type="button"
              key={`${r.ground_station_id}-${r.t}-${i}`}
              className="odin-tx-row"
              onClick={() => onSelectStation?.(r.ground_station_id)}
              title={
                onSelectStation
                  ? `Open ${names[r.ground_station_id] ?? "this station"}`
                  : undefined
              }
            >
              <span className="odin-tx-when">{r.clock ?? shortTime(r.t)}</span>
              <span className="odin-tx-who">
                {names[r.ground_station_id] ?? "unknown"}
              </span>
              <span className="odin-tx-what">{r.message}</span>
            </button>
          ))
        )}
      </div>
    </div>
  );
}

function shortTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
