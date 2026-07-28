import { useEffect, useRef } from "react";

/**
 * Synthetic camera view for demo mode.
 *
 * Drawn on a canvas rather than shipped as an image or a video file: it costs
 * nothing to download, works with no network at all, and can react to state -
 * which is the point of it. Turning the floodlight on visibly lights the
 * foreground, so a demo can show the command path working end to end instead of
 * only a number changing in a panel.
 *
 * Deliberately looks like a fixed security camera at night: a static frame, a
 * burnt-in timestamp, sensor grain, and nothing that could be mistaken for real
 * footage of a real place.
 */
export function DemoCamera({
  lightOn,
  compact,
}: {
  lightOn: boolean;
  compact?: boolean;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  // Read inside the animation loop without restarting it on every change.
  const lightRef = useRef(lightOn);
  lightRef.current = lightOn;

  useEffect(() => {
    const canvas = canvasRef.current;
    const parent = canvas?.parentElement;
    if (!canvas || !parent) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let raf = 0;
    let last = 0;
    // ~12 fps. This is a background panel on a console that may be left open for
    // a shift; there is no reason to spend a full refresh rate on grain.
    const FRAME_MS = 1000 / 12;
    // Deterministic star field, so the sky does not reshuffle every resize.
    const stars = Array.from({ length: 90 }, (_, i) => ({
      x: ((i * 2654435761) % 1000) / 1000,
      y: (((i + 7) * 40503) % 1000) / 1000,
      m: 0.3 + (((i * 97) % 100) / 100) * 0.7,
    }));

    let w = 0;
    let h = 0;
    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      w = rect.width;
      h = rect.height;
      // Only the backing store is set here. Layout size stays with the
      // stylesheet (100% of the panel), because writing pixel sizes back into
      // the element would make this canvas a participant in the sidebar's fit
      // measurement - which is exactly the loop that removing the spectrum
      // display just fixed.
      canvas.width = Math.max(1, Math.round(w * dpr));
      canvas.height = Math.max(1, Math.round(h * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(parent);

    const draw = (now: number) => {
      raf = requestAnimationFrame(draw);
      if (now - last < FRAME_MS) return;
      last = now;
      if (!w || !h) return;

      const lit = lightRef.current;
      const horizon = h * 0.58;

      // Sky.
      const sky = ctx.createLinearGradient(0, 0, 0, horizon);
      sky.addColorStop(0, "#05080d");
      sky.addColorStop(1, lit ? "#16202c" : "#0b131d");
      ctx.fillStyle = sky;
      ctx.fillRect(0, 0, w, horizon);

      for (const s of stars) {
        const y = s.y * horizon;
        ctx.globalAlpha = s.m * (lit ? 0.25 : 0.7) * (0.75 + Math.random() * 0.25);
        ctx.fillStyle = "#cfe2f0";
        ctx.fillRect(s.x * w, y, 1, 1);
      }
      ctx.globalAlpha = 1;

      // Ridge line behind, hills in front.
      const ridge = (offset: number, amp: number, fill: string) => {
        ctx.beginPath();
        ctx.moveTo(0, horizon);
        for (let x = 0; x <= w; x += 6) {
          const t = x / w;
          const y =
            horizon -
            amp *
              (Math.sin(t * 6.2 + offset) * 0.5 +
                Math.sin(t * 13.7 + offset * 2) * 0.3 +
                0.5);
          ctx.lineTo(x, y);
        }
        ctx.lineTo(w, horizon);
        ctx.closePath();
        ctx.fillStyle = fill;
        ctx.fill();
      };
      ridge(1.2, h * 0.1, "#0a1119");
      ridge(3.4, h * 0.06, "#070c12");

      // Ground. The floodlight throws a pool of warm light into the near field;
      // unlit, it is close to black, as it would be.
      ctx.fillStyle = lit ? "#141311" : "#05070a";
      ctx.fillRect(0, horizon, w, h - horizon);

      if (lit) {
        const pool = ctx.createRadialGradient(
          w * 0.5, h * 1.02, h * 0.05,
          w * 0.5, h * 1.02, h * 0.75,
        );
        pool.addColorStop(0, "rgba(255, 214, 150, 0.42)");
        pool.addColorStop(0.55, "rgba(255, 198, 120, 0.14)");
        pool.addColorStop(1, "rgba(255, 190, 110, 0)");
        ctx.fillStyle = pool;
        ctx.fillRect(0, horizon - h * 0.06, w, h - horizon + h * 0.06);

        // A fence line, visible only once something is lighting it.
        ctx.strokeStyle = "rgba(210, 190, 160, 0.28)";
        ctx.lineWidth = 1;
        for (let i = 0; i <= 9; i += 1) {
          const x = (i / 9) * w;
          ctx.beginPath();
          ctx.moveTo(x, horizon + h * 0.06);
          ctx.lineTo(x, horizon + h * 0.22);
          ctx.stroke();
        }
        ctx.beginPath();
        ctx.moveTo(0, horizon + h * 0.1);
        ctx.lineTo(w, horizon + h * 0.1);
        ctx.stroke();
      }

      // Sensor grain. Sparse - full-frame per-pixel noise is expensive and does
      // not look any more like a camera than this does.
      ctx.globalAlpha = lit ? 0.05 : 0.09;
      ctx.fillStyle = "#9fb4c4";
      for (let i = 0; i < (w * h) / 900; i += 1) {
        ctx.fillRect(Math.random() * w, Math.random() * h, 1, 1);
      }
      ctx.globalAlpha = 1;

      if (!compact) {
        const stamp = new Date().toLocaleString("en-NZ", {
          hour12: false,
          year: "numeric", month: "2-digit", day: "2-digit",
          hour: "2-digit", minute: "2-digit", second: "2-digit",
        });
        ctx.font = "11px ui-monospace, monospace";
        ctx.textAlign = "right";
        ctx.fillStyle = "rgba(0,0,0,0.55)";
        ctx.fillText(stamp, w - 7, h - 7);
        ctx.fillStyle = "rgba(226, 236, 243, 0.85)";
        ctx.fillText(stamp, w - 8, h - 8);

        ctx.textAlign = "left";
        ctx.fillStyle = "rgba(226, 236, 243, 0.6)";
        ctx.fillText("CAM-01", 8, h - 8);
      }
    };

    raf = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
    };
  }, [compact]);

  return <canvas ref={canvasRef} className="demo-camera" />;
}
