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
  const m = `${Math.round(metres).toLocaleString()} m`;
  const ft = `${Math.round(metres * FEET_PER_METRE).toLocaleString()} ft`;
  if (unit === "m") return m;
  if (unit === "ft") return ft;
  return `${m} · ${ft}`;
}
