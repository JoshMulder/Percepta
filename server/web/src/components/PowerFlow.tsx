import type { PowerPayload } from "../types";

/**
 * Where this site's power is coming from and going, drawn as it moves.
 *
 * Three possible sources across the top — solar, mains, generator — the battery
 * to one side, and the load beneath. Energy is animated along the links, and
 * the speed of the animation is the magnitude: a trickle looks like a trickle.
 * The point is that "the generator is carrying the site while the battery
 * recharges" is a sentence an operator should be able to read in one glance,
 * and four numbers in a list is not that.
 *
 * **A source that is not fitted is not drawn.** Not greyed, not zeroed —
 * absent. The station omits mains and generator entirely at a site that has
 * neither, and a diagram showing a dead generator at a site that has never had
 * one is inventing hardware. What *is* drawn dim is a fitted source currently
 * contributing nothing, because that is a real state with a real difference:
 * a mains input at 0 W might be a grid failure.
 *
 * **The battery link is the only reversible one** and its direction is taken
 * from `battery_w`, which the station measures. Deriving it here from the other
 * numbers would mean modelling conversion losses and source priority, and being
 * wrong about which way a battery is going is worse than not drawing an arrow.
 *
 * SVG rather than canvas: a handful of nodes and links, no per-frame redraw,
 * and the animation is CSS on stroke-dashoffset — so it costs the compositor
 * nothing and keeps running without JavaScript touching it. It is also a fixed
 * viewBox, which matters here for the same reason it did for the spectrum: this
 * panel is inside the sidebar the console scales to fit, and anything that
 * measures its own container re-triggers that fit.
 */

/** Anything under this reads as noise on a 48 V system and is drawn as idle
 *  rather than as a flow nobody can see moving. */
const IDLE_W = 5;

type Flow = {
  key: string;
  label: string;
  watts: number;
  /** Fitted but contributing nothing — drawn, dim. Distinct from absent. */
  idle: boolean;
};

/** Seconds per dash cycle: bigger flows animate faster. Clamped at both ends —
 *  too slow reads as stopped, too fast reads as a glitch. */
function speed(watts: number): number {
  const w = Math.abs(watts);
  if (w < IDLE_W) return 0;
  return Math.max(0.35, Math.min(3, 900 / w));
}

function Node({
  x, y, label, value, tone, dim, half = 42,
}: {
  x: number; y: number; label: string; value: string;
  tone: string; dim?: boolean;
  /** Half-width. Sized by the caller from the space each node actually has:
   *  three sources across a 300-unit box leaves 75 each, and a fixed 84-wide
   *  box overlaps its neighbours by nine. */
  half?: number;
}) {
  return (
    <g className={`pf-node pf-${tone}${dim ? " dim" : ""}`}>
      <rect x={x - half} y={y - 17} width={half * 2} height={34} rx={6} />
      <text x={x} y={y - 3} className="pf-label">{label}</text>
      <text x={x} y={y + 11} className="pf-value">{value}</text>
    </g>
  );
}

function Link({
  d, watts, tone,
}: {
  d: string; watts: number; tone: string;
}) {
  const seconds = speed(watts);
  return (
    <g className={`pf-link pf-${tone}${seconds ? "" : " idle"}`}>
      <path className="pf-track" d={d} />
      {seconds > 0 && (
        <path
          className="pf-dash"
          d={d}
          style={{ animationDuration: `${seconds}s` }}
        />
      )}
    </g>
  );
}

const W = (watts: number) => `${Math.round(Math.abs(watts))} W`;

export function PowerFlow({ power }: { power: PowerPayload | null }) {
  if (!power) return <div className="pf-empty" aria-hidden />;

  // `undefined` is "no such source at this site" and must not become 0 — see
  // the note on PowerPayload. Every check here is an explicit presence test
  // for that reason.
  const sources: Flow[] = [
    {
      key: "solar",
      label: "Solar",
      watts: power.pv_w,
      idle: power.pv_w < IDLE_W,
    },
  ];
  if (power.mains_present !== undefined) {
    sources.push({
      key: "mains",
      label: power.mains_present ? "Mains" : "Mains down",
      watts: power.mains_w ?? 0,
      idle: (power.mains_w ?? 0) < IDLE_W,
    });
  }
  if (power.generator_running !== undefined) {
    sources.push({
      key: "gen",
      label: power.generator_running ? "Generator" : "Generator off",
      watts: power.generator_w ?? 0,
      idle: (power.generator_w ?? 0) < IDLE_W,
    });
  }

  const batteryW = power.battery_w ?? 0;
  const charging = batteryW > IDLE_W;
  const discharging = batteryW < -IDLE_W;

  // Sources spread across the top, the bus in the middle, load below, battery
  // to the right of the bus. Positions are fixed so the diagram does not
  // reflow as sources appear and disappear — a layout that jumps when the
  // generator starts is a layout nobody can read at a glance.
  // Wide and flat, because that is the shape of the space. A 2:1 box
  // letterboxed into a 34rem-wide sidebar at a fixed height used less than
  // half the width and drew everything half-size for no reason.
  const width = 420;
  const busY = 56;
  const step = width / (sources.length + 1);
  // Fit the boxes to the gaps rather than the other way round, with a little
  // air between them.
  const half = Math.min(42, step / 2 - 3);

  return (
    <svg
      className="power-flow"
      viewBox={`0 0 ${width} 114`}
      // Letterboxed rather than stretched: the height is fixed in CSS so the
      // panel cannot change size when data arrives, and the drawing keeps its
      // proportions inside whatever width the sidebar happens to be.
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label="Power flow"
    >
      {sources.map((s, i) => {
        const x = step * (i + 1);
        return (
          <g key={s.key}>
            <Link
              d={`M ${x} 35 L ${x} ${busY}`}
              watts={s.idle ? 0 : s.watts}
              tone={s.key}
            />
            <Node
              x={x}
              y={18}
              half={half}
              label={s.label}
              value={W(s.watts)}
              tone={s.key}
              dim={s.idle}
            />
          </g>
        );
      })}

      {/* The bus: what every source feeds and what feeds the load. */}
      <path className="pf-bus" d={`M ${step - 6} ${busY} H ${width - step + 6}`} />

      {/* Battery. Drawn to the right of the bus, and the only link whose
          direction changes — taken from the station's signed measurement, not
          inferred here. */}
      <Link
        d={
          charging
            ? `M ${width / 2} ${busY} L ${width - 46} ${busY} L ${width - 46} 75`
            : `M ${width - 46} 75 L ${width - 46} ${busY} L ${width / 2} ${busY}`
        }
        watts={charging || discharging ? batteryW : 0}
        tone="battery"
      />
      <Node
        x={width - 46}
        y={92}
        label={charging ? "Charging" : discharging ? "Battery" : "Battery"}
        value={charging || discharging ? W(batteryW) : "idle"}
        tone="battery"
        dim={!charging && !discharging}
      />

      <Link
        d={`M ${width / 2} ${busY} L 46 ${busY} L 46 75`}
        watts={power.load_w}
        tone="load"
      />
      <Node
        x={46}
        y={92}
        label="Load"
        value={W(power.load_w)}
        tone="load"
        dim={power.load_w < IDLE_W}
      />
    </svg>
  );
}
