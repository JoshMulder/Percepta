import { useSyncExternalStore } from "react";
import type { AltitudeUnit } from "./format";

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
}

const KEY = "percepta.display";
const DEFAULTS: DisplayPrefs = { altitudeUnit: "both", labelFields: ["callsign"] };
const UNITS: AltitudeUnit[] = ["m", "ft", "both"];

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
