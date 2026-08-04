import { emitterName } from "./adsbIcons";
import type { DisplayPrefs, LabelField } from "./displayPrefs";
import { formatAltitude } from "./format";
import type { Aircraft } from "./types";

/**
 * The text under an unselected contact's glyph, from the operator's chosen
 * fields.
 *
 * One line per field, in the fixed order the settings list them, and a field
 * with nothing to say is simply skipped — a contact with no callsign does not
 * leave a blank line where its flight number would be. If every chosen field is
 * empty the ICAO address stands in, so a marker is never a glyph with no label
 * at all.
 *
 * Altitude on a label is always a single unit even when the card shows both: a
 * marker has no room for "3,500 m · 11,483 ft", so "both" collapses to feet, the
 * aviation default, and only an explicit choice of metres overrides it.
 */
export function buildLabel(
  contact: Aircraft,
  prefs: DisplayPrefs,
  registration: string | null,
): string {
  const parts: string[] = [];
  const seen = new Set<string>();
  for (const field of prefs.labelFields) {
    const value = fieldText(field, contact, prefs, registration);
    // Skip a field that would only repeat a line already on the label. The case
    // this is for: registration falls back to the callsign when the registry
    // has no tail number, so a label showing both flight number and
    // registration would otherwise print the callsign twice.
    if (value && !seen.has(value)) {
      seen.add(value);
      parts.push(value);
    }
  }
  if (parts.length === 0) parts.push(contact.icao);
  return parts.join("\n");
}

function fieldText(
  field: LabelField,
  contact: Aircraft,
  prefs: DisplayPrefs,
  registration: string | null,
): string | null {
  switch (field) {
    case "callsign":
      return contact.callsign?.trim() || null;
    case "registration":
      return registration || null;
    case "type":
      return emitterName(contact.emitter_type);
    case "altitude":
      return contact.altitude_m === null || contact.altitude_m === undefined
        ? null
        : formatAltitude(
            contact.altitude_m,
            prefs.altitudeUnit === "m" ? "m" : "ft",
          );
    case "speed":
      return contact.speed_kt === null || contact.speed_kt === undefined
        ? null
        : `${Math.round(contact.speed_kt)} kt`;
    default:
      return null;
  }
}
