import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { chime } from "../chime";
import type { FleetAdsb, FleetView, OdinAlert, PlatformMapConfig } from "../types";
import { useOdin } from "../useOdin";
import { useWatchAudio } from "../useWatchAudio";
import { AlertRail } from "./AlertRail";
import { FleetMap } from "./FleetMap";
import { OdinStatusBar } from "./OdinStatusBar";
import { StationPreviewDrawer } from "./StationPreviewDrawer";
import { TileWall } from "./TileWall";
import { TranscriptFeed } from "./TranscriptFeed";
import { WatchStrip } from "./WatchStrip";

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

  // The push feed, with the poll behind it. `link` is surfaced on the status
  // bar rather than kept internal: a wall that quietly drops from live to
  // polling still looks alive, and the operator only discovers the difference
  // at the moment they needed it to have been live.
  const { stations: pushed, alerts: pushedAlerts, link, lastFrameAt } = useOdin(true);

  /** The listening watch. Its own socket, deliberately: the wall's is one-way
   *  and identical for every viewer, and this one takes messages and reaches
   *  across tenant boundaries. Keeping the surface that can be talked into
   *  something separate from the one that cannot is worth a second connection. */
  const watch = useWatchAudio(true);

  /** Alerts arrive on the digest. This is the fallback for a dead socket, and
   *  the re-read after an operator acts so they see their own change at once
   *  rather than waiting out the next frame. */
  const [polledAlerts, setPolledAlerts] = useState<OdinAlert[]>([]);
  const refreshAlerts = useCallback(() => {
    void api.odinAlerts().then(setPolledAlerts).catch(() => {});
  }, []);
  useEffect(() => {
    if (link === "live") return;
    refreshAlerts();
    const id = window.setInterval(refreshAlerts, 15000);
    return () => window.clearInterval(id);
  }, [link, refreshAlerts]);

  const alerts = link === "live" && pushedAlerts ? pushedAlerts : polledAlerts;

  const polled = useMemo(() => fleet?.stations ?? [], [fleet]);
  /** Push when it is arriving, poll when it is not. Never a merge of the two:
   *  two sources of truth for one wall is how a station appears twice, or
   *  appears healthy in one and dark in the other. */
  const stations = link === "live" && pushed ? pushed : polled;
  const stationNames = useMemo(() => {
    const out: Record<string, string> = {};
    for (const s of stations) out[s.id] = s.name;
    return out;
  }, [stations]);

  /** Unacked criticals: the only thing on this wall that makes a noise. */
  const unackedCritical = useMemo(
    () => alerts.filter((a) => a.severity === "critical" && a.state === "open"),
    [alerts],
  );
  const unacked = useMemo(
    () => alerts.filter((a) => a.state === "open").length,
    [alerts],
  );

  // Sound on a NEW unacked critical, never on a re-render and never on one that
  // was already there. The chime rate-limits itself, so a comms outage that
  // raises a dozen stations at once makes one noise rather than twelve.
  const heard = useRef<Set<string>>(new Set());
  useEffect(() => {
    const fresh = unackedCritical.filter((a) => !heard.current.has(a.id));
    // Rebuilt rather than added to, so an alert that is acked and later
    // re-opened can sound again — and the set cannot grow without bound.
    heard.current = new Set(unackedCritical.map((a) => a.id));
    if (fresh.length > 0) chime();
  }, [unackedCritical]);

  // The count in the tab title, for the operator watching a second monitor.
  useEffect(() => {
    const base = "Percepta";
    document.title = unacked > 0 ? `(${unacked}) ${base}` : base;
    return () => {
      document.title = base;
    };
  }, [unacked]);

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
        unacked={unacked}
        // The freshest thing the wall has: a digest frame if the socket is up,
        // otherwise the last successful poll. The pip counts from whichever is
        // actually feeding the screen, which is the only honest answer to "how
        // old is this".
        lastPollAt={link === "live" ? lastFrameAt : lastPollAt}
        polling={error === null}
        error={
          error ??
          (link === "live"
            ? null
            : "live feed down — polling every 15s")
        }
      />

      <AlertRail
        alerts={alerts}
        stationNames={stationNames}
        onSelectStation={select}
        onChanged={refreshAlerts}
      />

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

      <WatchStrip stations={stations} watch={watch} />
      <TranscriptFeed guarded={watch.guarded} stations={stations} />

      <StationPreviewDrawer
        station={selected}
        onClose={close}
        onMaintenanceDeclared={refreshAlerts}
      />
    </div>
  );
}
