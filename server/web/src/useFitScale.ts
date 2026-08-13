import { useCallback, useEffect, useLayoutEffect, useRef } from "react";

/**
 * Size the console so the sidebar's natural height exactly fills the viewport,
 * by setting the root font size — not by transforming anything.
 *
 * WHY NOT `transform: scale()`, which this used to do:
 *
 *   A transform is a zoom. The element is laid out at its authored size,
 *   rasterised, and the *pixels* are scaled. Text goes soft, hairline borders
 *   land on fractional pixels, and the whole subtree has to be re-rasterised at
 *   the composited scale on every repaint — which, with telemetry updating
 *   several times a second inside it, is a real and continuous cost.
 *
 *   Setting the root font size instead makes the browser lay everything out at
 *   the new size. Every dimension in the stylesheet is in rem, so it all follows
 *   exactly as before, but text is rendered at its true size and 1px borders
 *   stay one physical pixel. It is the difference between enlarging a photo of a
 *   page and setting the page in a larger type.
 *
 * Solving for the size is iterative because height is not perfectly linear in
 * font size — hairlines do not scale, and the viewer holds a fixed aspect — but
 * the relationship is close enough to converge in two or three passes.
 */

/** Bounds on the root size. Below the floor the numeric readouts stop being
 *  glanceable; above the ceiling a large display wastes space rather than
 *  using it. */
const MIN_ROOT_PX = 11;
/* Raised from 24 when the squelch, meter and receiver setup moved to settings.
   The stack got three rows shorter, the solve hit the old ceiling, and the
   difference showed as dead space below the last panel rather than as a larger
   console. The ceiling exists so a very large display does not render absurdly
   oversized text, not to stop the sidebar filling its own height. */
const MAX_ROOT_PX = 30;
/** Stop when the fit is this close; further passes are invisible. */
const TOLERANCE_PX = 2;
const MAX_PASSES = 4;

export function useFitScale({
  enabled = true,
  ready = true,
}: { enabled?: boolean; ready?: boolean } = {}) {
  const outerRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const frame = useRef(0);
  const busy = useRef(false);

  const measure = useCallback(() => {
    const outer = outerRef.current;
    const inner = innerRef.current;
    if (!outer || !inner || busy.current) return;

    busy.current = true;
    try {
      const root = document.documentElement;
      for (let pass = 0; pass < MAX_PASSES; pass += 1) {
        const available = outer.clientHeight;
        const natural = inner.offsetHeight;
        if (!available || !natural) return;
        if (Math.abs(available - natural) <= TOLERANCE_PX) break;

        const current = parseFloat(getComputedStyle(root).fontSize);
        const next = Math.max(
          MIN_ROOT_PX,
          Math.min(MAX_ROOT_PX, current * (available / natural)),
        );
        if (Math.abs(next - current) < 0.05) break;
        // Reading offsetHeight on the next pass forces the reflow, so each
        // measurement is against the new size rather than the old one.
        root.style.fontSize = `${next.toFixed(2)}px`;
      }
    } finally {
      busy.current = false;
    }
  }, []);

  const schedule = useCallback(() => {
    if (frame.current) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = 0;
      measure();
    });
  }, [measure]);

  // Before the first paint, so the console is never shown at the wrong size.
  useLayoutEffect(() => {
    if (!enabled) {
      document.documentElement.style.removeProperty("font-size");
      return;
    }
    if (!ready) return;
    measure();
    return () => {
      // The size solved here is an INLINE style on :root, so it outlives this
      // component unless it is taken off. It was not, and the console is not the
      // only view: the platform dashboard renders with no fit-scale of its own
      // and would inherit whatever the console had last solved for its sidebar.
      // Every rem in that view — the shared header height, type sizes, chart
      // gutters — then resolved against a number chosen for a different layout,
      // so the same markup came out a different size depending on which view had
      // been open first.
      //
      // Removing it hands the next view back the stylesheet's own clamp(), which
      // is what it was written against.
      document.documentElement.style.removeProperty("font-size");
    };
  }, [enabled, ready, measure]);

  useEffect(() => {
    if (!enabled || !ready) return;

    // Web fonts change text metrics when they swap in, which changes the
    // stack's height. One re-fit once they are in settles it.
    if (document.fonts?.status !== "loaded") {
      void document.fonts?.ready.then(schedule);
    }

    // Only the viewport is watched. Observing the content would re-fit on every
    // telemetry update, and a re-fit relays out the page — far more expensive
    // than the transform this replaced, and pointless: the panels are height
    // stable by design.
    window.addEventListener("resize", schedule);
    return () => {
      if (frame.current) cancelAnimationFrame(frame.current);
      frame.current = 0;
      window.removeEventListener("resize", schedule);
    };
  }, [enabled, ready, schedule]);

  return { outerRef, innerRef, refit: schedule };
}
