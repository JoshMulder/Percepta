import { useEffect, useRef, useState } from "react";

import { useVideoStream } from "../useVideoStream";

/**
 * The deliberate look. One station's camera, on request, for five minutes.
 *
 * DELIBERATE IS THE WHOLE DESIGN, not a limitation. Video is the most expensive
 * thing on a station's uplink by an order of magnitude, and the relay that
 * carries it is in-process — so concurrent video is the single thing that would
 * force this deployment off one worker, and moving those relays out of process
 * is a separate project. A mosaic of camera tiles is therefore not a feature
 * this can grow into by accident; it is a different system.
 *
 * So: never on a tile, never automatic, one at a time, and it stops on its own.
 *
 * THE AUTO-STOP FLIPS `enabled`, NOT VISIBILITY. Hiding the <video> element
 * would leave the socket open and the camera streaming into a page nobody is
 * looking at — which is exactly the failure the five-minute limit exists to
 * prevent, arriving by a different route. `useVideoStream` tears down on
 * `enabled: false`, so that is the switch.
 *
 * The <video> element is rendered UNCONDITIONALLY, because the hook reads
 * `video.current` once when it starts: mounting the element at the same moment
 * as enabling the stream is a race the hook loses about half the time, and it
 * loses it silently — no picture, no error.
 */

/** Long enough for a look, short enough that a forgotten window costs pennies.
 *  It is also comfortably inside the SourceBuffer quota the hook's 30s trim
 *  exists to stay under, so the two limits never argue. */
const AUTO_STOP_MS = 5 * 60_000;

export function DrawerVideo({ stationId }: { stationId: string }) {
  const video = useRef<HTMLVideoElement>(null);
  const [live, setLive] = useState(false);
  const [remaining, setRemaining] = useState(0);

  // Reset when the drawer moves to another station. The body remounts on a
  // station change but this component does not, so without this an operator who
  // opened one camera would find the next station's drawer already streaming.
  useEffect(() => {
    setLive(false);
    setRemaining(0);
  }, [stationId]);

  useEffect(() => {
    if (!live) return;
    const stopAt = Date.now() + AUTO_STOP_MS;
    setRemaining(AUTO_STOP_MS);
    const id = window.setInterval(() => {
      const left = stopAt - Date.now();
      setRemaining(left);
      if (left <= 0) setLive(false);
    }, 1000);
    return () => window.clearInterval(id);
  }, [live, stationId]);

  const state = useVideoStream(video, stationId, live);

  return (
    <div className="odin-drawer-video">
      <div className="odin-drawer-video-head">
        <button
          type="button"
          className={`odin-chan-act${live ? " on" : ""}`}
          onClick={() => setLive((on) => !on)}
          title={
            live
              ? "Stop the stream and release the station's uplink"
              : "Open this station's camera. Uses the site's uplink."
          }
        >
          {live ? "stop video" : "live video"}
        </button>
        {/* Said on the button's own line rather than buried in a tooltip: the
            cost is the reason this is a decision and not a default. */}
        {!live && <span className="odin-watch-note">uses the site uplink</span>}
        {live && (
          <span className="odin-watch-note">
            {state === "playing"
              ? `stops in ${Math.max(0, Math.ceil(remaining / 1000))}s`
              : state}
          </span>
        )}
      </div>
      <video
        ref={video}
        className={`odin-drawer-video-el${live ? "" : " idle"}`}
        autoPlay
        muted
        playsInline
      />
    </div>
  );
}
