import { LABEL_FIELDS, setDisplayPrefs, useDisplayPrefs } from "../displayPrefs";
import type { LabelField } from "../displayPrefs";
import type {
  AltitudeUnit,
  PressureUnit,
  TemperatureUnit,
  WindUnit,
} from "../format";

const UNIT_OPTIONS: { value: AltitudeUnit; label: string }[] = [
  { value: "both", label: "Both" },
  { value: "ft", label: "Feet" },
  { value: "m", label: "Metres" },
];

const TEMPERATURE_OPTIONS: { value: TemperatureUnit; label: string }[] = [
  { value: "c", label: "Celsius" },
  { value: "f", label: "Fahrenheit" },
];

const PRESSURE_OPTIONS: { value: PressureUnit; label: string }[] = [
  { value: "hpa", label: "hPa" },
  { value: "mb", label: "Millibars" },
  { value: "inhg", label: "inHg" },
];

const WIND_OPTIONS: { value: WindUnit; label: string }[] = [
  { value: "kt", label: "Knots" },
  { value: "kmh", label: "km/h" },
  { value: "mph", label: "mph" },
  { value: "ms", label: "m/s" },
];

/**
 * Display choices that belong to the person looking, not the station.
 *
 * No Save button and no server round-trip: these are local, take effect the
 * moment they change, and are remembered on this browser — the same choice the
 * radio presets make, for the same reason. There is nothing here that can fail.
 */
export function SettingsDisplay() {
  const prefs = useDisplayPrefs();

  const toggleField = (key: LabelField, on: boolean) => {
    // Rebuilt from the canonical order rather than by pushing onto the stored
    // list, so the label always reads callsign, registration, type… whatever
    // order the boxes were ticked in.
    const next = LABEL_FIELDS.map((field) => field.key).filter((k) =>
      k === key ? on : prefs.labelFields.includes(k),
    );
    setDisplayPrefs({ labelFields: next });
  };

  return (
    <div className="settings-sections">
      <section className="settings-section">
        <h3>Altitude</h3>
        <p className="pref-note">
          The unit for every altitude on the console — the contact card and the
          map labels. Applies at once and is kept on this browser.
        </p>
        <div className="window-switch" role="radiogroup" aria-label="Altitude unit">
          {UNIT_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              role="radio"
              aria-checked={prefs.altitudeUnit === option.value}
              className={`window-btn${
                prefs.altitudeUnit === option.value ? " active" : ""
              }`}
              onClick={() => setDisplayPrefs({ altitudeUnit: option.value })}
            >
              {option.label}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-section">
        <h3>Weather units</h3>
        <p className="pref-note">
          How the weather panel and its charts are drawn. The station always
          reports Celsius, hectopascals and knots, and that is what is recorded —
          these change the reading on the screen, never the reading in the
          history.
        </p>

        <div className="pref-fields">
          <div className="field-row">
            <span className="pref-label">Temperature</span>
            <div
              className="window-switch"
              role="radiogroup"
              aria-label="Temperature unit"
            >
              {TEMPERATURE_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={prefs.temperatureUnit === option.value}
                  className={`window-btn${
                    prefs.temperatureUnit === option.value ? " active" : ""
                  }`}
                  onClick={() => setDisplayPrefs({ temperatureUnit: option.value })}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </div>

          <div className="field-row">
            <span className="pref-label">Pressure</span>
            <div
              className="window-switch"
              role="radiogroup"
              aria-label="Pressure unit"
            >
              {PRESSURE_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={prefs.pressureUnit === option.value}
                  className={`window-btn${
                    prefs.pressureUnit === option.value ? " active" : ""
                  }`}
                  onClick={() => setDisplayPrefs({ pressureUnit: option.value })}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </div>

          <div className="field-row">
            <span className="pref-label">Wind speed</span>
            <div className="window-switch" role="radiogroup" aria-label="Wind unit">
              {WIND_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={prefs.windUnit === option.value}
                  className={`window-btn${
                    prefs.windUnit === option.value ? " active" : ""
                  }`}
                  onClick={() => setDisplayPrefs({ windUnit: option.value })}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </div>
        </div>
        <p className="pref-note">
          Knots is the default deliberately: the airband radio, the ADS-B and the
          aerodromes around these sites all speak knots, and a wind in km/h
          beside an aircraft&rsquo;s groundspeed in knots is a conversion to do
          in your head at the worst moment.
        </p>
      </section>

      <section className="settings-section">
        <h3>Map labels</h3>
        <p className="pref-note">
          What to show beneath each aircraft that is not selected. Registration
          is looked up per aircraft, so it asks a little more of the link than
          the fields the transponder already sends.
        </p>
        <div className="pref-checks">
          {LABEL_FIELDS.map((field) => (
            <label key={field.key} className="pref-check">
              <input
                type="checkbox"
                checked={prefs.labelFields.includes(field.key)}
                onChange={(e) => toggleField(field.key, e.target.checked)}
              />
              <span>{field.label}</span>
            </label>
          ))}
        </div>
      </section>

      <section className="settings-section">
        <h3>Proximity alert</h3>
        <p className="pref-note">
          A contact within this range <em>and</em> below this altitude is drawn
          red — close and low, the traffic worth noticing. Both conditions must
          hold, and an aircraft that is not reporting its altitude is never
          flagged. This is your view; the station keeps its own alerting.
        </p>
        <div className="pref-fields">
          <label className="pref-field">
            <span>Within</span>
            <input
              type="number"
              min={1}
              max={300}
              step={1}
              value={prefs.criticalRangeKm}
              onChange={(e) => {
                const v = Number(e.target.value);
                if (Number.isFinite(v) && v > 0) {
                  setDisplayPrefs({ criticalRangeKm: v });
                }
              }}
            />
            <span className="pref-unit">km</span>
          </label>
          <label className="pref-field">
            <span>Below</span>
            <input
              type="number"
              min={100}
              max={60000}
              step={100}
              value={prefs.criticalAltitudeFt}
              onChange={(e) => {
                const v = Number(e.target.value);
                if (Number.isFinite(v) && v > 0) {
                  setDisplayPrefs({ criticalAltitudeFt: v });
                }
              }}
            />
            <span className="pref-unit">ft</span>
          </label>
        </div>
      </section>
    </div>
  );
}
