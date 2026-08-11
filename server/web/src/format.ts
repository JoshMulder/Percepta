export const FEET_PER_METRE = 3.28084;

export type AltitudeUnit = "m" | "ft" | "both";

/**
 * An altitude in the unit the operator asked for.
 *
 * The console is metric everywhere else, but altitude is the one number an
 * aviation reader wants in feet — so it is a preference rather than a fixed
 * choice. "both" keeps the old behaviour, showing each so neither is asserted to
 * be the real one; the single-unit options are for a reader who has decided.
 */
export function formatAltitude(metres: number, unit: AltitudeUnit): string {
  // Floored at the ground for display. A barometric ADS-B altitude reads
  // slightly negative on a low-pressure day — the contract allows down to
  // -1000 m and the station reports it honestly — but a below-ground height
  // reads to an operator as a fault, so the console shows 0 rather than a
  // negative number. Only the display is floored; the alert logic still sees
  // the true altitude.
  const grounded = Math.max(0, metres);
  const m = `${Math.round(grounded).toLocaleString()} m`;
  const ft = `${Math.round(grounded * FEET_PER_METRE).toLocaleString()} ft`;
  if (unit === "m") return m;
  if (unit === "ft") return ft;
  return `${m} · ${ft}`;
}
