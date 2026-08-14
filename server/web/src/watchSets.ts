/**
 * Saved watch sets, in localStorage.
 *
 * An operator coming on at 22:00 should get their channels back in one action,
 * not eight. That is the whole feature, and the size of it is why it lives here
 * rather than on the account: a server-side store would be a table, a migration
 * and two endpoints for a list of uuids that is meaningful to exactly one person
 * at exactly one desk. Radio presets and display preferences made the same call.
 *
 * The cost is real and worth stating: a set is per-BROWSER, so an operator who
 * moves to the spare position finds it empty. On a wall display — a fixed
 * machine in a fixed room, which is what these are — that is nearly always the
 * same browser, and the failure mode is "load it again", not "lose something".
 *
 * STATION IDS ARE STORED, NOT NAMES. A saved set is a claim about which sites to
 * guard, and it has to survive a station being renamed. It does NOT have to
 * survive a station being deleted or deactivated: the server refuses those on
 * `watch_set` and simply returns the set without them, so a stale entry costs a
 * channel that does not light rather than an error.
 */

const KEY = "percepta.odin.watchsets";

export interface WatchSet {
  name: string;
  stations: string[];
}

export function loadWatchSets(): WatchSet[] {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Validated rather than trusted. This is a string somebody could have edited
    // by hand, and a malformed entry must cost that entry rather than the whole
    // list — an operator who loses every saved set because one is broken has
    // lost the feature at the moment they needed it.
    return parsed.filter(
      (s): s is WatchSet =>
        s !== null &&
        typeof s === "object" &&
        typeof s.name === "string" &&
        Array.isArray(s.stations) &&
        s.stations.every((x: unknown) => typeof x === "string"),
    );
  } catch {
    return [];
  }
}

export function saveWatchSet(name: string, stations: string[]): WatchSet[] {
  const trimmed = name.trim();
  if (!trimmed) return loadWatchSets();
  // Replaces by name rather than appending, so saving twice under the same name
  // updates it. A list that grows a duplicate every time somebody re-saves is a
  // list nobody keeps using.
  const next = [
    ...loadWatchSets().filter((s) => s.name !== trimmed),
    { name: trimmed, stations },
  ];
  write(next);
  return next;
}

export function deleteWatchSet(name: string): WatchSet[] {
  const next = loadWatchSets().filter((s) => s.name !== name);
  write(next);
  return next;
}

function write(sets: WatchSet[]): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(sets));
  } catch {
    // Private browsing, or a full quota. Swallowed: failing to remember a
    // convenience must not break the watch itself, which is the thing on shift.
  }
}
