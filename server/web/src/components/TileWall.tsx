import { useEffect, useMemo } from "react";
import { StationTile } from "./StationTile";
import type { FleetStation } from "../types";

/**
 * The wall: one tile per station, worst first.
 *
 * Reading order is the whole design. A tile is caught peripherally long before
 * anybody reads a word of it, so the tile most worth catching has to be where
 * the eye already rests — top left — and it has to still be there on the next
 * poll. That is why the comparator has two halves that do different jobs. Rank
 * decides what is worst; organisation then name decides everything else, and
 * decides it from values that do not change between polls. Leaving ties to
 * whatever order the server returned would let two neighbouring tiles swap
 * places on a refresh where nothing happened, and on a wall whose alarm channel
 * is motion, a swap is a false alarm every few seconds. An operator who learns
 * that the wall moves for no reason has been trained to ignore it moving.
 *
 * Dark sorts above merely offline because they are different questions. Offline
 * is "we have not heard from it lately", which a satellite uplink does on its
 * own several times a day; dark is "it is gone", which somebody has to drive to.
 * Never-seen sorts below both: a station that has not once reported is a
 * commissioning job, not an outage, and it would otherwise sit at the top of
 * the wall for weeks holding the position that outages need.
 *
 * Past about sixty stations the wall pages BY EXCEPTION rather than by page.
 * Every non-nominal station keeps its tile and the nominal remainder collapses
 * to a single count. Paging by page, or letting the grid overflow, both end the
 * same way: the thing that needed attention was on the part of the wall nobody
 * was looking at. A wall that has to be scrolled is a wall that will be missed.
 * Below the threshold nothing is collapsed, because a wall that fits is worth
 * more whole — the nominal tiles are the reference against which a coloured one
 * reads as wrong.
 *
 * No colour is decided here. The tile owns its own tone, and the rules for it
 * (one flat neutral for nominal, colour rationed so that any colour at all means
 * attention) live there. This file decides only which stations get a tile and in
 * what order — and being nominal enough to be collapsed is a statement about
 * position on the wall, not a statement about colour.
 *
 * The empty fleet renders an empty grid on purpose. Nothing in this layout says
 * "no stations" better than the status bar's own count already does, and a wall
 * that explains itself in prose is a wall with prose on it.
 */

/**
 * The point past which nominal stations lose their tiles.
 *
 * Roughly a screen and a half at the sizes this grid is laid out for, chosen so
 * the collapse only ever fires on a wall that was going to overflow anyway. It
 * is deliberately not derived from the viewport: a threshold that changed with
 * the window would make the same fleet look different on the operator's desk
 * and on the wall panel, and those two have to agree.
 */
const COLLAPSE_ABOVE = 60;

/**
 * State of charge at or below which a station keeps its tile even when
 * everything else about it reads fine.
 *
 * A solar site that is discharging is not yet news — that is what the battery is
 * for, and every site on the fleet does it every night. A site that has drawn
 * itself down this far is news, because the remaining vitals stop being true
 * some hours from now and there is nobody there to notice. Keep this equal to
 * whatever threshold the tile turns its charge band amber at, or the wall will
 * collapse a tile that would have been coloured had it been drawn.
 */
const LOW_SOC_PCT = 30;

/**
 * Fixed locale, not the browser's. Two operators looking at the same fleet from
 * different machines have to see the same order, and `localeCompare` with no
 * locale is whatever the machine was set up as. Numeric so "Ridge 9" sorts above
 * "Ridge 10" rather than the way a plain string sort insists on.
 */
const byText = new Intl.Collator("en", { numeric: true, sensitivity: "base" });

/**
 * How worrying a station is, lowest first.
 *
 * REACHABILITY FIRST, THEN TROUBLE. A station we cannot hear from outranks one
 * that is shouting, because a dark site may be shouting too and we would not
 * know. That ordering is the original and it survives.
 *
 * What is new is everything below rank 2: this used to return 3 for every
 * station that was merely online, so a site with five open criticals sorted
 * identically to a nominal one and landed wherever its organisation's name fell
 * alphabetically. "Trouble is top-left" was true only for stations that had
 * stopped talking.
 *
 * UNACKED BEATS ACKED, and that is the whole point of acking: taking an alert
 * says somebody is dealing with it, so the tile steps aside for the ones nobody
 * has picked up. It does NOT leave the trouble tiers — a station stays above the
 * nominal ones until its alerts are closed, because acking is not fixing.
 *
 * A station under a maintenance window cannot be promoted here, and needs no
 * special case: suppression happens at raise time on the server, so a silenced
 * site has no open alerts to promote it.
 */
function rank(s: FleetStation, alerts?: StationAlertSummary): number {
  if (s.dark) return 0;
  if (s.status === "offline") return 1;
  if (s.status === "never") return 2;
  if (alerts) {
    if (alerts.unackedCritical > 0) return 3;
    if (alerts.critical > 0) return 4;
    if (alerts.unackedWarning > 0) return 5;
    if (alerts.warning > 0) return 6;
  }
  // The station's own account of itself, for a site with no platform alert
  // raised against it yet — a condition it is reporting is still trouble.
  // `?? 0` because the field is optional on the wire: a station that has not
  // reported health yet has no count, and an absent count is not a fault.
  if ((s.condition_count ?? 0) > 0) return 7;
  return 8;
}

/** What the rail knows about one station, reduced to what the sort needs. */
export interface StationAlertSummary {
  critical: number;
  unackedCritical: number;
  warning: number;
  unackedWarning: number;
}

/**
 * Index the alert list by station.
 *
 * Built once per render of the wall rather than scanned per station inside the
 * comparator — a sort is O(n log n) comparisons and a linear scan inside one
 * makes the wall quadratic in the fleet just as it gets big enough to matter.
 */
export function alertIndex(
  alerts: { ground_station_id: string; severity: string; state: string }[],
): Record<string, StationAlertSummary> {
  const out: Record<string, StationAlertSummary> = {};
  for (const a of alerts) {
    const e = (out[a.ground_station_id] ??= {
      critical: 0,
      unackedCritical: 0,
      warning: 0,
      unackedWarning: 0,
    });
    const unacked = a.state === "open";
    if (a.severity === "critical") {
      e.critical += 1;
      if (unacked) e.unackedCritical += 1;
    } else if (a.severity === "warning") {
      e.warning += 1;
      if (unacked) e.unackedWarning += 1;
    }
  }
  return out;
}

/**
 * Whether this station is quiet enough to be counted rather than drawn.
 *
 * Every test here is written so that an absent or null vital reads as nominal,
 * because null means "not known right now" and not knowing is not a fault. A
 * station that has connected but not yet sent a health frame is the ordinary
 * case a minute after a restart, and treating that silence as trouble would put
 * a tile on the wall for every station that rebooted overnight.
 *
 * Running on battery is not on this list, for the same reason `LOW_SOC_PCT`
 * exists: on a solar site it is the normal state of every night, and a rule that
 * flagged it would collapse nothing between dusk and dawn — exactly the hours
 * this wall is least watched and most needs to be short.
 */
function isNominal(s: FleetStation): boolean {
  if (s.dark || s.status !== "online") return false;
  if (s.health != null && s.health !== "ok") return false;
  if ((s.condition_count ?? 0) > 0) return false;
  // A named condition with no count is still a condition; the count is the
  // optional half of that pair, not the authoritative one.
  if (s.worst_condition) return false;
  // Only an explicit false. The station saying it cannot see us, while we can
  // see it, is one of the more interesting disagreements on this wall.
  if (s.uplink_connected === false) return false;
  if (s.soc_pct != null && s.soc_pct <= LOW_SOC_PCT) return false;
  // A failed device. Without this a station whose camera or receiver has died
  // but whose link and battery are fine reads as nominal and is collapsed off
  // the wall entirely — which is the one outcome exception-pagination exists to
  // prevent, and the failure would be invisible precisely because the station
  // is otherwise healthy enough not to draw the eye.
  //
  // "present" and "absent" are both fine: absent means never fitted, and a
  // camera nobody installed has not failed. Anything else is the station
  // telling us something about a device it expected to work.
  if (s.slots) {
    for (const state of Object.values(s.slots)) {
      if (state !== "present" && state !== "absent") return false;
    }
  }
  return true;
}

export function TileWall({
  stations,
  selectedId,
  onSelect,
  alerts,
  onShowing,
}: {
  stations: FleetStation[];
  /** The station whose drawer is open, or null. */
  selectedId: string | null;
  onSelect: (id: string) => void;
  /** Open alerts per station, from `alertIndex`. Optional so the wall still
   *  sorts sensibly on reachability alone if the rail has not loaded — a tile
   *  in the wrong order is better than a wall that does not render. */
  alerts?: Record<string, StationAlertSummary>;
  /** Told which stations actually have a tile on screen, so the wall can ask
   *  those — and only those — for a periodic still. The collapsed nominal
   *  stations are deliberately excluded: they have no tile to put a picture on,
   *  and asking them anyway would put a fleet of field cameras on duty for a
   *  row that says "14 nominal". */
  onShowing?: (stationIds: string[]) => void;
}) {
  // Two memos rather than one, and the split is the point: the comparator runs
  // over the whole fleet and must not be re-run because somebody clicked a tile.
  // Sorting depends on the poll; the exception split depends on the selection;
  // keeping them apart means a click costs a filter and a poll costs a sort.
  const sorted = useMemo(
    () =>
      // Copied before sorting. `stations` belongs to the caller's fleet state and
      // is handed to the map and the rail as well; sorting in place would reorder
      // what they are rendering from, mid-poll, from inside a memo.
      [...stations].sort((a, b) => {
        const byRank = rank(a, alerts?.[a.id]) - rank(b, alerts?.[b.id]);
        if (byRank !== 0) return byRank;
        const byOrg = byText.compare(a.organization_name, b.organization_name);
        if (byOrg !== 0) return byOrg;
        const byName = byText.compare(a.name, b.name);
        if (byName !== 0) return byName;
        // Names repeat across a fleet ("Ridge", "North Mast"), including inside
        // one organisation. Without a last resort those two tiles trade places
        // on every poll, which is the exact flicker the sort exists to prevent.
        return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
      }),
    [stations, alerts],
  );

  const { tiled, collapsed } = useMemo(() => {
    if (sorted.length <= COLLAPSE_ABOVE) return { tiled: sorted, collapsed: 0 };
    // The open station keeps its tile whatever its condition. A tile that
    // disappears at the moment it is clicked, because opening its drawer is how
    // you found out it was nominal, is a wall arguing with the operator.
    const tiled = sorted.filter((s) => s.id === selectedId || !isNominal(s));
    return { tiled, collapsed: sorted.length - tiled.length };
  }, [sorted, selectedId]);

  // SORTED AND JOINED, and the effect depends on that string alone.
  //
  // `tiled` is a fresh array on every poll, so depending on it would re-declare
  // the set every three seconds — a burst of commands to every station on the
  // wall, for ever, to say what they were already told. The wall also reorders
  // constantly as ranks change, and a reorder is not a change of membership, so
  // the ids are sorted before they are compared. The array handed to the caller
  // is rebuilt from the key for the same reason: it makes the key the single
  // source of both the comparison and the value.
  const showingKey = tiled.map((s) => s.id).sort().join(",");
  useEffect(() => {
    onShowing?.(showingKey ? showingKey.split(",") : []);
  }, [showingKey, onShowing]);

  return (
    <div className="odin-wall-grid" role="group" aria-label="Stations, worst first">
      {tiled.map((s) => (
        <StationTile
          key={s.id}
          station={s}
          selected={s.id === selectedId}
          onSelect={onSelect}
        />
      ))}

      {/* Not a button. There is nothing behind it to open, and a control that
          looks pressable and is not costs more than the count is worth — the
          stations it stands for are the ones nobody needs to look at. The count
          changes between polls, so this needs tabular figures in the stylesheet
          like every other changing number here: a digit that reflows is motion,
          and motion on this wall means something is wrong. */}
      {collapsed > 0 && (
        <div
          className="odin-wall-nominal neutral"
          title="Online, no open conditions, and reporting healthy"
        >
          {collapsed} nominal
        </div>
      )}
    </div>
  );
}
