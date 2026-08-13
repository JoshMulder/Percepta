/**
 * The time axis under a trend chart.
 *
 * Every chart in the console draws its trace into a `viewBox="0 0 100 H"` with
 * `preserveAspectRatio="none"`, which stretches the drawing to whatever width
 * the panel happens to be. That is right for a trace and fatal for type: an
 * SVG `<text>` in there comes out squashed or smeared by whatever the x-scale
 * turned out to be. So the axis is HTML laid out beneath the plot, the same way
 * the y labels are HTML positioned over it.
 *
 * The labels come from the samples' own timestamps rather than from the window
 * that was requested. A station that has been offline for six hours has no
 * samples for those hours, and an axis that confidently reads "now" at the right
 * edge would be captioning stale data with the current time. What is drawn is
 * what was recorded, so the axis says when that was.
 *
 * Times are the browser's local time, matching the transcripts and the alert
 * list. On a station in another timezone that is the operator's clock, not the
 * site's - worth knowing before reading dusk off a solar curve.
 */

/** Roughly a day and a half. Past this, a bare clock time stops being enough:
 *  two ticks reading "06:00" three days apart is not an axis. */
const DATE_ABOVE_MS = 36 * 3600 * 1000;

export function axisFormatter(spanMs: number): (t: number) => string {
  if (spanMs >= DATE_ABOVE_MS) {
    return (t) =>
      new Date(t).toLocaleDateString([], { day: "numeric", month: "short" });
  }
  return (t) =>
    new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** The full stamp, for the title attribute - the axis is terse, hovering is not. */
function exact(t: number): string {
  return new Date(t).toLocaleString();
}

export function ChartTimeAxis({
  from,
  to,
  ticks = 3,
}: {
  /** First sample's timestamp, epoch ms. */
  from: number;
  /** Last sample's timestamp, epoch ms. */
  to: number;
  /** Including both ends. Three fits a sidebar; a wider chart can take more. */
  ticks?: number;
}) {
  if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) return null;
  const fmt = axisFormatter(to - from);
  const n = Math.max(2, ticks);
  const marks = Array.from(
    { length: n },
    (_, i) => from + ((to - from) * i) / (n - 1),
  );

  return (
    <div className="chart-x">
      {marks.map((t, i) => (
        <span
          key={i}
          className={`chart-x-tick${i === 0 ? " first" : ""}${
            i === n - 1 ? " last" : ""
          }`}
          title={exact(t)}
        >
          {fmt(t)}
        </span>
      ))}
    </div>
  );
}
