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

const FEET_PER_METRE = 3.28084;
const KM_PER_NM = 1.852;

/** Metres and feet together, because altitude is the one number here an
 *  aviation reader will want in feet and everything else in this console is
 *  metric. Neither unit is made the "real" one. */
function altitudeText(metres: number): string {
  const feet = Math.round(metres * FEET_PER_METRE);
  return `${Math.round(metres).toLocaleString()} m · ${feet.toLocaleString()} ft`;
}

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
    altitude_type,
    altitude_corrected_m,
    vertical_speed_ms: vertical_speed,
    speed_kt: speed,
    track_deg: track,
    range_km,
    bearing_deg: bearing,
    squawk,
    seconds_since_contact,
    on_ground,
    simulated,
    source,
    alert,
  } = contact;

  // A pressure altitude is referenced to 1013.25 hPa, not to the local datum,
  // so it is not the aircraft's height above the sea today. Saying which datum
  // it is turns a number that looks exact into one that can be trusted
  // correctly, and it is the whole reason the correction below exists.
  const datum =
    altitude_type === "pressure"
      ? "pressure altitude, 1013.25 hPa datum"
      : altitude_type === "geometric"
        ? "geometric (GNSS)"
        : null;

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
          <>
            <strong>{altitudeText(altitude)}</strong>
            {datum && <span className="contact-datum">{datum}</span>}
          </>
        )}
      </div>

      <div className="contact-rows">
        {/* Beside the reported altitude, never instead of it: what the receiver
            said and what it means against this station's barometer are two
            facts, and showing only the second hides the working. Absent
            entirely when the correction is off, rather than shown empty — an
            empty row invites the reading that it failed. */}
        {altitude_corrected_m !== null && altitude_corrected_m !== undefined && (
          <Row label="Corrected" wide>
            {altitudeText(altitude_corrected_m)}
            <span className="contact-note"> against this station's barometer</span>
          </Row>
        )}

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

        {/* No Type row. The panel is opened by clicking the aircraft's own
            glyph, and that glyph *is* the type — a row naming it again is the
            same fact twice, one of them redundant the moment you know how the
            icons read. The mapping is still in emitters.ts and still worth
            keeping honest; it simply does not need a line here. */}

        {on_ground !== null && on_ground !== undefined && (
          <Row label="State">{on_ground ? "on the ground" : "airborne"}</Row>
        )}

        {source && (
          <Row label="Band">{source === "uat" ? "UAT, 978 MHz" : "1090ES"}</Row>
        )}

        {!stale
          && seconds_since_contact !== null
          && seconds_since_contact !== undefined && (
          <Row label="Last heard">{`${Math.round(seconds_since_contact)} s ago`}</Row>
        )}
      </div>
    </div>
  );
}
