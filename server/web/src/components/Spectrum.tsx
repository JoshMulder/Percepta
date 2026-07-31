import { useEffect, useRef } from "react";
import type { RadioPayload } from "../types";

/**
 * The airband spectrum, ported from Remote-Radio's `drawSpectrum`.
 *
 * Same scale (−110…−10 dBFS), same furniture — a dashed squelch line, a centre
 * marker on the tuned channel, a filled trace, and the window's edge
 * frequencies along the bottom — because those were arrived at against a real
 * receiver and there is nothing to improve by redrawing them from scratch.
 *
 * **Fixed size, on purpose.** The earlier attempt at this lived in the sidebar
 * and sized itself to its container, so every 1 Hz telemetry frame re-measured
 * a canvas, which re-triggered the fit that scales the whole console
 * (`useFitScale`). This one is 512×110 device-independent pixels and never
 * consults its parent, so nothing it does can move anything else on the page.
 * It also lives on the settings page rather than the sidebar, where there is
 * no fit to disturb in the first place.
 *
 * **Nothing is drawn from nothing.** No spectrum in the payload means the
 * station has not been asked for one, or has not answered yet — either way the
 * canvas keeps whatever it last drew rather than flashing empty, and the
 * caller shows the "waiting" state.
 */

/** Matches the squelch slider's range, so a threshold read off one lines up
 *  with the same number on the other. */
const MIN_DB = -110;
const MAX_DB = -10;

const WIDTH = 512;
const HEIGHT = 110;

export function Spectrum({
  radio,
  onTune,
}: {
  radio: RadioPayload | null;
  /** Click-to-tune, snapped by the caller to the channel grid. Omitted when
   *  the viewer may listen but not retune. */
  onTune?: (freqHz: number) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const spectrum = radio?.spectrum;
  const spanHz = radio?.span_hz;

  useEffect(() => {
    const element = canvas.current;
    if (!element || !spectrum || !spectrum.length) return;
    const ctx = element.getContext("2d");
    if (!ctx) return;

    // Drawn in CSS pixels; the backing store is scaled once at mount for the
    // display's density, so this arithmetic stays readable.
    const W = WIDTH;
    const H = HEIGHT;
    const y = (db: number) =>
      H - ((Math.max(MIN_DB, Math.min(MAX_DB, db)) - MIN_DB) / (MAX_DB - MIN_DB)) * H;

    ctx.clearRect(0, 0, W, H);

    // Squelch threshold: where the gate opens, against the trace that has to
    // cross it. The one line that makes the rest actionable.
    if (radio?.threshold_db !== undefined) {
      ctx.strokeStyle = "rgba(0, 160, 220, 0.5)";
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(0, y(radio.threshold_db));
      ctx.lineTo(W, y(radio.threshold_db));
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // The tuned channel sits at the centre by construction: the station
    // measures a window centred on it.
    ctx.strokeStyle = "rgba(221, 230, 237, 0.35)";
    ctx.beginPath();
    ctx.moveTo(W / 2, 0);
    ctx.lineTo(W / 2, H);
    ctx.stroke();

    const n = spectrum.length;
    const x = (i: number) => (i / (n - 1)) * W;

    ctx.beginPath();
    ctx.moveTo(0, H);
    for (let i = 0; i < n; i++) ctx.lineTo(x(i), y(spectrum[i]));
    ctx.lineTo(W, H);
    ctx.closePath();
    ctx.fillStyle = "rgba(53, 196, 138, 0.18)";
    ctx.fill();

    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      if (i === 0) ctx.moveTo(x(i), y(spectrum[i]));
      else ctx.lineTo(x(i), y(spectrum[i]));
    }
    ctx.strokeStyle = "#35c48a";
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Edge and centre frequencies. The window follows the tuning, so these
    // move as you tune and are the only thing saying where you are looking.
    const half = (spanHz ?? 240000) / 2;
    const centre = radio?.freq_hz ?? 0;
    ctx.fillStyle = "rgba(127, 146, 159, 0.9)";
    ctx.font = "10px ui-monospace, monospace";
    ctx.textBaseline = "bottom";
    ctx.textAlign = "left";
    ctx.fillText(((centre - half) / 1e6).toFixed(3), 4, H - 3);
    ctx.textAlign = "center";
    ctx.fillText((centre / 1e6).toFixed(3), W / 2, H - 3);
    ctx.textAlign = "right";
    ctx.fillText(((centre + half) / 1e6).toFixed(3), W - 4, H - 3);
  }, [spectrum, spanHz, radio?.threshold_db, radio?.freq_hz]);

  // Density scaling, once. Doing it per draw would reset the transform every
  // frame and compound it.
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const ratio = window.devicePixelRatio || 1;
    element.width = WIDTH * ratio;
    element.height = HEIGHT * ratio;
    element.getContext("2d")?.scale(ratio, ratio);
  }, []);

  return (
    <div className="spectrum-wrap">
      <canvas
        ref={canvas}
        className="spectrum"
        style={{ width: `${WIDTH}px`, height: `${HEIGHT}px` }}
        onClick={
          onTune && radio?.freq_hz !== undefined
            ? (e) => {
                const rect = e.currentTarget.getBoundingClientRect();
                const frac = (e.clientX - rect.left) / rect.width - 0.5;
                onTune(radio.freq_hz + frac * (spanHz ?? 240000));
              }
            : undefined
        }
        // Only a control when it can retune; otherwise it is a picture.
        role={onTune ? "button" : "img"}
        aria-label={
          onTune ? "Spectrum — click to tune" : "Airband spectrum"
        }
      />
      {!spectrum?.length && (
        <div className="spectrum-waiting">Waiting for the receiver…</div>
      )}
    </div>
  );
}
