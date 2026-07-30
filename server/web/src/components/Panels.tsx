import { memo, useEffect, useRef } from "react";
import type { Capability, LightPayload, PowerPayload } from "../types";
import { DemoCamera } from "./DemoCamera";
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
  canPtz,
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
  canPtz: boolean;
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
      {canPtz && !compact && (
        <div className="ptz">
          <div className="ptz-pad">
            <button type="button" className="ptz-btn ptz-up" aria-label="Tilt up">
              ▲
            </button>
            <button type="button" className="ptz-btn ptz-left" aria-label="Pan left">
              ◀
            </button>
            <button type="button" className="ptz-btn ptz-home" aria-label="Home">
              ⌂
            </button>
            <button type="button" className="ptz-btn ptz-right" aria-label="Pan right">
              ▶
            </button>
            <button type="button" className="ptz-btn ptz-down" aria-label="Tilt down">
              ▼
            </button>
          </div>
          <div className="ptz-zoom">
            <button type="button" className="ptz-btn">−</button>
            <span>zoom</span>
            <button type="button" className="ptz-btn">+</button>
          </div>
        </div>
      )}
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
  const soc = power?.soc_pct ?? null;
  // Thresholds match the station's own duty-cycling policy: below 20% the site
  // starts shedding load, so the console should look worried before it does.
  const level = soc === null ? "" : soc < 20 ? " critical" : soc < 40 ? " low" : "";

  return (
    <div className="power">
      <div className="soc">
        <div className="soc-bar">
          <div
            className={`soc-fill${level}`}
            style={{ width: `${soc ?? 0}%` }}
          />
        </div>
        <div className="soc-value">
          {soc === null ? "--" : `${soc.toFixed(0)}%`}
        </div>
      </div>
      <div className="chart-head">
        <span className="muted">Battery level</span>
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
      </div>
      <BatteryChart samples={history} loading={historyLoading} />

      <dl className="stats">
        <div>
          <dt>Battery</dt>
          <dd>{power ? `${power.battery_v.toFixed(1)} V` : "--"}</dd>
        </div>
        <div>
          <dt>Solar in</dt>
          <dd>{power ? `${power.pv_w.toFixed(0)} W` : "--"}</dd>
        </div>
        <div>
          <dt>Load</dt>
          <dd>{power ? `${power.load_w.toFixed(0)} W` : "--"}</dd>
        </div>
        <div>
          <dt>Runtime</dt>
          <dd>
            {power
              ? power.runtime_h === null
                ? "charging"
                : `${power.runtime_h.toFixed(1)} h`
              : "--"}
          </dd>
        </div>
      </dl>
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
