import { useDisplayPrefs } from "../displayPrefs";
import { weatherDisplay } from "../format";
import type { FleetStation } from "../types";

/**
 * What a station is, without spending anything to find out.
 *
 * HOVER IS FREE AND CLICK IS NOT — that split is the whole design. A mouse
 * crossing the map passes over many pins, so anything hover triggers happens
 * dozens of times a minute by accident. Everything here comes from the
 * `FleetStation` the wall is already holding: name, health, power, and the
 * weather that has been arriving all along. No request, no subscription, no
 * byte off any station's link. Clicking is what opens the drawer, attaches live
 * telemetry and can start video — deliberate, once, on purpose.
 *
 * READ-ONLY AND POINTER-TRANSPARENT. The card cannot be interacted with and does
 * not take the pointer, so moving toward it never steals the hover from the pin
 * underneath and starts a flicker loop. Anything worth clicking belongs in the
 * drawer.
 *
 * UNITS GO THROUGH THE OPERATOR'S PREFERENCES, like every other reading in the
 * console. The wire is knots and Celsius; what somebody reads is their own
 * choice, and a card that ignored that would be the one surface in the product
 * showing units nobody selected.
 */
export function StationHoverCard({
  station,
  x,
  y,
  flipX,
  flipY,
}: {
  station: FleetStation;
  /** Pixel position of the pin within the map holder. */
  x: number;
  y: number;
  /** Draw to the left / above instead, when the pin is near an edge. */
  flipX: boolean;
  flipY: boolean;
}) {
  const prefs = useDisplayPrefs();
  const wind = weatherDisplay("wind", prefs);
  const temp = weatherDisplay("temp", prefs);

  const status =
    station.status === "never"
      ? "never connected"
      : station.dark
        ? "dark"
        : station.status;

  const tone =
    station.dark || station.status === "never"
      ? "bad"
      : station.status === "offline"
        ? "warn"
        : (station.condition_count ?? 0) > 0
          ? "warn"
          : "";

  const where = [station.locality, station.region].filter(Boolean).join(", ");

  // Rendered only when there is something to say. A weather row of four dashes
  // is worse than no weather row: it implies a sensor that is failing rather
  // than a site that has none.
  const hasWeather =
    station.wind_kt != null ||
    station.temperature_c != null ||
    station.visibility_km != null;

  return (
    <div
      className={`fleet-hover${tone ? ` ${tone}` : ""}`}
      style={{
        left: flipX ? undefined : x,
        right: flipX ? undefined : undefined,
        top: flipY ? undefined : y,
        transform: `translate(${flipX ? "calc(-100% - 18px)" : "18px"}, ${
          flipY ? "calc(-100% - 6px)" : "-6px"
        })`,
      }}
      aria-hidden="true"
    >
      <div className="fleet-hover-head">
        <span className="fleet-hover-name">{station.name}</span>
        <span className={`fleet-hover-status ${tone}`}>{status}</span>
      </div>

      <div className="fleet-hover-where">
        {station.organization_name}
        {where ? ` · ${where}` : ""}
      </div>

      {(station.worst_condition || (station.condition_count ?? 0) > 0) && (
        <div className="fleet-hover-cond">
          {station.worst_condition ?? "condition"}
          {(station.condition_count ?? 0) > 1 && ` +${station.condition_count! - 1}`}
        </div>
      )}

      {hasWeather && (
        <div className="fleet-hover-wx">
          {station.wind_kt != null && (
            <span>
              {wind.convert(station.wind_kt).toFixed(wind.digits)}
              {station.gust_kt != null && station.gust_kt > station.wind_kt && (
                <>
                  {"–"}
                  {wind.convert(station.gust_kt).toFixed(wind.digits)}
                </>
              )}
              {wind.suffix}
            </span>
          )}
          {station.temperature_c != null && (
            <span>
              {temp.convert(station.temperature_c).toFixed(temp.digits)}
              {temp.suffix}
            </span>
          )}
          {station.visibility_km != null && (
            <span>{station.visibility_km.toFixed(0)} km vis</span>
          )}
          {station.sky && <span className="fleet-hover-sky">{station.sky}</span>}
        </div>
      )}

      {station.soc_pct != null && (
        <div className="fleet-hover-power">
          {station.soc_pct.toFixed(0)}%
          {station.on_battery ? " on battery" : ""}
        </div>
      )}

      <div className="fleet-hover-more">Click to open</div>
    </div>
  );
}
