import type { ReactNode } from "react";

/**
 * Three states, kept deliberately distinct.
 *
 *   loading  nothing has arrived yet, and not enough time has passed to call
 *            that a problem. Shows skeletons.
 *   live     data is arriving. Shows the panel.
 *   fault    data was expected and is not there - either it stopped, or it
 *            never started long after the link came up. Shows a red X.
 *
 * Collapsing "no data yet" and "sensor is broken" into one state is the mistake
 * this exists to avoid. On a console watching unattended sites, an operator
 * needs to tell "still connecting" from "the weather station has failed", and a
 * spinner that never resolves communicates neither.
 *
 * Fault is suppressed in demo mode: the simulator is the sensor there, so a red
 * X would only ever mean the simulator stopped.
 */
export type PanelStatus = "loading" | "live" | "fault";

/** Time from the link coming up to calling a silent sensor faulty. Generous:
 *  a station on a degraded backhaul can legitimately take a while to get its
 *  first frame through, and crying wolf trains operators to ignore the X. */
const FIRST_DATA_GRACE_MS = 12_000;

/**
 * @param lastSeen  epoch ms of the most recent frame for this panel, or null
 * @param since     epoch ms the stream became available, or null when it is not
 * @param staleAfterMs  how long without a frame counts as failed. Per panel,
 *                      because weather reports far less often than power does.
 */
export function panelStatus(
  lastSeen: number | null,
  since: number | null,
  staleAfterMs: number,
  demo: boolean,
): PanelStatus {
  if (lastSeen === null) {
    if (demo || since === null) return "loading";
    return Date.now() - since > FIRST_DATA_GRACE_MS ? "fault" : "loading";
  }
  if (demo) return "live";
  return Date.now() - lastSeen > staleAfterMs ? "fault" : "live";
}

export function Skeleton({
  w = "100%",
  h = "1rem",
  radius = "0.25rem",
}: {
  w?: string;
  h?: string;
  radius?: string;
}) {
  return (
    <span
      className="skeleton"
      style={{ width: w, height: h, borderRadius: radius }}
      aria-hidden
    />
  );
}

/**
 * Wraps a panel body and overlays a loading shimmer or a fault marker.
 *
 * The real panel is ALWAYS rendered, including while loading. Every panel draws
 * dashes when it has no reading, so it already occupies its full height with no
 * data - and overlaying rather than substituting makes the loading and loaded
 * heights identical by construction.
 *
 * That is not a detail. The sidebar scales the whole stack to the viewport, so
 * the stack's natural height sets the size of every control in it. An earlier
 * version swapped in hand-built skeletons of approximately the right height;
 * "approximately" meant the bar visibly rescaled the moment data arrived, and
 * the approximation broke again for any user whose panel had an extra row.
 */
export function PanelState({
  status,
  label,
  children,
}: {
  status: PanelStatus;
  /** What failed, in the operator's words: "Weather station", "Solar array". */
  label: string;
  children: ReactNode;
}) {
  if (status === "live") return <>{children}</>;

  return (
    <div className="panel-state">
      {/* Dimmed and hidden from assistive tech: the readings underneath are
          stale or absent, and the overlay is what carries the meaning. */}
      <div className="panel-state-content" aria-hidden>
        {children}
      </div>

      {status === "loading" && (
        <div className="panel-shimmer" aria-busy="true" aria-label={`${label} loading`} />
      )}

      {status === "fault" && (
        <div className="panel-fault" role="alert">
          {/* The X spans the whole panel deliberately. A small warning icon
              reads as advisory; a panel struck through reads as "this is not
              telling you anything", which is the actual situation. */}
          <svg className="fault-x" viewBox="0 0 100 100" preserveAspectRatio="none"
               aria-hidden focusable="false">
            <line x1="4" y1="4" x2="96" y2="96" />
            <line x1="96" y1="4" x2="4" y2="96" />
          </svg>
          <div className="fault-text">
            <strong>{label}</strong>
            <span>No data</span>
          </div>
        </div>
      )}
    </div>
  );
}

/** Still used for the map and video preview, which have no dashed-out state of
 *  their own - an empty map is just an empty map. */
export function MapSkeleton() {
  return <div className="skeleton-map" />;
}


/**
 * A single reading the station has no source for, struck through in red.
 *
 * Distinct from the dashes a panel shows while it is waiting. A dash means "not
 * yet"; this means "there is no sensor for this, and there will not be a
 * number". Confusing the two is how an operator ends up waiting for a reading
 * that is never coming - or worse, reading a placeholder as a measurement.
 *
 * Sized to the text it replaces so a panel keeps its height, which the sidebar
 * scaling depends on.
 */
export function NoSource({ what }: { what: string }) {
  return (
    <span className="no-source" role="img" aria-label={`${what}: no sensor`} title={`No sensor for ${what}`}>
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden focusable="false">
        <line x1="8" y1="8" x2="92" y2="92" />
        <line x1="92" y1="8" x2="8" y2="92" />
      </svg>
    </span>
  );
}
