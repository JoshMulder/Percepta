import { useEffect, useMemo, useState } from "react";
import type { FleetEvent } from "../types";

/**
 * The standing queue of things the wall wants somebody to look at, down the
 * left of the screen and never hidden.
 *
 * It is a QUEUE, not a feed, and the sort order is the whole argument.
 * Severity first, then oldest-first within each severity, so the top row is
 * the worst thing that has been waiting longest. A feed sorts newest-first,
 * which reads well for somebody already watching; nobody is already watching
 * this wall, and newest-first pushes the alert that has gone unattended for
 * three hours off the bottom of the rail — the one item whose entire problem
 * is that it has been forgotten. Here the queue ages upward, and the forgotten
 * thing rises to meet the operator.
 *
 * Read-only, deliberately. There is no acknowledgement endpoint yet, and the
 * tempting local version — a set of dismissed ids held in component state —
 * would be worse than none at all: it would teach an operator that the wall
 * remembers what has been dealt with, then lose the lot on the next reload, or
 * overnight when the browser restarts itself. A queue that forgets quietly is
 * how an alarm goes unanswered. Until the platform can remember, the rail does
 * not claim to.
 *
 * Nothing is truncated here. Whatever the caller hands over is drawn, because
 * a cap applied inside the rail would silently drop rows that a caller had
 * chosen to pass, and under this sort the rows a cap drops are the oldest or
 * the newest — never the unimportant ones. The rail scrolls instead, and how
 * much history reaches it stays the caller's decision.
 *
 * Colour is rationed the same way as the rest of ODIN: critical is `bad`,
 * warning is `warn`, and everything else — info, and any severity a future
 * station invents — is `neutral` and carries no colour whatsoever. The `ok`
 * tone exists in the vocabulary but is never used on this rail: green belongs
 * to the liveness pip and to nothing else, and a rail that turned green for
 * "seen to" would be training the operator to stop reading colour.
 */

/** Lowest sorts highest up the rail. Anything unrecognised ranks with info
 *  rather than above critical: the station vocabulary is normalised server-side
 *  to info/warning/critical, so a value outside that set is a station saying
 *  something this console does not understand yet, and an unread word is not
 *  grounds for promoting a row over a stated critical. */
const SEVERITY_RANK: Record<string, number> = { critical: 0, warning: 1 };

function rank(severity: string): number {
  return SEVERITY_RANK[severity] ?? 2;
}

function tone(severity: string): string {
  if (severity === "critical") return "bad";
  if (severity === "warning") return "warn";
  return "neutral";
}

/** Stands in for a timestamp that would not parse. A sentinel rather than
 *  Infinity because the comparator subtracts one age from another, and
 *  Infinity minus Infinity is NaN, which leaves the sort order arbitrary.
 *  Far-future, so an unreadable event sinks to the bottom of its severity
 *  group instead of claiming to be the oldest thing on the wall and camping at
 *  the top of the rail. */
const UNDATED = Number.MAX_SAFE_INTEGER;

function receivedMs(ev: FleetEvent): number {
  const t = Date.parse(ev.received_at);
  return Number.isNaN(t) ? UNDATED : t;
}

function age(at: number, now: number): string {
  if (at === UNDATED) return "—";
  // Clamped at zero. The station's clock and the platform's need not agree to
  // the second, and a row reading "-2s" looks like a broken display rather
  // than like the small skew it actually is.
  const s = Math.max(0, (now - at) / 1000);
  // Floored, never rounded. Rounding turns thirty-one seconds into "1m" and
  // overstates how long the queue has gone unattended, which is the one number
  // this rail exists to state honestly. Flooring can only ever understate it.
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

const SPOKEN_UNITS: Record<string, string> = {
  s: "seconds",
  m: "minutes",
  h: "hours",
  d: "days",
};

/** The age again, in words, for the row's accessible name. The visible token is
 *  compressed to keep the column narrow and the digits still; read aloud, "4m"
 *  is not a duration. */
function spokenAge(token: string): string {
  const unit = SPOKEN_UNITS[token.slice(-1)];
  return unit ? `${token.slice(0, -1)} ${unit} ago` : "age unknown";
}

/** The exact time, for the hover. The rail states ages because an age is what
 *  an operator acts on, but the moment is what they write down afterwards, and
 *  a relative age cannot be recovered from a screen photographed an hour on. */
function exactly(ev: FleetEvent): string | undefined {
  const t = Date.parse(ev.received_at);
  return Number.isNaN(t) ? undefined : new Date(t).toLocaleString();
}

export function AlertRail({
  events,
  onSelectStation,
}: {
  events: FleetEvent[];
  /** A row click is a request to look at that station. What looking means
   *  belongs to the wall, not to the rail. */
  onSelectStation: (stationId: string) => void;
}) {
  // Ages have to keep counting on their own. The fleet is polled on a slow
  // interval because station state moves in minutes, and an age frozen between
  // polls is a wall that looks stopped — on a screen where stillness is the
  // normal state, a stopped clock is indistinguishable from a dead browser.
  // One second is the finest resolution the rail displays, so it is the rate
  // the rail recalculates at; anything slower would show ages that skip.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  // Keyed on the events alone, not on the tick: the order depends on when each
  // event arrived, which the passing of time does not change. Re-sorting every
  // second would be work done to reach the same answer.
  const queue = useMemo(() => {
    return events
      .map((ev) => ({ ev, at: receivedMs(ev) }))
      .sort(
        (a, b) =>
          rank(a.ev.severity) - rank(b.ev.severity) ||
          a.at - b.at ||
          // Two events recorded in the same instant, which happens when one
          // fault raises several. Broken by id so the pair cannot swap places
          // between renders: a row that moves under a pointer on the way to a
          // click is a mis-click, and on this wall any movement at all reads
          // as something happening.
          a.ev.id.localeCompare(b.ev.id),
      );
  }, [events]);

  return (
    <section className="odin-rail" aria-label="Alert queue">
      {queue.length === 0 ? (
        // The normal state, and it must not look like a failure to load. No
        // glyph, no reassuring green badge, no "all clear" — a wall that
        // decorates its own quiet is a wall with something on it to ignore.
        <p className="odin-rail-empty">Nothing waiting.</p>
      ) : (
        queue.map(({ ev, at }) => {
          const token = age(at, now);
          const severity = tone(ev.severity);
          // An event may be stored with no message, or with a message that is
          // only whitespace. Its type is the least the rail can honestly say,
          // and it is worth more than a row that is blank where the sentence
          // should be.
          const said = ev.message?.trim() || ev.type;
          return (
            <button
              key={ev.id}
              type="button"
              className={`odin-rail-row ${severity}`}
              onClick={() => onSelectStation(ev.station_id)}
              title={exactly(ev)}
              // The severity reaches a sighted reader as colour and position,
              // neither of which survives being read aloud, so the accessible
              // name carries it in words and the marker itself is hidden.
              aria-label={`${ev.severity}, ${spokenAge(token)}: ${ev.station_name}, ${
                ev.organization_name
              }. ${said}`}
            >
              <span className={`odin-rail-sev ${severity}`} aria-hidden="true" />
              {/* Tabular figures, set in the stylesheet: this digit changes
                  every second on the newest row, and a proportional 1 next to a
                  proportional 8 would shuffle the whole line each tick. */}
              <span className="odin-rail-when">{token}</span>
              <span className="odin-rail-where">
                {ev.station_name} · {ev.organization_name}
              </span>
              {/* The message is the row's own text rather than a span of its
                  own, which is why there is no class for it. It is what the row
                  says; everything wrapped above is furniture around it. */}
              {said}
            </button>
          );
        })
      )}
    </section>
  );
}
