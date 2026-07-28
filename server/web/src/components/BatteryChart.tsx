import { useMemo } from "react";

export interface SocSample {
  t: number;
  soc: number;
}

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
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden>
        <line className="chart-shed" x1="0" y1={shedY} x2={W} y2={shedY} />
        {line && (
          <>
            <path className="chart-area" d={area} />
            <path className="chart-line" d={line} />
          </>
        )}
      </svg>
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
