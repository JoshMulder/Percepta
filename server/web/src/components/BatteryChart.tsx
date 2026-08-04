import { useMemo } from "react";

export interface SocSample {
  t: number;
  soc: number;
  // The power flows, in watts, recorded at the same minute as the SoC. Null
  // where the source is not fitted (no grid, no generator); the flow chart
  // leaves an all-null series off entirely rather than drawing it flat at zero.
  pv?: number | null;
  load?: number | null;
  mains?: number | null;
  gen?: number | null;
}

/** The four flows drawn under the state of charge, in the order they read in
 *  the legend, each matched to the colour its source has in the power diagram
 *  (PowerFlow.tsx / styles.css) so the two are read as the same thing. */
const POWER_SERIES = [
  { key: "load", label: "Load", colour: "#d7dee7" },
  { key: "pv", label: "Solar", colour: "#e8b04b" },
  { key: "mains", label: "AC In", colour: "#00a0dc" },
  { key: "gen", label: "Generator", colour: "#b98cf0" },
] as const;

/** Selectable windows. These come from the server's recorded history, not a
 *  browser buffer, so they can outlast the tab - `hours` is what the API takes. */
export const SOC_WINDOWS = [
  { key: "12h", label: "12h", hours: 12 },
  { key: "1d", label: "1d", hours: 24 },
  { key: "7d", label: "7d", hours: 168 },
] as const;

export type SocWindowKey = (typeof SOC_WINDOWS)[number]["key"];

/**
 * Battery state of charge over time.
 *
 * Drawn as an area rather than a line because the question an operator asks of
 * it is "is it going up or down, and how close to the floor" - a shape answers
 * that faster than a stroke does. The 20% shed line is drawn in because that is
 * where the station starts dropping load, so it is the number that matters
 * rather than zero.
 */
export function BatteryChart({
  samples,
  loading,
}: {
  samples: SocSample[];
  loading?: boolean;
}) {
  const W = 100;
  const H = 34;
  const SHED_PCT = 20;

  const { area, line, first, last } = useMemo(() => {
    const points = samples;
    if (points.length < 2) {
      return { area: "", line: "", first: null, last: null };
    }
    const t0 = points[0].t;
    const span = Math.max(1, points[points.length - 1].t - t0);
    const xy = points.map((s) => ({
      x: ((s.t - t0) / span) * W,
      y: H - (Math.max(0, Math.min(100, s.soc)) / 100) * H,
    }));
    const line = xy.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(" ");
    return {
      area: `${line} L${W} ${H} L0 ${H} Z`,
      line,
      first: points[0].soc,
      last: points[points.length - 1].soc,
    };
  }, [samples]);

  const trend = last !== null && first !== null ? last - first : 0;
  const shedY = H - (SHED_PCT / 100) * H;

  /*
   * The chart and its footer are always rendered, even before there is a curve
   * to draw. An earlier version returned a bare "Collecting…" box instead, which
   * was shorter by exactly the height of the footer - so about ten seconds after
   * load, once a second sample arrived, the panel grew and the whole sidebar
   * quietly rescaled. Every element in this bar has to hold its height from the
   * first paint.
   */
  return (
    <div className="battery-chart">
      <div className="chart-plot">
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden>
          <line className="chart-shed" x1="0" y1={shedY} x2={W} y2={shedY} />
          {line && (
            <>
              <path className="chart-area" d={area} />
              <path className="chart-line" d={line} />
            </>
          )}
        </svg>
        {/* Fixed 0–100 scale, with the 20% shed line called out — the number
            that actually matters, since that is where the station starts
            dropping load. */}
        <span className="chart-y top">100%</span>
        <span className="chart-y shed">{SHED_PCT}%</span>
        <span className="chart-y bot">0%</span>
      </div>
      <div className="chart-foot">
        {line ? (
          <span className={trend >= 0 ? "trend up" : "trend down"}>
            {trend >= 0 ? "▲" : "▼"} {Math.abs(trend).toFixed(1)}%
          </span>
        ) : (
          <span className="muted">{loading ? "loading…" : "—"}</span>
        )}
        <span className={`muted${!line && !loading ? "" : " hidden"}`}>
          no history yet
        </span>
      </div>
    </div>
  );
}

/**
 * The power flows behind the battery, over the same window.
 *
 * State of charge is the answer to "will it last"; this is the answer to "why"
 * — solar tailing off at dusk, the load climbing, the generator picking up the
 * shortfall. Drawn as plain lines, not areas: four filled bands would occlude
 * each other, and here the crossings (solar falling below load, say) are the
 * whole point.
 *
 * A source that is not fitted is null throughout its column and drops out of
 * both the chart and the legend — a site with no grid shows three lines, not a
 * fourth pinned to the floor pretending an AC input exists. The scale is shared
 * across every drawn series so their heights are comparable, and anchored by
 * the watt figures in the legend rather than a drawn axis.
 */
export function PowerFlowHistory({
  samples,
  loading,
}: {
  samples: SocSample[];
  loading?: boolean;
}) {
  const W = 100;
  const H = 34;

  const { drawn, peak } = useMemo(() => {
    if (samples.length < 2) return { drawn: [], peak: 0 };
    const t0 = samples[0].t;
    const span = Math.max(1, samples[samples.length - 1].t - t0);
    let max = 0;
    const raw = POWER_SERIES.map((s) => {
      const pts = samples.map((sample) => {
        const v = sample[s.key];
        if (v === null || v === undefined || Number.isNaN(v)) return null;
        if (v > max) max = v;
        return { x: ((sample.t - t0) / span) * W, v };
      });
      let last: number | null = null;
      for (let i = pts.length - 1; i >= 0; i--) {
        if (pts[i]) {
          last = pts[i]!.v;
          break;
        }
      }
      return { ...s, pts, present: pts.some((p) => p !== null), last };
    });
    // A touch of headroom so the peak is not welded to the top edge, and never
    // divide by zero on a window where every source read 0 W.
    const yMax = Math.max(1, max * 1.08);
    const drawnSeries = raw
      .filter((s) => s.present)
      .map((s) => {
        // The line breaks across a null rather than leaping the gap: a source
        // that only ran for part of the window should read as absent the rest,
        // not as a diagonal drawn through data that was never there.
        let d = "";
        let pen = false;
        for (const p of s.pts) {
          if (!p) {
            pen = false;
            continue;
          }
          const y = H - (p.v / yMax) * H;
          d += `${pen ? "L" : "M"}${p.x.toFixed(2)} ${y.toFixed(2)} `;
          pen = true;
        }
        return {
          key: s.key,
          label: s.label,
          colour: s.colour,
          d: d.trim(),
          last: s.last,
        };
      });
    return { drawn: drawnSeries, peak: Math.round(max) };
  }, [samples]);

  return (
    <div className="power-series">
      <div className="chart-plot">
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden>
          {drawn.map((s) => (
            <path
              key={s.key}
              className="series-line"
              d={s.d}
              stroke={s.colour}
            />
          ))}
        </svg>
        {drawn.length > 0 && (
          <>
            {/* One watt scale shared across all four sources, so their heights
                compare. */}
            <span className="chart-y top">{peak} W</span>
            <span className="chart-y bot">0 W</span>
          </>
        )}
      </div>
      <div className="series-legend">
        {drawn.length === 0 ? (
          <span className="muted">{loading ? "loading…" : "no history yet"}</span>
        ) : (
          drawn.map((s) => (
            <span key={s.key} className="series-key">
              <span className="series-swatch" style={{ background: s.colour }} />
              {s.label}
              {s.last !== null && <b>{Math.round(s.last)} W</b>}
            </span>
          ))
        )}
      </div>
    </div>
  );
}

/**
 * The load, on its own, as a filled area.
 *
 * The flow chart above carries load as one line among four, which answers "how
 * do the sources balance". This answers a different question — "what is the site
 * actually drawing, and when did it spike" — and a filled area against its own
 * scale reads that at a glance where a shared-scale line cannot. The scale is
 * the window's own peak, so a quiet site's few watts still fill the box rather
 * than sitting flat along the bottom under a chart scaled for a generator.
 */
export function LoadHistory({
  samples,
  loading,
}: {
  samples: SocSample[];
  loading?: boolean;
}) {
  const W = 100;
  const H = 34;

  const { area, line, last, peak } = useMemo(() => {
    if (samples.length < 2) {
      return { area: "", line: "", last: null as number | null, peak: 0 };
    }
    const t0 = samples[0].t;
    const span = Math.max(1, samples[samples.length - 1].t - t0);
    const pts: { x: number; v: number }[] = [];
    for (const s of samples) {
      if (s.load === null || s.load === undefined || Number.isNaN(s.load)) continue;
      pts.push({ x: ((s.t - t0) / span) * W, v: s.load });
    }
    if (pts.length < 2) {
      return { area: "", line: "", last: null, peak: 0 };
    }
    const peak = Math.max(1, ...pts.map((p) => p.v));
    const scale = peak * 1.08;
    const xy = pts.map((p) => ({ x: p.x, y: H - (p.v / scale) * H }));
    const line = xy
      .map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(2)} ${p.y.toFixed(2)}`)
      .join(" ");
    const area = `${line} L${xy[xy.length - 1].x.toFixed(2)} ${H} L${xy[0].x.toFixed(2)} ${H} Z`;
    return { area, line, last: pts[pts.length - 1].v, peak };
  }, [samples]);

  return (
    <div className="load-chart">
      <div className="chart-plot">
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden>
          {line && (
            <>
              <path className="load-area" d={area} />
              <path className="load-line" d={line} />
            </>
          )}
        </svg>
        {line && (
          <>
            <span className="chart-y top">{Math.round(peak)} W</span>
            <span className="chart-y bot">0 W</span>
          </>
        )}
      </div>
      <div className="series-legend">
        {last === null ? (
          <span className="muted">{loading ? "loading…" : "no history yet"}</span>
        ) : (
          <>
            <span className="series-key">
              now <b>{Math.round(last)} W</b>
            </span>
            <span className="series-key">
              peak <b>{Math.round(peak)} W</b>
            </span>
          </>
        )}
      </div>
    </div>
  );
}
