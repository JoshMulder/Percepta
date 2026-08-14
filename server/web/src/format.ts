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


/* --------------------------------------------------------------- weather ---
 *
 * The station reports in one set of units and always will: degrees Celsius,
 * hectopascals, knots. Those are the contract's, they are what the sensors
 * produce, and nothing here changes what is stored, transmitted or recorded —
 * only what is drawn.
 *
 * That distinction is the whole design. Converting on the way in would mean the
 * history table, the alert thresholds and the station's own logs disagreed with
 * each other depending on who last changed a preference. Converting at the last
 * possible moment means a person can read the screen in whatever they think in,
 * and every number underneath it stays comparable.
 *
 * Knots are kept as an option and as the default because this is an aviation
 * product: the airband radio, the ADS-B and the aerodrome sites all speak knots,
 * and a wind in km/h beside an aircraft's groundspeed in knots is a conversion
 * an operator has to do in their head at the worst moment.
 */

export type TemperatureUnit = "c" | "f";
export type PressureUnit = "hpa" | "inhg" | "mb";
export type WindUnit = "kt" | "kmh" | "mph" | "ms";

export function formatTemperature(celsius: number, unit: TemperatureUnit): string {
  if (unit === "f") return ((celsius * 9) / 5 + 32).toFixed(1);
  return celsius.toFixed(1);
}

export function temperatureSuffix(unit: TemperatureUnit): string {
  return unit === "f" ? "\u00b0F" : "\u00b0C";
}

export function formatPressure(hpa: number, unit: PressureUnit): string {
  // Millibars and hectopascals are the SAME unit under two names - 1 mb is
  // exactly 1 hPa - and both are offered because the label is what differs and
  // people are firm about which one they read.
  if (unit === "inhg") return (hpa * 0.0295299830714).toFixed(2);
  return hpa.toFixed(0);
}

export function pressureSuffix(unit: PressureUnit): string {
  if (unit === "inhg") return "inHg";
  return unit === "mb" ? "mb" : "hPa";
}

export function formatWind(knots: number, unit: WindUnit): string {
  if (unit === "kmh") return (knots * 1.852).toFixed(0);
  if (unit === "mph") return (knots * 1.15078).toFixed(0);
  if (unit === "ms") return (knots * 0.514444).toFixed(1);
  return knots.toFixed(0);
}

export function windSuffix(unit: WindUnit): string {
  if (unit === "kmh") return "km/h";
  if (unit === "mph") return "mph";
  if (unit === "ms") return "m/s";
  return "kt";
}

/**
 * Everything a weather series needs to draw itself in the reader's units.
 *
 * One helper rather than three lookups at each call site, because the three
 * answers must agree: converting the value without changing the suffix mislabels
 * it, and changing the suffix without the decimals prints an inHg pressure as
 * "30" — which is a plausible-looking number that is wrong by a factor a pilot
 * would notice.
 *
 * The conversion happens BEFORE the axis is scaled, not after. An axis gradated
 * in Celsius and labelled Fahrenheit is worse than one in the wrong units
 * throughout, because it is wrong only where somebody reads it carefully.
 */
export interface WeatherDisplay {
  convert: (value: number) => number;
  suffix: string;
  digits: number;
}

export function weatherDisplay(
  series: string,
  prefs: {
    temperatureUnit: TemperatureUnit;
    pressureUnit: PressureUnit;
    windUnit: WindUnit;
  },
): WeatherDisplay {
  if (series === "temp") {
    const f = prefs.temperatureUnit === "f";
    return {
      convert: (v) => (f ? (v * 9) / 5 + 32 : v),
      suffix: temperatureSuffix(prefs.temperatureUnit),
      digits: 1,
    };
  }
  if (series === "pressure") {
    const inhg = prefs.pressureUnit === "inhg";
    return {
      convert: (v) => (inhg ? v * 0.0295299830714 : v),
      suffix: pressureSuffix(prefs.pressureUnit),
      // inHg moves in hundredths across a whole weather system; printed to the
      // nearest whole number every reading in a week would be "30".
      digits: inhg ? 2 : 0,
    };
  }
  if (series === "wind") {
    const u = prefs.windUnit;
    const factor = u === "kmh" ? 1.852 : u === "mph" ? 1.15078 : u === "ms" ? 0.514444 : 1;
    return {
      convert: (v) => v * factor,
      suffix: windSuffix(u),
      // m/s puts a calm day between 0 and 3, so whole numbers would quantise it
      // to almost nothing.
      digits: u === "ms" ? 1 : 0,
    };
  }
  // Humidity, and anything added later: per cent is per cent everywhere.
  return { convert: (v) => v, suffix: "%", digits: 0 };
}
