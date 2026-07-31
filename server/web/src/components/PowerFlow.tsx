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
  x, y, label, value, sub, tone, dim, half = 42,
}: {
  x: number; y: number; label: string; value: string;
  /** A second reading under the first. Only the battery has one — its state of
   *  charge is the headline and its throughput is the detail. */
  sub?: string;
  tone: string; dim?: boolean;
  /** Half-width. Sized by the caller from the space each node actually has:
   *  three sources across a 300-unit box leaves 75 each, and a fixed 84-wide
   *  box overlaps its neighbours by nine. */
  half?: number;
}) {
  return (
    <g className={`pf-node pf-${tone}${dim ? " dim" : ""}`}>
      <rect
        x={x - half}
        y={y - (sub ? 24 : 18)}
        width={half * 2}
        height={sub ? 48 : 36}
        rx={6}
      />
      <text x={x} y={y - (sub ? 9 : 3)} className="pf-label">{label}</text>
      <text x={x} y={y + (sub ? 7 : 12)} className="pf-value">{value}</text>
      {sub && <text x={x} y={y + 20} className="pf-sub">{sub}</text>}
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

  const soc = power.soc_pct ?? null;
  // The same thresholds the station duty-cycles on: below 20% it starts
  // shedding load, so the panel should look worried before the site acts.
  const socTone =
    soc === null ? "battery" : soc < 20 ? "battery-critical"
      : soc < 40 ? "battery-low" : "battery";
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
  const busY = 58;
  // The battery box is the widest of them - a percentage over a wattage - and
  // is centred on its tap, so the tap has to sit a full half-width plus a
  // margin in from the edge or the box is clipped by the viewBox.
  const batteryHalf = 52;
  const batteryX = width - batteryHalf - 4;
  const step = width / (sources.length + 1);
  // Fit the boxes to the gaps rather than the other way round, with a little
  // air between them.
  const half = Math.min(42, step / 2 - 3);

  // Where everything attaches to the rail, and what it puts in or takes out.
  // A sink is negative, which makes the running total below a straight sum.
  const taps = [
    { x: 46, w: -power.load_w },
    ...sources.map((s, i) => ({ x: step * (i + 1), w: s.idle ? 0 : s.watts })),
    // Charging absorbs, discharging supplies — one expression because
    // `battery_w` already carries the sign the station measured.
    { x: batteryX, w: -batteryW },
  ].sort((a, b) => a.x - b.x);

  // What crosses each span between neighbours: everything to its left, summed.
  // With sources balancing sinks this is zero outside the outermost taps and
  // non-zero only where power genuinely has to travel.
  const segments: { from: number; to: number; flow: number }[] = [];
  let running = 0;
  for (let i = 0; i < taps.length - 1; i++) {
    running += taps[i].w;
    segments.push({ from: taps[i].x, to: taps[i + 1].x, flow: running });
  }

  return (
    <svg
      className="power-flow"
      viewBox={`0 0 ${width} 122`}
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
              d={`M ${x} 36 L ${x} ${busY}`}
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

      {/* The bus, one animated segment per span between attachment points.

          A single animation along the whole rail was wrong in both directions:
          first it appeared to pour out of whichever source happened to sit at
          the centre — a mains input reading 0 W, in the case that showed it up
          — and then, made static, it said nothing at all about power that
          plainly had to cross it to reach the load.

          What actually crosses any point of the rail is the sum of everything
          attached to its left: sources add, sinks subtract. Positive means
          the power is travelling right, negative left. Because the station's
          sources balance its sinks, the running total is zero at both ends and
          only the middle carries anything — which is the truthful picture, and
          it falls out of the arithmetic rather than being drawn on top of it. */}
      {segments.map((seg) => (
        <Link
          key={`bus-${seg.from}`}
          d={
            seg.flow >= 0
              ? `M ${seg.from} ${busY} L ${seg.to} ${busY}`
              : `M ${seg.to} ${busY} L ${seg.from} ${busY}`
          }
          watts={seg.flow}
          tone="bus"
        />
      ))}

      {/* Battery. Drawn to the right of the bus, and the only link whose
          direction changes — taken from the station's signed measurement, not
          inferred here.

          The label does not say "charging" or "discharging". The animation
          already does, by running the other way, and a word repeating what the
          picture shows is a word competing with the two numbers that are only
          available here: the state of charge and what is going in or out. */}
      {/* Down into the battery when charging, up out of it when discharging.
          The path's own direction is what the dash animation follows, so the
          arrow is the geometry rather than a separate flag that could
          disagree with it. */}
      <Link
        d={
          charging
            ? `M ${batteryX} ${busY} L ${batteryX} 68`
            : `M ${batteryX} 68 L ${batteryX} ${busY}`
        }
        watts={charging || discharging ? batteryW : 0}
        tone="battery"
      />
      <Node
        x={batteryX}
        y={92}
        half={batteryHalf}
        label="Battery"
        value={soc === null ? "--" : `${Math.round(soc)}%`}
        sub={charging || discharging ? W(batteryW) : "idle"}
        tone={socTone}
        dim={false}
      />

      <Link
        d={`M 46 ${busY} L 46 74`}
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
