import { useMemo } from "react";
import { ChartFrame, gridLines } from "./ChartFrame";
import { niceScale, type Scale } from "../chartScale";

export interface WeatherSample {
  t: number;
  temp?: number | null;
  humidity?: number | null;
  pressure?: number | null;
  wind?: number | null;
}

/**
 * The weather series that get a trend line, each with its own unit and colour.
 *
 * Not one chart with four lines, the way the power flows are: watts share a
 * scale and °C, %, hPa and kt do not, so a shared axis would flatten three of
 * them to nothing. Each is its own sparkline against its own min–max instead,
 * which is what makes a two-degree overnight swing legible next to a pressure
 * that moved thirty hPa.
 */
const WEATHER_SERIES = [
  { key: "temp", label: "Temperature", unit: "°C", colour: "#e8b04b", digits: 1 },
  { key: "humidity", label: "Humidity", unit: "%", colour: "#35c48a", digits: 0 },
  { key: "pressure", label: "Pressure", unit: "hPa", colour: "#00a0dc", digits: 0 },
  { key: "wind", label: "Wind", unit: "kt", colour: "#b98cf0", digits: 0 },
] as const;

const W = 100;
/* Only a coordinate space: preserveAspectRatio="none" stretches it to whatever
   the CSS box is, and every trace here carries non-scaling-stroke, so the drawn
   height lives in .weather-row svg rather than in this number. */
const H = 24;
/* No PAD constant any more. It existed to stop a flat trace welding itself to
   an edge, which a scale rounded outward to whole gradations does for free -
   and unlike the fudge, the rounded ends are numbers the axis can label. */

interface Row {
  key: string;
  label: string;
  unit: string;
  colour: string;
  digits: number;
  line: string;
  area: string;
  last: number;
  min: number;
  max: number;
  scale: Scale;
}

/**
 * A station's weather over the window, one sparkline per fitted sensor.
 *
 * A sensor the station does not have is null across its whole column and drops
 * out entirely — a barometer-less site shows three rows, not a fourth pinned
 * flat — the same rule the power flow chart follows.
 */
export function WeatherHistory({
  samples,
  loading,
}: {
  samples: WeatherSample[];
  loading?: boolean;
}) {
  const rows = useMemo<Row[]>(() => {
    if (samples.length < 2) return [];
    const t0 = samples[0].t;
    const span = Math.max(1, samples[samples.length - 1].t - t0);
    const out: Row[] = [];
    for (const series of WEATHER_SERIES) {
      const pts: { t: number; v: number }[] = [];
      for (const sample of samples) {
        const v = sample[series.key];
        if (v === null || v === undefined || Number.isNaN(v)) continue;
        pts.push({ t: sample.t, v });
      }
      if (pts.length < 2) continue;
      let min = Infinity;
      let max = -Infinity;
      for (const p of pts) {
        if (p.v < min) min = p.v;
        if (p.v > max) max = p.v;
      }
      // Four gradations, not five: these rows are a third the height of the
      // power charts and any more reads as a hatch pattern rather than a scale.
      const scale = niceScale(min, max, 4);
      const y = (v: number) => H - scale.frac(v) * H;
      const xy = pts.map((p) => ({ x: ((p.t - t0) / span) * W, y: y(p.v) }));
      const line = xy
        .map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(2)} ${p.y.toFixed(2)}`)
        .join(" ");
      const area = `${line} L${xy[xy.length - 1].x.toFixed(2)} ${H} L${xy[0].x.toFixed(2)} ${H} Z`;
      out.push({
        ...series,
        line,
        area,
        last: pts[pts.length - 1].v,
        min,
        max,
        scale,
      });
    }
    return out;
  }, [samples]);

  if (rows.length === 0) {
    return (
      <div className="weather-history-empty muted">
        {loading ? "loading…" : "no history yet"}
      </div>
    );
  }

  return (
    <div className="weather-history">
      {rows.map((r, i) => (
        <div className="weather-row" key={r.key}>
          <div className="weather-row-head">
            <span className="weather-row-label" style={{ color: r.colour }}>
              {r.label}
            </span>
            <span className="weather-row-now">
              <b>{r.last.toFixed(r.digits)}</b> {r.unit}
            </span>
          </div>
          {/* The time axis only under the last row. Every row is drawn from the
              same samples over the same window, so four of them would be the
              same axis four times - but each row keeps its OWN gutter, because
              degrees, per cent, hPa and knots share no scale. */}
          <ChartFrame
            scale={r.scale}
            unit={r.unit}
            from={i === rows.length - 1 ? samples[0].t : undefined}
            to={i === rows.length - 1 ? samples[samples.length - 1].t : undefined}
          >
            <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden>
              {gridLines(r.scale, H)}
              <path
                className="weather-area"
                d={r.area}
                style={{ fill: r.colour, fillOpacity: 0.14 }}
              />
              <path
                className="weather-line"
                d={r.line}
                style={{ stroke: r.colour }}
              />
            </svg>
          </ChartFrame>
        </div>
      ))}
    </div>
  );
}
