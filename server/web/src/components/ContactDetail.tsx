import { useAircraftInfo } from "../aircraftInfo";
import { emitterName } from "../adsbIcons";
import { isCritical, useDisplayPrefs } from "../displayPrefs";
import { FEET_PER_METRE, formatAltitude } from "../format";
import type { Aircraft } from "../types";

/**
 * One ADS-B contact, in full.
 *
 * **Altitude leads**, because it is the question this panel exists to answer:
 * a dot on a map already carries position and range, and what an operator
 * cannot get from the map is how high the thing is.
 *
 * The rule everywhere below is that a field the aircraft did not send reads as
 * *not sent*, never as zero and never as a blank that might be either. The
 * receiver attaches a validity flag to each of altitude, heading, velocity,
 * vertical velocity, callsign and squawk, and that flag is the only copy of the
 * distinction between "the value is zero" and "there is no value" — squawk 0000
 * is a real code, 0 kt is an aircraft that has stopped, and a level flight
 * vertical speed of 0 is not the same as a receiver that never reported one.
 * The station preserved that distinction all the way to the wire; discarding it
 * in the last 50 pixels would waste the whole exercise.
 */

const KM_PER_NM = 1.852;

function Row({
  label,
  children,
  wide,
}: {
  label: string;
  children: React.ReactNode;
  wide?: boolean;
}) {
  return (
    <div className={`contact-row${wide ? " wide" : ""}`}>
      <span className="contact-k">{label}</span>
      <span className="contact-v">{children}</span>
    </div>
  );
}

/** The one place a null becomes visible text, so there is exactly one wording
 *  for it and no call site can invent another. */
function Absent() {
  return <span className="contact-absent">not reported</span>;
}

export function ContactDetail({
  contact,
  onClose,
}: {
  contact: Aircraft;
  onClose: () => void;
}) {
  const {
    icao,
    callsign,
    altitude_m: altitude,
    vertical_speed_ms: vertical_speed,
    speed_kt: speed,
    track_deg: track,
    range_km,
    bearing_deg: bearing,
    squawk,
    seconds_since_contact,
    on_ground,
    simulated,
    emitter_type,
  } = contact;

  // The emitter category in words — "Helicopter", "Large aircraft" — which is
  // all the transponder broadcasts. The lookup below fills in the specific model
  // and the tail number, which are not in ADS-B, when a registry has them.
  const category = emitterName(emitter_type);
  const prefs = useDisplayPrefs();
  const { altitudeUnit } = prefs;
  const info = useAircraftInfo(icao);
  // Model when a registry has it, category otherwise: "Boeing 737-838" says more
  // than "Large aircraft", but the category is always there to fall back on.
  const type = info?.model ?? category;
  // A resolved lookup with no tail number falls back to the callsign — the
  // operator asked the flight number to stand in when the registry has nothing.
  // `info` is null only while the lookup is pending, so the row stays absent
  // until there is an answer rather than flashing the callsign and replacing it.
  const registration = info
    ? info.registration ?? callsign?.trim() ?? null
    : null;
  // The console's own close-and-low judgement, on the operator's thresholds —
  // not the station's alert flag, which is for the station's own alerting.
  const alert = isCritical(range_km, altitude, prefs);

  const stale = seconds_since_contact !== null
    && seconds_since_contact !== undefined
    && seconds_since_contact >= 30;

  return (
    <div
      className="contact-detail"
      role="dialog"
      aria-label={`Contact ${callsign?.trim() || icao}`}
    >
      <div className="contact-head">
        <div>
          <h4>{callsign?.trim() || icao}</h4>
          <span className="contact-icao">{icao}</span>
        </div>
        <button
          type="button"
          className="contact-close"
          onClick={onClose}
          aria-label="Close"
        >
          ×
        </button>
      </div>

      {/* Two flags that change how everything below should be read, so they go
          above it rather than in the list. */}
      {simulated && (
        <div className="contact-flag sim">
          Injected test target, not real traffic
        </div>
      )}
      {stale && (
        <div className="contact-flag stale">
          Last heard {Math.round(seconds_since_contact as number)} s ago —
          this position is a memory
        </div>
      )}
      {alert && !stale && (
        <div className="contact-flag alert">Close and low</div>
      )}

      <div className="contact-altitude">
        {altitude === null || altitude === undefined ? (
          <span className="contact-absent">Altitude not reported</span>
        ) : (
          <strong>{formatAltitude(altitude, altitudeUnit)}</strong>
        )}
      </div>

      <div className="contact-rows">
        <Row label="Vertical">
          {vertical_speed === null || vertical_speed === undefined ? (
            <Absent />
          ) : Math.abs(vertical_speed) < 0.5 ? (
            "level"
          ) : (
            `${vertical_speed > 0 ? "▲" : "▼"} ${Math.abs(
              Math.round(vertical_speed * FEET_PER_METRE * 60),
            ).toLocaleString()} ft/min`
          )}
        </Row>

        <Row label="Speed">
          {speed === null || speed === undefined ? <Absent /> : `${Math.round(speed)} kt`}
        </Row>

        <Row label="Track">
          {track === null || track === undefined
            ? <Absent />
            : `${Math.round(track).toString().padStart(3, "0")}°`}
        </Row>

        <Row label="Range">
          {`${range_km.toFixed(1)} km · ${(range_km / KM_PER_NM).toFixed(1)} NM`}
        </Row>

        <Row label="Bearing">
          {`${Math.round(bearing).toString().padStart(3, "0")}° from station`}
        </Row>

        <Row label="Squawk">
          {/* Padded to four octal digits: 0 is the code 0000, and rendering it
              as "0" makes a valid code look like missing data. */}
          {squawk === null || squawk === undefined
            ? <Absent />
            : squawk.toString().padStart(4, "0")}
        </Row>

        {/* The model where a registry has it, the emitter category otherwise.
            Absent only when the transponder sent no category and no lookup
            answered — reads as not reported, like every other unsent field. */}
        <Row label="Type">{type ?? <Absent />}</Row>

        {/* Only when known. A tail number is not in ADS-B, so an aircraft no
            registry has simply shows no Registration row, the same way the
            ground state is omitted when the receiver cannot know it. */}
        {registration && <Row label="Registration">{registration}</Row>}

        {on_ground !== null && on_ground !== undefined && (
          <Row label="State">{on_ground ? "on the ground" : "airborne"}</Row>
        )}
      </div>
    </div>
  );
}
