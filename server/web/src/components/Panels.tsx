import { memo, useEffect, useRef, useState } from "react";
import type { Capability, LightPayload, PowerPayload } from "../types";
import { DemoCamera } from "./DemoCamera";
import { PowerFlow } from "./PowerFlow";
import { IconChart } from "./Icons";
import type { StreamState } from "../useVideoStream";
import type { VideoPayload } from "../types";
import {
  BatteryChart,
  SOC_WINDOWS,
  type SocSample,
  type SocWindowKey,
} from "./BatteryChart";

export function has(caps: Capability[], capability: Capability): boolean {
  return caps.includes(capability);
}

/** Shown where a control would be if the user held the capability. Saying
 *  nothing at all makes an operator wonder whether the feature is broken; this
 *  makes it clear the console is working and they simply are not cleared. */
export function NotPermitted({ what }: { what: string }) {
  return <div className="not-permitted">No access to {what}</div>;
}

/* ---------------------------------------------------------------- video --- */

function VideoPanelInner({
  compact,
  streaming,
  frame,
  online,
  demo,
  lightOn,
  videoEl,
  streamState,
}: {
  /** Kept for callers; the stream itself is owned by the console now, because
   *  it must outlive this component being remounted by a layout swap. */
  stationId?: string | null;
  live?: boolean;
  compact?: boolean;
  streaming: boolean;
  /** Latest frame from the station, if any. */
  frame?: VideoPayload | null;
  /** Held in the contract and still granted per station, but no longer drawn:
   *  the pad moved nothing, because no fitted camera has a mount. It comes
   *  back when one does, and the capability keeps meaning what it meant. */
  canPtz?: boolean;
  online: boolean;
  /** Demo deployments render a synthetic camera view rather than an empty
   *  placeholder, so the panel shows what it is for. It is drawn, not footage:
   *  nothing here could be mistaken for a real place. */
  demo?: boolean;
  lightOn?: boolean;
  /** The one `<video>` element for the whole console, created outside React and
   *  adopted here. Swapping the map and the camera remounts this component, and
   *  a `<video>` React owns would be destroyed and rebuilt with it - a new
   *  ticket, a new socket, the station asked to stop and start encoding, and
   *  three seconds of black for what is visually a resize. An element the
   *  parent owns is merely re-parented, which a browser does without
   *  interrupting playback. */
  videoEl?: HTMLVideoElement | null;
  streamState?: StreamState;
}) {
  const surfaceRef = useRef<HTMLDivElement>(null);
  const showingLive = streamState === "playing";

  // Adopt the shared element into whichever surface is currently on screen.
  useEffect(() => {
    const surface = surfaceRef.current;
    if (!surface || !videoEl) return;
    if (videoEl.parentElement !== surface) surface.insertBefore(videoEl, surface.firstChild);
  });

  // The class is set imperatively for the same reason the element is: React
  // does not render it, so it cannot keep its className in sync.
  useEffect(() => {
    if (videoEl) videoEl.className = `video-live-el${showingLive ? "" : " hidden"}`;
  }, [videoEl, showingLive]);

  return (
    <div className="video-panel">
      <div className="video-surface" ref={surfaceRef}>
        {showingLive ? (
          <div className="video-live">
            <span className="video-live-dot" />
            live
          </div>
        ) : frame?.available === false ? (
          // A camera that is not fitted is not a camera that has failed, and an
          // operator does different things about each.
          <div className="video-idle">
            <span className="no-source-badge">NO CAMERA</span>
            <span>{frame.unavailable_reason ?? "No camera on this station"}</span>
          </div>
        ) : frame?.jpeg ? (
          <>
            <img
              className="video-frame"
              src={`data:image/jpeg;base64,${frame.jpeg}`}
              alt="Camera view"
            />
            <div className="video-live">
              <span className="video-live-dot" />
              {compact ? "live" : <FrameAge capturedAt={frame.captured_at} />}
            </div>
          </>
        ) : demo ? (
          <>
            <DemoCamera lightOn={lightOn ?? false} compact={compact} />
            <div className="video-live demo">
              <span className="video-live-dot" />
              {compact ? "sim" : "simulated feed"}
            </div>
          </>
        ) : streaming ? (
          <div className="video-live">
            <span className="video-live-dot" />
            live
          </div>
        ) : (
          <div className="video-idle">
            {online ? "No video stream attached" : "Station offline"}
          </div>
        )}
      </div>
    </div>
  );
}

/* ----------------------------------------------------------- floodlight --- */

function FloodlightPanelInner({
  light,
  caps,
  onToggle,
  pending,
}: {
  light: LightPayload | null;
  caps: Capability[];
  onToggle: (on: boolean) => void;
  pending: boolean;
}) {
  const allowed = has(caps, "light.control");
  const on = light?.on ?? false;

  return (
    <div className="floodlight">
      <div className="flood-state">
        <span className={`flood-lamp${on ? " on" : ""}`} />
        <span className="flood-label">{on ? "ON" : "OFF"}</span>
      </div>
      <button
        type="button"
        className={`btn flood-btn${on ? " on" : ""}`}
        disabled={!allowed || pending}
        onClick={() => onToggle(!on)}
      >
        {pending ? "…" : on ? "Turn off" : "Turn on"}
      </button>
      {/* Reserved, not conditional - see the note in RadioPanel. */}
      <span className={`flood-denied${allowed ? " hidden" : ""}`}>no access</span>
    </div>
  );
}

/* ----------------------------------------------------------------- power -- */

function PowerPanelInner({
  power,
  history,
  historyLoading,
  windowKey,
  onWindowChange,
}: {
  power: PowerPayload | null;
  history: SocSample[];
  historyLoading?: boolean;
  windowKey: SocWindowKey;
  onWindowChange: (key: SocWindowKey) => void;
}) {
  const [detailOpen, setDetailOpen] = useState(false);

  return (
    <div className="power">
      {/* No state-of-charge bar. It was a second rendering of a number that
          now sits on the battery in the diagram, where it is next to the
          thing it describes, and removing it gave the diagram the room it
          needed to be legible at a glance. */}
      <button
        type="button"
        className="power-detail-btn"
        onClick={() => setDetailOpen(true)}
        title="Battery history"
        aria-label="Battery history"
      >
        <IconChart />
      </button>

      <PowerFlow power={power} />

      {detailOpen && (
        <div
          className="power-detail-scrim"
          onClick={() => setDetailOpen(false)}
          role="presentation"
        >
          <div
            className="power-detail"
            role="dialog"
            aria-label="Battery history"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="power-detail-head">
              <h4>
                Battery level
                {power && (
                  <span className="power-detail-v">
                    {power.battery_v.toFixed(1)} V
                    {/* Null while charging, and the diagram says so already.
                        A runtime figure is a thing you go looking for, not a
                        thing you glance at, which is why it lives here. */}
                    {power.runtime_h !== null && (
                      <> · {power.runtime_h.toFixed(1)} h left</>
                    )}
                  </span>
                )}
              </h4>
              <div className="window-switch" role="group" aria-label="Chart period">
                {SOC_WINDOWS.map((w) => (
                  <button
                    key={w.key}
                    type="button"
                    className={`window-btn${w.key === windowKey ? " active" : ""}`}
                    onClick={() => onWindowChange(w.key)}
                  >
                    {w.label}
                  </button>
                ))}
              </div>
              <button
                type="button"
                className="contact-close"
                onClick={() => setDetailOpen(false)}
                aria-label="Close"
              >
                ×
              </button>
            </div>
            <BatteryChart samples={history} loading={historyLoading} />
          </div>
        </div>
      )}
    </div>
  );
}

/* Memoised for the same reason as the other panels - see WeatherPanel. */
export const PowerPanel = memo(PowerPanelInner);
export const FloodlightPanel = memo(FloodlightPanelInner);
export const VideoPanel = memo(VideoPanelInner);


/**
 * How old the picture is, not when it arrived.
 *
 * On a link that buffers and drops, an operator looking at a still image will
 * assume it is current unless told otherwise - and a frozen frame from four
 * minutes ago is exactly the thing that gets acted on wrongly. The station
 * timestamps at capture for this reason.
 */
function FrameAge({ capturedAt }: { capturedAt?: string }) {
  if (!capturedAt) return <>live</>;
  const ms = Date.now() - new Date(capturedAt).getTime();
  if (Number.isNaN(ms)) return <>live</>;
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 5) return <>live</>;
  if (seconds < 90) return <>{seconds}s old</>;
  return <>{Math.round(seconds / 60)} min old</>;
}
