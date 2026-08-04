import { useSyncExternalStore } from "react";
import { FEET_PER_METRE, type AltitudeUnit } from "./format";

/**
 * Per-operator display preferences, held in localStorage.
 *
 * Local, not on the account: there is no server-side preference store, and one
 * would be a schema, a migration and an endpoint for two display toggles. The
 * radio presets live in localStorage for the same reason. The cost is that the
 * choice is per-browser rather than per-login, which for "feet or metres" is a
 * fair trade.
 *
 * A tiny external store rather than lifting into `Console` and threading through
 * every panel: the map's marker code is not React, the contact card and the
 * settings pane are, and `useSyncExternalStore` lets all three read the same
 * value and re-render on a change without a context provider or prop-drilling.
 */

/** The fields a marker's label can carry, in the fixed order they render. Only
 *  callsign is on by default, which is what the label always showed. */
export const LABEL_FIELDS = [
  { key: "callsign", label: "Flight number" },
  { key: "registration", label: "Registration" },
  { key: "type", label: "Type" },
  { key: "altitude", label: "Altitude" },
  { key: "speed", label: "Speed" },
] as const;

export type LabelField = (typeof LABEL_FIELDS)[number]["key"];

export interface DisplayPrefs {
  altitudeUnit: AltitudeUnit;
  /** Which fields to show on an unselected contact's map label. */
  labelFields: LabelField[];
  /** What the operator considers a close contact worth flagging red: within
   *  this range AND below this altitude. The station computes its own `alert`
   *  flag for its own local alerting on its own thresholds — this is the
   *  console's, for the person looking, and it is what drives the red styling
   *  here. Distance in km (the map's rings are km); altitude in feet (how "low"
   *  is read in aviation). */
  criticalRangeKm: number;
  criticalAltitudeFt: number;
}

const KEY = "percepta.display";
/** 12 km and 5,000 ft: the station's own defaults are 12 km / 1,500 m, and
 *  5,000 ft is that altitude rounded to a figure an operator recognises. */
const DEFAULTS: DisplayPrefs = {
  altitudeUnit: "both",
  labelFields: ["callsign"],
  criticalRangeKm: 12,
  criticalAltitudeFt: 5000,
};
const UNITS: AltitudeUnit[] = ["m", "ft", "both"];

/** A stored number, or the default if it is missing, not a number, or outside a
 *  sane band — a hand-edited localStorage value cannot make the whole console
 *  flag everything or nothing. */
function positiveWithin(value: unknown, fallback: number, max: number): number {
  return typeof value === "number" && Number.isFinite(value)
    && value > 0 && value <= max
    ? value
    : fallback;
}

function load(): DisplayPrefs {
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY) || "{}");
    return {
      altitudeUnit: UNITS.includes(parsed.altitudeUnit)
        ? parsed.altitudeUnit
        : DEFAULTS.altitudeUnit,
      // Filtered against the known keys and kept in the canonical order, so a
      // stored field that has since been removed cannot break the label and the
      // order does not depend on the order they were clicked.
      labelFields: Array.isArray(parsed.labelFields)
        ? (LABEL_FIELDS.map((f) => f.key).filter((k) =>
            parsed.labelFields.includes(k),
          ) as LabelField[])
        : DEFAULTS.labelFields,
      criticalRangeKm: positiveWithin(
        parsed.criticalRangeKm, DEFAULTS.criticalRangeKm, 300,
      ),
      criticalAltitudeFt: positiveWithin(
        parsed.criticalAltitudeFt, DEFAULTS.criticalAltitudeFt, 60000,
      ),
    };
  } catch {
    return DEFAULTS;
  }
}

let current: DisplayPrefs = load();
const listeners = new Set<() => void>();

export function getDisplayPrefs(): DisplayPrefs {
  return current;
}

export function setDisplayPrefs(patch: Partial<DisplayPrefs>): void {
  current = { ...current, ...patch };
  try {
    localStorage.setItem(KEY, JSON.stringify(current));
  } catch {
    /* private mode or a full quota; the choice just does not persist */
  }
  listeners.forEach((notify) => notify());
}

function subscribe(notify: () => void): () => void {
  listeners.add(notify);
  return () => {
    listeners.delete(notify);
  };
}

/** Reactive read of the current preferences. New object only on a real change,
 *  so it is a stable dependency between them. */
export function useDisplayPrefs(): DisplayPrefs {
  return useSyncExternalStore(subscribe, getDisplayPrefs, getDisplayPrefs);
}

/**
 * Whether a contact is close enough and low enough to flag red, by the
 * operator's own thresholds.
 *
 * Both conditions, and altitude must be reported: the same rule the station
 * applies for its own alerting (range AND below-altitude, an aircraft with no
 * altitude never alerting), but judged here against the console's settings so
 * each operator sees the airspace at the distance and height they care about.
 * A contact with no range or no altitude is never critical — the honest answer
 * when the thing that decides it was not reported.
 */
export function isCritical(
  rangeKm: number | null | undefined,
  altitudeM: number | null | undefined,
  prefs: DisplayPrefs,
): boolean {
  if (rangeKm === null || rangeKm === undefined) return false;
  if (altitudeM === null || altitudeM === undefined) return false;
  return (
    rangeKm < prefs.criticalRangeKm
    && altitudeM * FEET_PER_METRE < prefs.criticalAltitudeFt
  );
}

/** Test-only: forget everything, so one test's choice does not leak into the
 *  next through the module-level state or localStorage. */
export function _resetDisplayPrefs(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* nothing to clear */
  }
  current = { ...DEFAULTS };
  listeners.forEach((notify) => notify());
}
