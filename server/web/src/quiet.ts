import { isNominal } from "./stance";
import type { FleetStation } from "./types";

/**
 * NOTHING TO REPORT: the wall is the list of what is wrong, not a register of
 * the fleet.
 *
 * The premise is one step past "fine has no colour" — **fine has no pixels.**
 * If a wall of green teaches an operator to ignore colour, a wall of neutral
 * rectangles teaches them to ignore rectangles. So a healthy station's name is
 * not on the wall at all, and a station going dark stops being a recoloured
 * 200x120 rectangle among similar rectangles and becomes a large photograph of
 * a site appearing on an otherwise empty screen. The alarm channel gains an
 * order of magnitude of contrast for free, purely because the resting state is
 * empty.
 *
 * THE WALL'S OCCUPIED AREA TRACKS TROUBLE, NOT FLEET SIZE. A hundred healthy
 * stations and three healthy stations render identically — one line — which is
 * why this layout has no equivalent of `COLLAPSE_ABOVE`. There is nothing to
 * collapse.
 *
 * THE HONEST OBJECTION, kept in view rather than argued away: a screen that is
 * blank most of the time is a screen people stop glancing at, and an operator
 * never rehearses the occupied layout. The muster strip, the quiet counter and
 * the memory line below are the mitigations, and they are mitigations rather
 * than answers.
 */

/** 0 cases, 1-6, or 7+. Three learned shapes, not a continuum — an operator can
 *  learn three, and "the map is big" then reads as "nothing is wrong" from
 *  across the room, before a single word. */
export type Geometry = "quiet" | "cases" | "storm";

/** Past this many cases there is no "the worst thing", and spending the best
 *  real estate on an arbitrary pick among equals is worse than not picking. */
export const LEAD_LIMIT = 6;

/** Past this the wall groups by cause rather than listing stations, or it is
 *  wallpaper again. */
export const STORM_AT = 7;

export function geometryFor(caseCount: number): Geometry {
  if (caseCount === 0) return "quiet";
  return caseCount >= STORM_AT ? "storm" : "cases";
}

export interface Split {
  geometry: Geometry;
  /** The single worst thing, given the whole left panel. Null in quiet, and
   *  null in a storm — see `LEAD_LIMIT`. */
  lead: FleetStation | null;
  /** Cases below the lead, one row each. */
  files: FleetStation[];
  /** Everything nominal: counted, never named. */
  muster: FleetStation[];
}

/**
 * Split a ranked fleet into what the wall actually draws.
 *
 * `ranked` must already be worst-first — the caller owns the comparator, and
 * this deliberately does not re-sort, so there is exactly one answer on the
 * screen to "which station is worst".
 *
 * `promote` is the open drawer's station. It becomes a case for as long as it
 * is open even when it is perfectly healthy, because the operator asked to look
 * at it and a wall that empties the moment you click something is arguing with
 * the person using it.
 */
export function split(
  ranked: FleetStation[],
  { promote }: { promote?: string | null } = {},
): Split {
  const cases: FleetStation[] = [];
  const muster: FleetStation[] = [];
  for (const s of ranked) {
    if (!isNominal(s) || (promote != null && s.id === promote)) cases.push(s);
    else muster.push(s);
  }
  const geometry = geometryFor(cases.length);
  if (geometry === "quiet") return { geometry, lead: null, files: [], muster };
  if (geometry === "storm") return { geometry, lead: null, files: cases, muster };
  return { geometry, lead: cases[0], files: cases.slice(1), muster };
}

/**
 * Which stations are worth a picture.
 *
 * CASES ONLY, and in a storm nobody — which is the property that matters most
 * on this fleet's hardware. Every station on this list opens its camera once a
 * minute, and on a board whose supply already cannot hold its own peak that is
 * not free. A healthy fleet puts ZERO cameras on duty here, against one per
 * drawn tile on the grid.
 */
export function withPosters(s: Split): string[] {
  if (s.geometry === "storm") return [];
  return [s.lead, ...s.files].filter(Boolean).map((x) => (x as FleetStation).id);
}

/** How long a case has been a case, from the last time we heard from it. Used
 *  for the lead tie-break and shown on every row: "offline" without "for how
 *  long" is not something anybody can act on. */
export function onsetOf(s: FleetStation): number | null {
  if (!s.last_seen_at) return null;
  const t = Date.parse(s.last_seen_at);
  return Number.isFinite(t) ? t : null;
}

export interface StormGroup {
  key: string;
  /** "offline", "dark" — the shared cause, in the words the wall already uses. */
  cause: string;
  organization: string;
  stations: FleetStation[];
  /** Oldest onset in the group, or null when nothing in it has ever reported. */
  since: number | null;
}

/** Onset bucket width. Five minutes is wide enough that one provider's outage
 *  lands in a single bucket despite stations noticing it seconds apart, and
 *  narrow enough that two unrelated failures an hour apart never merge. */
export const BUCKET_MS = 5 * 60_000;

/**
 * Group a storm by what is actually happening to it.
 *
 * "14 offline · Meridian Air · all since 03:11" is ONE case, not fourteen. That
 * sentence is the single most valuable fact during a mass outage and nothing on
 * the wall today can say it — fourteen identical tiles say "fourteen problems"
 * when the truth is one, and an operator who reads fourteen starts fourteen
 * investigations.
 *
 * Grouped by cause, then organisation, then a five-minute onset bucket, because
 * those three together are what makes a common cause visible: the same failure,
 * at the same customer, at the same moment.
 */
export function groupStorm(cases: FleetStation[]): StormGroup[] {
  const by = new Map<string, StormGroup>();
  for (const s of cases) {
    const cause = s.dark ? "dark" : s.status === "offline" ? "offline" : s.status === "never" ? "never connected" : "degraded";
    const onset = onsetOf(s);
    const bucket = onset == null ? "unknown" : String(Math.floor(onset / BUCKET_MS));
    const key = `${cause}|${s.organization_name}|${bucket}`;
    const found = by.get(key);
    if (found) {
      found.stations.push(s);
      if (onset != null) {
        found.since = found.since == null ? onset : Math.min(found.since, onset);
      }
    } else {
      by.set(key, {
        key,
        cause,
        organization: s.organization_name,
        stations: [s],
        since: onset,
      });
    }
  }
  // Biggest group first: the largest common cause is the story of the outage.
  // Ties by cause then organisation so the order is stable across polls — a
  // group that changes place on a refresh where nothing happened is motion, and
  // motion on this wall is supposed to mean something.
  return [...by.values()].sort(
    (a, b) =>
      b.stations.length - a.stations.length ||
      a.cause.localeCompare(b.cause) ||
      a.organization.localeCompare(b.organization),
  );
}

/** Short and spoken, for durations that are read at a distance. */
export function since(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  const m = ms / 60000;
  if (m < 1) return "just now";
  if (m < 90) return `${Math.round(m)}m`;
  const h = m / 60;
  if (h < 48) return `${Math.round(h)}h${Math.round(m % 60) ? ` ${Math.round(m % 60)}m` : ""}`;
  return `${Math.round(h / 24)}d`;
}
