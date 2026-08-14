import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, api } from "../api";
import type { OdinAlert } from "../types";

/**
 * The queue down the left of the wall, and the surface an operator works.
 *
 * A QUEUE, NOT A FEED. Critical above warning, and OLDEST FIRST within a
 * severity. A queue ages upward; newest-first is a social feed, and it buries
 * the thing that has been waiting longest — which on a wall is precisely the
 * thing most likely to have been forgotten. Unacked sorts above acked for the
 * same reason: what nobody has taken belongs at the top.
 *
 * ACK IS NOT CLOSE, and keeping them apart is the whole design.
 *
 *   Ack   "I have seen this and I am dealing with it." It stops a second
 *         operator picking up the same fault, and it is also the assignment —
 *         there is no separate assignee, because two concepts where one will do
 *         is how a queue stops being read.
 *   Close "It stopped being true."
 *
 * The station keeps its attention colour on the wall until the alert is CLOSED,
 * so acknowledging never hides a site that is still broken. Command centres lose
 * faults exactly where those two actions are merged into one button.
 *
 * One click, no modal, no confirmation. A dialog between an operator and an ack
 * is a dialog they will learn to dismiss without reading, and it costs the
 * seconds where the fault is newest.
 *
 * ANOTHER OPERATOR'S ACK ARRIVES ON THE DIGEST. The list is a prop, refreshed
 * on the same frame as the stations, so two desks cannot disagree about who
 * holds something. A 409 from the server is not an error to retry: it is the
 * answer, and it means somebody else got there first.
 */

const SEVERITY_RANK: Record<string, number> = { critical: 0, warning: 1, info: 2 };

function ago(iso: string, now: number): string {
  const s = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

export function AlertRail({
  alerts,
  stationNames,
  onSelectStation,
  onChanged,
}: {
  alerts: OdinAlert[];
  /** id -> name, so a row can say where it is without another lookup. */
  stationNames: Record<string, string>;
  onSelectStation: (stationId: string) => void;
  /** Called after an action lands, so the caller can re-read rather than wait
   *  out the digest cycle — the operator who clicked should see their own
   *  change immediately even though everyone else sees it on the next frame. */
  onChanged: () => void;
}) {
  // Ages have to keep counting while no new props arrive: a quiet fleet sends
  // no frames worth re-rendering for, and "4m" frozen at 4m is a lie that grows.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  /** Which rows are mid-request. Keyed by id rather than a single boolean so one
   *  slow ack does not freeze the whole rail. */
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [conflict, setConflict] = useState<Record<string, boolean>>({});

  const ordered = useMemo(() => {
    return [...alerts].sort((a, b) => {
      // Unacked first: what nobody has taken is what needs somebody.
      const takenA = a.state === "acked" ? 1 : 0;
      const takenB = b.state === "acked" ? 1 : 0;
      if (takenA !== takenB) return takenA - takenB;
      const sevA = SEVERITY_RANK[a.severity] ?? 3;
      const sevB = SEVERITY_RANK[b.severity] ?? 3;
      if (sevA !== sevB) return sevA - sevB;
      // Oldest first. The queue ages upward.
      return (
        new Date(a.first_seen_at).getTime() - new Date(b.first_seen_at).getTime()
      );
    });
  }, [alerts]);

  const act = useCallback(
    async (id: string, what: "ack" | "close") => {
      setBusy((b) => ({ ...b, [id]: true }));
      setConflict((c) => ({ ...c, [id]: false }));
      try {
        if (what === "ack") await api.ackAlert(id);
        else await api.closeAlert(id);
        onChanged();
      } catch (e) {
        // 409 means another operator holds it. NOT retried: it is the answer,
        // and the row is marked so the person who clicked learns why nothing
        // happened rather than clicking again.
        if (e instanceof ApiError && e.status === 409) {
          setConflict((c) => ({ ...c, [id]: true }));
          onChanged();
        }
      } finally {
        setBusy((b) => ({ ...b, [id]: false }));
      }
    },
    [onChanged],
  );

  if (ordered.length === 0) {
    return (
      <div className="odin-rail">
        {/* The normal state, and it must not look like a broken one. */}
        <div className="odin-rail-empty">Nothing waiting</div>
      </div>
    );
  }

  return (
    <div className="odin-rail">
      {ordered.map((a) => {
        const taken = a.state === "acked";
        return (
          <div
            key={a.id}
            className={`odin-rail-row ${a.severity}${taken ? " acked" : ""}`}
          >
            <span className={`odin-rail-sev ${a.severity}`} aria-hidden="true" />

            <button
              type="button"
              className="odin-rail-body"
              onClick={() => onSelectStation(a.ground_station_id)}
              title={a.message ?? a.title}
            >
              <span className="odin-rail-title">{a.title}</span>
              <span className="odin-rail-where">
                {stationNames[a.ground_station_id] ?? "unknown station"}
                {a.occurrences > 1 && (
                  // One row per fact, so this is how often it has happened
                  // rather than how many rows it earned.
                  <span className="odin-rail-count"> ×{a.occurrences}</span>
                )}
              </span>
            </button>

            <span className="odin-rail-when">{ago(a.first_seen_at, now)}</span>

            <span className="odin-rail-actions">
              {!taken && (
                <button
                  type="button"
                  className="odin-rail-act"
                  disabled={busy[a.id]}
                  onClick={() => void act(a.id, "ack")}
                  title="Take ownership. Stops anyone else picking this up."
                >
                  ack
                </button>
              )}
              <button
                type="button"
                className="odin-rail-act"
                disabled={busy[a.id]}
                onClick={() => void act(a.id, "close")}
                title="It stopped being true. Removes the station's attention colour."
              >
                close
              </button>
            </span>

            {taken && (
              <span className="odin-rail-held" title="Acknowledged">
                held
              </span>
            )}
            {conflict[a.id] && (
              <span className="odin-rail-held warn">taken by someone else</span>
            )}
          </div>
        );
      })}
    </div>
  );
}
