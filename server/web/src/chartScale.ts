/**
 * Axis gradations: round numbers a person would have chosen.
 *
 * The charts used to label only the extremes of whatever the data happened to
 * do — "14.7°C" at the top, "8.2°C" at the bottom — which is a range readout,
 * not an axis. You cannot read a value off it, only the two ends, and the two
 * ends move every time a sample arrives.
 *
 * This is Heckbert's loose labelling: pick a step from {1, 2, 5} × 10ⁿ, then
 * widen the range out to whole multiples of that step. The trace is then drawn
 * against the *rounded* range rather than its own min/max, which is what makes a
 * gridline land exactly on its label — and it retires the old 10% padding hack
 * that existed to stop a flat trace welding itself to the top edge.
 *
 * Reference: Paul Heckbert, "Nice Numbers for Graph Labels", Graphics Gems, 1990.
 */

export interface Tick {
  /** The value at this gradation, for the label. */
  value: number;
  /** 0 at the bottom of the axis, 1 at the top. Both the label's position and
   *  the gridline's coordinate come from this, so they cannot drift apart. */
  frac: number;
}

export interface Scale {
  /** The rounded bottom of the axis — at or below the data's minimum. */
  min: number;
  /** The rounded top — at or above the data's maximum. */
  max: number;
  ticks: Tick[];
  /** Maps a value to 0..1 up the axis. Returns 0 for a zero-height scale. */
  frac(value: number): number;
}

/** The nearest {1,2,5}×10ⁿ to `range`. `round` picks the nearest rather than the
 *  next one up, which is what a step wants and a span does not. */
function niceNum(range: number, round: boolean): number {
  const exp = Math.floor(Math.log10(range));
  const f = range / 10 ** exp;
  let nf: number;
  if (round) nf = f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10;
  else nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
  return nf * 10 ** exp;
}

/**
 * A scale covering [min, max], with gradations on round numbers.
 *
 * @param target the MOST gradations wanted. Heckbert divides the range into
 *   `target - 1` intervals and then rounds outward, so asking for fewer than
 *   the data deserves does not just thin the labels - it widens the axis. At
 *   target 4 a 8.2-14.7 range comes out 5..15 in steps of 5, three gradations
 *   over an axis half of which has no data in it. At 5 it is 8..16 by 2.
 *   Small charts want 4; a full-height one wants 5 or 6.
 */
export function niceScale(min: number, max: number, target = 5): Scale {
  // A flat series - every sample identical, or a single sample - has no range to
  // divide. Centre it in a unit band rather than dividing by zero: the trace
  // then draws as a line across the middle, which is the truth about it.
  if (!Number.isFinite(min) || !Number.isFinite(max) || max === min) {
    const v = Number.isFinite(min) ? min : 0;
    const pad = Math.abs(v) > 0 ? Math.abs(v) * 0.1 : 1;
    return build(v - pad, v + pad, [v - pad, v, v + pad]);
  }
  if (min > max) [min, max] = [max, min];

  const step = niceNum(niceNum(max - min, false) / Math.max(1, target - 1), true);
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;

  const values: number[] = [];
  const steps = Math.round((hi - lo) / step);
  for (let i = 0; i <= steps; i++) values.push(snap(lo + i * step, step));
  return build(lo, hi, values);
}

/**
 * Snap a computed tick to the step's own precision.
 *
 * Multiplying rather than repeatedly adding is not enough on its own: 6 x 0.1 is
 * 0.6000000000000001 in binary floating point, and that reaches the label. The
 * step says how many decimals can be meaningful, so round to one more than that
 * and let Number() drop the trailing zero.
 */
function snap(v: number, step: number): number {
  const dp = Math.max(0, -Math.floor(Math.log10(step)) + 1);
  return Number(v.toFixed(Math.min(dp, 20)));
}

function build(lo: number, hi: number, values: number[]): Scale {
  const span = hi - lo;
  const frac = (v: number) => (span === 0 ? 0 : (v - lo) / span);
  return {
    min: lo,
    max: hi,
    frac,
    ticks: values.map((value) => ({ value, frac: frac(value) })),
  };
}

/**
 * A scale pinned to a range that is meaningful in itself, gradated on round
 * numbers within it. State of charge is 0–100 because that is what a percentage
 * is, not because that is what the battery did this window.
 */
export function fixedScale(lo: number, hi: number, target = 4): Scale {
  // Not niceNum here. A fixed range wants a step that DIVIDES it: 0-100 in
  // quarters is 25, and 25 is 2.5x10^1, which is not in the {1,2,5} set that
  // niceNum picks from - it would answer 50 and give a percentage axis of
  // 0/50/100. So the candidates are the range's own divisors, and 2.5 is
  // allowed here because it is the one that makes quarters work.
  const span = hi - lo;
  let best: number | null = null;
  let bestCost = Infinity;
  for (let n = 2; n <= 10; n++) {
    const step = span / n;
    const mantissa = step / 10 ** Math.floor(Math.log10(step));
    const nice = [1, 2, 2.5, 5, 10].some((m) => Math.abs(mantissa - m) < 1e-9);
    if (!nice) continue;
    const cost = Math.abs(n - (target - 1));
    // <= so that a tie goes to the LARGER n. 0-100 at target 4 ties n=2 (step
    // 50) against n=4 (step 25), and quarters are what a reader of a percentage
    // expects; halves are not an axis.
    if (cost <= bestCost) {
      bestCost = cost;
      best = step;
    }
  }
  const step = best ?? niceNum(span / Math.max(1, target - 1), true);

  const values: number[] = [];
  for (let i = 0; lo + i * step <= hi + step / 1000; i++) {
    values.push(snap(lo + i * step, step));
  }
  // A step that does not divide the range can stop short. The top of a fixed
  // range is the whole reason for fixing it, so it is always labelled.
  if (values[values.length - 1] < hi) values.push(hi);
  return build(lo, hi, values);
}

/** Enough decimals to tell two gradations apart, and no more: a 2° step wants
 *  "8", a 0.5° step wants "8.0". */
export function tickFormat(ticks: Tick[]): (v: number) => string {
  if (ticks.length < 2) return (v) => String(Math.round(v));
  const step = Math.abs(ticks[1].value - ticks[0].value);
  const dp = step >= 10 ? 0 : step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
  return (v) => v.toFixed(dp);
}
