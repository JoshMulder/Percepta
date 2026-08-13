import type { ReactNode } from "react";
import { ChartTimeAxis } from "./ChartTimeAxis";
import { tickFormat, type Scale } from "../chartScale";

/**
 * The furniture around a trend chart: a y axis in its own gutter, gradations
 * across the plot, and the time axis beneath.
 *
 * The y labels used to be absolutely positioned *inside* the plot with a text
 * shadow behind them, so the trace ran under the numbers and the numbers sat on
 * the data. That is why these panes read as cramped: nothing had room, because
 * everything was in the same box. The gutter costs about three characters of
 * width and gives the plot its whole area back.
 *
 * Gridlines are drawn by the caller inside its own `<svg>` rather than here, so
 * they land behind the trace instead of over it — see `gridLines`. Only the
 * *text* is HTML, and that is not a stylistic choice: every plot in this console
 * is `preserveAspectRatio="none"` and stretched to the panel width, which would
 * squash any `<text>` drawn inside it by whatever the x-scale happened to be.
 */
export function ChartFrame({
  scale,
  unit,
  from,
  to,
  children,
  footer,
  className = "",
}: {
  scale: Scale;
  /** Appended to the topmost gradation only. Repeating "W" down the axis is
   *  four times the ink to say the thing once. */
  unit?: string;
  /** Epoch ms of the first and last sample, for the time axis. Omit on a chart
   *  whose x is not time, and no axis is drawn. */
  from?: number;
  to?: number;
  /** The plot itself: an `<svg>`, drawn in whatever viewBox the caller likes. */
  children: ReactNode;
  /** Legend or readout, below the time axis. */
  footer?: ReactNode;
  className?: string;
}) {
  const fmt = tickFormat(scale.ticks);
  return (
    <div className={`chart-frame ${className}`.trim()}>
      <div className="chart-body">
        <div className="chart-gutter" aria-hidden="true">
          {scale.ticks.map((t, i) => (
            <span
              key={t.value}
              className="chart-gutter-tick"
              // From the bottom, because that is how the scale is defined and
              // how the SVG coordinate is derived. Deriving the label's position
              // and the gridline's from the same fraction is what stops them
              // drifting apart at awkward ranges.
              style={{ bottom: `${t.frac * 100}%` }}
            >
              {fmt(t.value)}
              {unit && i === scale.ticks.length - 1 ? ` ${unit}` : ""}
            </span>
          ))}
        </div>
        <div className="chart-plot">{children}</div>
      </div>
      {from !== undefined && to !== undefined && (
        <div className="chart-x-wrap">
          <ChartTimeAxis from={from} to={to} />
        </div>
      )}
      {footer}
    </div>
  );
}

/**
 * The horizontal gradations, as SVG lines for the caller to render *before* its
 * trace so the data sits on top of them.
 *
 * `vector-effect: non-scaling-stroke` in the stylesheet keeps these hairlines
 * whatever the plot is stretched to; without it a horizontal rule in a box
 * squashed to 7rem comes out several pixels thick.
 *
 * @param height the caller's viewBox height, so this works whatever coordinate
 *   space it chose.
 */
export function gridLines(scale: Scale, height: number, width = 100): ReactNode {
  return scale.ticks.map((t) => (
    <line
      key={t.value}
      className="chart-grid"
      x1={0}
      x2={width}
      y1={height * (1 - t.frac)}
      y2={height * (1 - t.frac)}
    />
  ));
}
