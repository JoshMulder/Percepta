import { memo } from "react";
import { SKY_LABEL, SkyIcon } from "./Icons";
import { NoSource } from "./PanelState";
import type { WeatherPayload } from "../types";

const POINTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];

function cardinal(deg: number): string {
  return POINTS[Math.round((deg % 360) / 45) % 8];
}

/**
 * Wind rose plus the numbers a station operator actually reads.
 *
 * The needle points the way the wind is going, while the label reports the
 * direction it comes from - which is the meteorological convention and what a
 * METAR or a weather station reports. Showing one without the other is how
 * these displays end up 180 degrees wrong.
 */
function WeatherPanelInner({ weather }: { weather: WeatherPayload | null }) {
  // Dashes, not an early return: the panel has to hold its full height with no
  // data so the sidebar's scale does not change when the first reading lands.
  const has = weather !== null;
  const from = weather?.wind_dir_deg ?? 0;
  const gusting = has && weather.gust_kt > weather.wind_kt + 3;
  // Beaufort-ish banding, chosen for what matters at a remote mast: settled,
  // brisk, then strong enough to worry about the structure and any airframe.
  // Met thresholds: light below 2.5 mm/h, heavy above 10.
  const rainRate = weather?.rain_rate_mmh ?? 0;
  const rainBand =
    !has || rainRate === 0
      ? ""
      : rainRate < 2.5
        ? "rain-light"
        : rainRate < 10
          ? "rain-mod"
          : "rain-heavy";

  const level = !has
    ? ""
    : weather.gust_kt >= 34
      ? "critical"
      : weather.gust_kt >= 22
        ? "warn"
        : "";

  return (
    <div className="weather">
      <div className="rose-wrap">
        <svg viewBox="0 0 120 120" className="rose" role="img"
             aria-label={
               has
                 ? `Wind ${weather.wind_kt.toFixed(0)} knots from ${cardinal(from)}`
                 : "Wind, no reading"
             }>
          <circle cx="60" cy="60" r="52" className="rose-ring" />
          <circle cx="60" cy="60" r="34" className="rose-ring inner" />

          {POINTS.map((label, i) => {
            const angle = (i * 45 * Math.PI) / 180;
            const x = 60 + Math.sin(angle) * 46;
            const y = 60 - Math.cos(angle) * 46;
            return (
              <text
                key={label}
                x={x}
                y={y + 3}
                className={`rose-point${i % 2 ? " minor" : ""}`}
                textAnchor="middle"
              >
                {label}
              </text>
            );
          })}

          {Array.from({ length: 24 }, (_, i) => {
            const angle = (i * 15 * Math.PI) / 180;
            const outer = i % 2 === 0 ? 36 : 33;
            return (
              <line
                key={i}
                x1={60 + Math.sin(angle) * 30}
                y1={60 - Math.cos(angle) * 30}
                x2={60 + Math.sin(angle) * outer}
                y2={60 - Math.cos(angle) * outer}
                className="rose-tick"
              />
            );
          })}

          {/* Arrow points downwind: rotate by the "from" bearing plus 180. */}
          <g transform={`rotate(${from + 180} 60 60)`} className={`rose-arrow ${level}`}>
            <path d="M60 30 L67 62 L60 56 L53 62 Z" />
            <line x1="60" y1="56" x2="60" y2="88" />
          </g>

          <circle cx="60" cy="60" r="3.5" className="rose-hub" />
        </svg>

        <div className="rose-readout">
          <div className={`wind-speed ${level}`}>
            {has ? weather.wind_kt.toFixed(0) : "--"}
            <span className="unit">kt</span>
          </div>
          <div className="wind-from">
            {has ? `from ${cardinal(from)} ${from.toFixed(0)}°` : "no reading"}
          </div>
          {/* Always rendered, hidden when not gusting. Letting the line come
              and go changed the panel's height, which rescaled the entire
              sidebar every time the wind eased. */}
          <div className={`wind-gust ${level}${gusting ? "" : " hidden"}`}>
            gusting {has ? weather.gust_kt.toFixed(0) : "--"} kt
          </div>
        </div>

        {/* Sky state sits to the right of the wind readout, at the end of the
            row. An icon rather than a word because it is the one thing on this
            panel read from across a room; the label keeps it unambiguous. */}
        <div className="sky-state" title={has ? SKY_LABEL[weather.sky ?? "clear"] : ""}>
          <SkyIcon state={weather?.sky} isDay={weather?.is_day ?? true} />
          <span>{has ? SKY_LABEL[weather.sky ?? "clear"] : "—"}</span>
        </div>
      </div>

      <dl className="stats weather-stats">
        <div>
          <dt>Temp</dt>
          <dd>{has ? `${weather.temperature_c.toFixed(1)} °C` : "--"}</dd>
        </div>
        <div>
          <dt>Humidity</dt>
          {/* Optional in the contract: the fitted instrument may have no RH
              module. Absent means no sensor, which is a different thing from a
              reading that has not arrived. */}
          <dd>
            {!has ? (
              "--"
            ) : weather.humidity_pct === undefined || weather.humidity_pct === null ? (
              <NoSource what="humidity" />
            ) : (
              `${weather.humidity_pct.toFixed(0)} %`
            )}
          </dd>
        </div>
        <div>
          <dt>Pressure</dt>
          <dd>
            {!has ? (
              "--"
            ) : weather.pressure_hpa === undefined || weather.pressure_hpa === null ? (
              <NoSource what="pressure" />
            ) : (
              `${weather.pressure_hpa.toFixed(0)} hPa`
            )}
          </dd>
        </div>
        <div>
          <dt>Visibility</dt>
          <dd>
            {!has ? (
              "--"
            ) : weather.visibility_km === undefined || weather.visibility_km === null ? (
              <NoSource what="visibility" />
            ) : (
              `${weather.visibility_km.toFixed(0)} km`
            )}
          </dd>
        </div>
        {/* Rainfall reads as two more numbers alongside the rest rather than a
            gauge of its own. Total first: the rate flickers, while the total is
            what says whether a track is passable. */}
        <div>
          <dt>Rain today</dt>
          <dd>
            {!has ? (
              "--"
            ) : weather.rain_mm_today === undefined || weather.rain_mm_today === null ? (
              <NoSource what="rainfall" />
            ) : (
              `${weather.rain_mm_today.toFixed(1)} mm`
            )}
          </dd>
        </div>
        <div>
          <dt>Rain rate</dt>
          <dd className={rainBand}>
            {!has ? (
              "--"
            ) : weather.rain_rate_mmh === undefined || weather.rain_rate_mmh === null ? (
              <NoSource what="rain rate" />
            ) : (
              `${weather.rain_rate_mmh.toFixed(1)} mm/h`
            )}
          </dd>
        </div>
      </dl>

    </div>
  );
}

/**
 * Memoised. Telemetry arrives on several streams at about 1 Hz each, so the
 * console re-renders a few times a second; without this every panel re-rendered
 * on every frame regardless of whose data it was. The map is the expensive one -
 * reconciling it also re-ran its contact update.
 */
export const WeatherPanel = memo(WeatherPanelInner);
