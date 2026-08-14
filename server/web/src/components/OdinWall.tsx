import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FleetAdsb, FleetView, PlatformMapConfig } from "../types";
import { AlertRail } from "./AlertRail";
import { FleetMap } from "./FleetMap";
import { OdinStatusBar } from "./OdinStatusBar";
import { StationPreviewDrawer } from "./StationPreviewDrawer";
import { TileWall } from "./TileWall";

/**
 * ODIN: every station, every organisation, one screen.
 *
 * This is not the customer's console with more rows in it. The console is one
 * operator watching one site they own; this is a watch position in a command
 * centre, staffed for a shift, mostly not touched. The difference decides the
 * whole layout: a queue on the left that ages upward, tiles in the middle sorted
 * so trouble is always in the top-left corner, and the map on the right — and
 * nothing anywhere that has to be clicked for the screen to be worth having.
 *
 * Three things are deliberate and easy to undo by accident.
 *
 * COLOUR IS RATIONED. Green is the liveness pip and nothing else. A wall where
 * healthy stations are green is a wall of green, and within one shift that
 * teaches the operator colour carries no information — after which the amber you
 * needed them to catch is just another light. Fine is the absence of colour.
 *
 * THE POLL'S HONESTY IS PART OF THE PRODUCT. The view this replaces caught its
 * poll errors, kept the last fleet on screen, and went on rendering it with
 * complete confidence. A wall that degrades silently is worse than one that goes
 * blank, because the operator's certainty is highest exactly when the data is
 * worst. So the moment of the last SUCCESSFUL poll is tracked here and handed to
 * the status bar, which counts the staleness up on its own clock — the failure
 * case is precisely the one where no new props arrive to re-render it.
 *
 * DRILLING IN NEVER COSTS THE WALL. The preview drawer slides over the right
 * column; it is not a route, not a modal, and above all not an organisation
 * switch. Switching org revokes the session server-side and reloads the page
 * (api/auth.py), which would destroy the thing the operator is watching in order
 * to glance at one site in it.
 */
export function OdinWall({
  fleet,
  adsb,
  mapConfig,
  lastPollAt,
  error,
}: {
  fleet: FleetView | null;
  adsb: FleetAdsb | null;
  mapConfig: PlatformMapConfig | null;
  /** When the fleet was last read SUCCESSFULLY. Paired with `error` this is the
   *  pip: "there was an error" without "and this is from 47 seconds ago" is not
   *  something an operator can act on. Owned by the parent, which owns the poll
   *  — the wall polling for itself would double every request on a screen that
   *  is open all day beside a dashboard already asking the same questions. */
  lastPollAt: number | null;
  error: string | null;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const stations = useMemo(() => fleet?.stations ?? [], [fleet]);
  const events = useMemo(() => fleet?.recent_events ?? [], [fleet]);

  const selected = useMemo(
    () => stations.find((s) => s.id === selectedId) ?? null,
    [stations, selectedId],
  );

  /** Selecting from the rail names a station id that may not be on the wall yet
   *  — the events feed and the station list are two queries and can disagree by
   *  one poll. Selecting it anyway is right: the drawer renders nothing until
   *  the station appears, rather than the click doing nothing at all. */
  const select = useCallback((id: string) => setSelectedId(id), []);
  const close = useCallback(() => setSelectedId(null), []);

  // Escape closes the drawer from anywhere on the wall, including from a tile
  // that still holds focus after being clicked.
  const drawerOpen = selected !== null;
  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  return (
    <div className="odin">
      <OdinStatusBar
        stations={stations}
        unacked={events.length}
        lastPollAt={lastPollAt}
        polling={error === null}
        error={error}
      />

      <AlertRail events={events} onSelectStation={select} />

      <TileWall stations={stations} selectedId={selectedId} onSelect={select} />

      <div className="odin-map">
        {mapConfig ? (
          <FleetMap
            config={mapConfig}
            stations={stations}
            aircraft={adsb?.aircraft ?? []}
          />
        ) : (
          <div className="odin-rail-empty">Map unavailable</div>
        )}
      </div>

      <StationPreviewDrawer station={selected} onClose={close} />
    </div>
  );
}
