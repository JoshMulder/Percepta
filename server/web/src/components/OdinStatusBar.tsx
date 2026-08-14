import { useEffect, useMemo, useRef, useState } from "react";
import type { FleetStation } from "../types";

/**
 * The one line across the top of the wall: how much of the estate is in
 * trouble, what time it is in two timezones, and — the reason this component
 * exists at all — whether anything on this screen is still true.
 *
 * The dashboard it replaces polls the fleet every fifteen seconds and swallows
 * the outcome (PlatformDashboard.tsx:89-125): a failed poll leaves the previous
 * fleet on screen, rendered with exactly the same confidence as a fresh one, so
 * a wall watched from across a room can be twenty minutes stale and look
 * perfect. That is the worst failure this surface has, because an operator's
 * certainty is highest precisely when the data is worst. The pip is the fix.
 * It ticks on every completed poll and, when the polls stop arriving, goes
 * amber and counts the silence out loud in seconds.
 *
 * Staleness is derived from `lastPollAt` against a clock ticking in here, NOT
 * from a prop. A parent whose poll has died sends no new props, so anything
 * computed only on render would freeze at the last good value — the failure
 * case would be the one case the indicator could not report. The clock keeps
 * counting whether or not anything else in the tree moves.
 *
 * Colour is rationed. Only the numbers that mean trouble AND are non-zero take
 * any: a nought offline reads as neutral furniture, the same weight as the
 * total. Online is deliberately colourless — a wall of green is a wall an
 * operator stops reading, and on this screen "fine" is the absence of colour.
 * Green appears once, in the liveness pip, where it means "this data arrived
 * just now" and nothing else.
 */

type Tone = "ok" | "warn" | "bad" | "neutral";

/** The fleet poll's cadence today, used only until two completed polls have
 *  been seen and the real one can be measured. Nothing here depends on it
 *  staying accurate; see `cadence`. */
const NOMINAL_POLL_MS = 15000;

/** How many poll periods of silence before the data stops counting as current.
 *  Two whole missed polls plus a margin: one late arrival is jitter on a
 *  satellite uplink, two in a row is a fault, and an indicator that cries at
 *  the first is an indicator that gets ignored by the second. */
const STALE_AFTER_PERIODS = 2.5;

/** Floor under the staleness threshold, so a caller polling every second does
 *  not put the pip into a flapping alarm over ordinary network jitter. */
const STALE_FLOOR_MS = 10000;

/** The operator's own zone, for the local clock's tooltip. Read once: a wall
 *  display does not travel, and a browser that somehow changes zone is one
 *  reload away from being right. */
const LOCAL_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;

/** Silence, counted out. Seconds while seconds still mean something, then
 *  zero-padded minutes and hours so the figure keeps its width as it climbs. */
function elapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h${String(m % 60).padStart(2, "0")}m`;
}

export function OdinStatusBar({
  stations,
  unacked,
  lastPollAt,
  polling,
  error,
}: {
  stations: FleetStation[];
  /** Events nobody has taken responsibility for yet, counted by the parent
   *  because the rail owns what "acknowledged" means. */
  unacked: number;
  /** Epoch ms of the last poll that SUCCEEDED. Not merely completed: a failed poll must leave this frozen, or staleness can never grow and a twenty-minute blackout reads as 'failed 2s' forever. Null
   *  before the first one lands. */
  lastPollAt: number | null;
  /** A poll is in flight. Only distinguishes a cold start from a dead feed;
   *  once data has arrived its age is the thing that matters, not whether a
   *  request happens to be open at this instant. */
  polling: boolean;
  /** The last poll's failure, if it failed. Shown on hover rather than in the
   *  line: this bar is one row of figures and a sentence-length message would
   *  push them about. The pip and its label carry the fact. */
  error: string | null;
}) {
  // One interval for the whole component: both clocks and the staleness count
  // read the same instant, so they can never disagree by a tick. A second is
  // the resolution the staleness label is quoted in; anything finer would be
  // motion for its own sake, and on this wall motion is the alarm channel.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  // The observed poll cadence, smoothed, rather than a constant compiled in
  // here. A hardcoded fifteen seconds would turn a caller that deliberately
  // slowed its poll into a permanent false alarm, and a false alarm on this
  // line destroys the only indicator that says whether the wall is lying.
  // Smoothed because one poll delayed by a slow response should not rewrite the
  // cadence and drag the staleness threshold with it.
  const cadence = useRef<number | null>(null);
  const previousPoll = useRef<number | null>(null);
  useEffect(() => {
    if (lastPollAt === null) return;
    const previous = previousPoll.current;
    previousPoll.current = lastPollAt;
    // Guard against a parent that resets its clock; a negative gap is not a
    // measurement of anything.
    if (previous === null || lastPollAt <= previous) return;
    const gap = lastPollAt - previous;
    cadence.current =
      cadence.current === null ? gap : cadence.current * 0.7 + gap * 0.3;
  }, [lastPollAt]);

  // Held in a ref rather than state on purpose: this re-renders every second
  // regardless, so a measurement taken after paint is on screen within a tick,
  // and a render per poll bought nothing.
  const period = cadence.current ?? NOMINAL_POLL_MS;
  const staleAfter = Math.max(period * STALE_AFTER_PERIODS, STALE_FLOOR_MS);
  // Clamped at zero because the parent's stamp can sit slightly ahead of this
  // clock, and "stale -1s" would read as a bug in the indicator.
  const age = lastPollAt === null ? null : Math.max(0, now - lastPollAt);
  const stale = age !== null && age >= staleAfter;

  let pipTone: Tone;
  let liveness: string;
  if (age === null) {
    // Nothing has arrived yet. A first load in flight is not a fault; a first
    // load that has already failed is, and saying "no data" is more use than an
    // empty wall that looks like an estate of nothing.
    pipTone = polling && !error ? "neutral" : "warn";
    liveness = polling && !error ? "connecting" : "no data";
  } else if (stale) {
    // Staleness outranks the error text even when both are true: how old the
    // fleet is decides whether anything on screen can be acted on, and the
    // reason it stopped is a detail for the tooltip.
    pipTone = "warn";
    liveness = `stale ${elapsed(age)}`;
  } else if (error) {
    pipTone = "warn";
    liveness = `failed ${elapsed(age)}`;
  } else {
    pipTone = "ok";
    liveness = `polling (${Math.round(period / 1000)}s)`;
  }

  // Counted here rather than taken from the server's FleetStats because those
  // counts overlap: stations_offline there includes the dark ones, so a wall
  // reading "6 offline, 4 dark" cannot tell whether that is six boxes or ten.
  // This partition is disjoint and the row adds up to the total, which is the
  // arithmetic an operator does without meaning to. Dark is tested first so a
  // station can only land in one column.
  const counts = useMemo(() => {
    let online = 0;
    let offline = 0;
    let dark = 0;
    let never = 0;
    for (const s of stations) {
      if (s.dark) dark += 1;
      else if (s.status === "online") online += 1;
      else if (s.status === "never") never += 1;
      else offline += 1;
    }
    return { total: stations.length, online, offline, dark, never };
  }, [stations]);

  // "never" stays neutral however many there are: a station enrolled but not
  // yet dialling home is a deployment in progress, not a site in trouble, and
  // the server draws the same distinction (services/station_status.py).
  const figures: { label: string; value: number; tone: Tone }[] = [
    { label: "total", value: counts.total, tone: "neutral" },
    { label: "online", value: counts.online, tone: "neutral" },
    { label: "offline", value: counts.offline, tone: counts.offline ? "warn" : "neutral" },
    { label: "dark", value: counts.dark, tone: counts.dark ? "bad" : "neutral" },
    { label: "never", value: counts.never, tone: "neutral" },
    { label: "unacked", value: unacked, tone: unacked ? "warn" : "neutral" },
  ];

  const stamp = new Date(now).toISOString();

  return (
    <div className="odin-statusbar">
      {figures.map((f) => (
        <span key={f.label} className={`odin-statusbar-figure ${f.tone}`}>
          {f.value}
          <span className="odin-statusbar-label">{f.label}</span>
        </span>
      ))}

      {/* Keyed by the poll stamp, which is not a list key and is not an
          oversight: changing the key remounts the element, and remounting is
          what restarts the pip's CSS animation. That is the tick — one beat per
          completed poll, visible from across the room, with no timer of its own
          to fall out of step with the data it is reporting on. */}
      <span key={lastPollAt ?? "cold"} className={`odin-pip ${pipTone}`} aria-hidden="true" />
      {/* Never an aria-live region. It changes every second while stale, and a
          screen reader counting seconds out loud is not an accessibility win;
          the text sits in the DOM beside the pip for anyone reading it. */}
      <span className={`odin-statusbar-label ${pipTone}`}>
        {liveness}
      </span>
      {/* The reason, as text. It was on a `title` attribute of a non-focusable
          span: mouse-only, so unreachable by keyboard, unreachable by touch, and
          not reliably announced. On a wall display a pointer is frequently the
          one input that is not available, and "stale 47s" without "why" is not
          something anybody can act on. */}
      {error && <span className="odin-statusbar-label warn">{error}</span>}

      <time className="odin-clock" dateTime={stamp}>
        {/* toISOString, not a locale format: this is the clock the site logs and
            the event rail are stamped in, and it has to mean the same thing
            whoever is on shift. */}
        {stamp.slice(11, 19)}
        <span className="odin-statusbar-label">UTC</span>
      </time>
      <time className="odin-clock" dateTime={stamp} title={LOCAL_ZONE}>
        {/* Forced to 24 hour. The browser's own preference is 12 hour in several
            of the locales this runs in, and "11:59:59 pm" beside a UTC clock is
            both wider and a different shape, so the pair would jump every time
            the meridiem changed. */}
        {new Date(now).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
        })}
        <span className="odin-statusbar-label">local</span>
      </time>
    </div>
  );
}
