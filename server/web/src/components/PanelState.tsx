import type { ReactNode } from "react";

/**
 * Four states, kept deliberately distinct.
 *
 *   loading    nothing has arrived yet, and not enough time has passed to call
 *              that a problem. Shows skeletons.
 *   live       data is arriving. Shows the panel.
 *   fault      data was expected and is not there - either it stopped, or it
 *              never started long after the link came up. Shows a red X.
 *   not-fitted no sensor is selected for this slot. Not a fault, and never
 *              becomes one however long you wait.
 *
 * Collapsing "no data yet" and "sensor is broken" into one state is the mistake
 * this exists to avoid. On a console watching unattended sites, an operator
 * needs to tell "still connecting" from "the weather station has failed", and a
 * spinner that never resolves communicates neither.
 *
 * `not-fitted` closes the other half of the same gap. A station with no
 * floodlight publishes no light stream at all, so the only evidence was an
 * absence — and an absence takes twelve seconds to become suggestive and then
 * resolves to a red X, which is the wrong answer twice: it wastes the wait, and
 * it calls a complete station broken. The station already says which slots are
 * configured in every health frame, so this is knowable on the first frame and
 * needs no timer.
 *
 * **Demo streams go stale like any other, and that is a correction.** Fault
 * used to be suppressed for them, reasoning that the simulator is the sensor so
 * a red X could only ever mean the simulator stopped. That was true when demo
 * meant the platform's own in-process simulator, which cannot fail on its own.
 *
 * It is not true now. `simulated` is a per-stream flag a *real station* stamps
 * on its own telemetry, so a demo sensor is a synthetic reading travelling from
 * a real box, over a real link, through a real broker — every one of which can
 * stop. Suppressing the fault meant a station whose credential had been revoked
 * went on showing green panels full of readings from before it was cut off,
 * which is the one thing this file exists to prevent.
 */
export type PanelStatus = "loading" | "live" | "fault" | "not-fitted";

/** Time from the link coming up to calling a silent sensor faulty. Generous:
 *  a station on a degraded backhaul can legitimately take a while to get its
 *  first frame through, and crying wolf trains operators to ignore the X. */
const FIRST_DATA_GRACE_MS = 12_000;

/**
 * @param lastSeen  epoch ms of the most recent frame for this panel, or null
 * @param since     epoch ms the stream became available, or null when it is not
 * @param staleAfterMs  how long without a frame counts as failed. Per panel,
 *                      because weather reports far less often than power does.
 * @param fitted    what the station says about the slot, or undefined until its
 *                  first health frame arrives.
 * @param stationOnline  what the *platform* says about the station: whether it
 *                  has been heard from recently. `undefined` before the station
 *                  list has loaded, which is genuinely not knowing.
 *
 * There is deliberately no `demo` parameter. It was here to suppress faults for
 * synthetic streams, and removing it rather than ignoring it is what stops that
 * being rewired: whether a reading is synthetic says nothing about whether it
 * is still arriving.
 */
export function panelStatus(
  lastSeen: number | null,
  since: number | null,
  staleAfterMs: number,
  fitted?: boolean,
  stationOnline?: boolean,
): PanelStatus {
  // Checked before anything time-based: an empty slot is a fact the station
  // states, not something to be inferred from silence. Only `false` counts —
  // `undefined` means no health frame has arrived yet, which is genuinely
  // still loading and must not be read as "nothing fitted".
  //
  // Deliberately not applied once data is arriving: a slot reported empty
  // while its stream is live is a contradiction, and showing the readings the
  // station is actually sending is the safer half of it.
  if (fitted === false && lastSeen === null) return "not-fitted";
  if (lastSeen === null) {
    if (since === null) return "loading";
    // **Nothing to wait for.** The grace period is for a station that is
    // talking and whose first frame is merely late — a degraded backhaul, a
    // sensor slow to start. It is not for one the platform has not heard from
    // at all: `online` is computed from `last_seen_at` and arrives with the
    // station list, before any telemetry, so waiting the full twelve seconds
    // showed skeletons to discover something already known on load. On a
    // console opened to a station that went offline hours ago, that is twelve
    // seconds of implying a connection is being established.
    //
    // Strictly `=== false`: undefined means the station list has not arrived
    // and nothing is known yet, which is the one case that really is loading.
    if (stationOnline === false) return "fault";
    return Date.now() - since > FIRST_DATA_GRACE_MS ? "fault" : "loading";
  }
  return Date.now() - lastSeen > staleAfterMs ? "fault" : "live";
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

      {status === "not-fitted" && (
        // Struck through like a fault, because the readings underneath are
        // equally not-a-measurement — but grey and without role="alert". This
        // is a complete station that simply does not have this sensor, and an
        // operator scanning for red must not find one here.
        <div className="panel-unfitted">
          <svg className="fault-x" viewBox="0 0 100 100" preserveAspectRatio="none"
               aria-hidden focusable="false">
            <line x1="4" y1="4" x2="96" y2="96" />
            <line x1="96" y1="4" x2="4" y2="96" />
          </svg>
          <div className="fault-text">
            <strong>{label}</strong>
            <span>Not fitted</span>
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
