/** Mirrors the server. Kept in one file so a protocol change breaks the build
 *  loudly rather than surfacing as an undefined at 3am on a live console. */

export type Capability =
  | "station.view"
  | "telemetry.view"
  | "video.view"
  | "video.ptz"
  | "radio.listen"
  | "radio.control"
  | "radio.transmit"
  | "light.control"
  | "media.review"
  | "config.write";

export interface Me {
  user_id: string;
  email: string;
  display_name: string;
  organization_id: string;
  organization_name: string;
  roles: string[];
  /** Deployment is showing synthetic data. Badged everywhere, and suppresses
   *  sensor-fault indication - in demo the simulator is the sensor. */
  /** Deployment-wide override: badge and suppress faults on every station. The
   *  per-station `is_simulated` is the normal mechanism; this forces it on. */
  demo_mode: boolean;
  /** True only while the active org IS the platform org — a property of the
   *  session, not the person. A platform admin working inside a customer's org
   *  is bound by RLS exactly like its own members. */
  is_platform_admin: boolean;
  /** Working inside an organisation you are not a member of, reached through
   *  platform access. Someone else's tenant. */
  is_guest: boolean;
}

export interface StationSummary {
  id: string;
  name: string;
  timezone: string;
  latitude: number | null;
  longitude: number | null;
  last_seen_at: string | null;
  online: boolean;
  /** This station's data is synthetic. Per-station, because a deployment is
   *  routinely both at once — a real station alongside simulated ones — and a
   *  global flag had to be wrong about one of them. */
  is_simulated: boolean;
}

export interface DeviceSummary {
  id: string;
  kind: string;
  slug: string;
  name: string;
}

export interface StationDetail extends StationSummary {
  capabilities: Capability[];
  devices: DeviceSummary[];
}

/* ---- WebSocket protocol ---- */

export type ClientMessage =
  | { type: "select_station"; ground_station_id: string }
  | { type: "subscribe"; stream: StreamName }
  | { type: "unsubscribe"; stream: StreamName }
  | { type: "ping" };

export type StreamName = "status" | "telemetry" | "audio";

export type ServerMessage =
  | { type: "hello"; user_id: string; organization_id: string; stations: string[] }
  | { type: "station_selected"; ground_station_id: string; capabilities: Capability[] }
  | { type: "subscribed"; stream: StreamName }
  | { type: "unsubscribed"; stream: StreamName }
  | { type: "event"; stream: StreamName; station_id: string; payload: EventPayload }
  | { type: "status"; station_id: string; payload: StatusPayload }
  | { type: "station_revoked"; reason: string }
  | { type: "revoked"; reason: string }
  | { type: "error"; code: string; message: string }
  | { type: "pong" };

/** Telemetry payloads, discriminated by `kind`. */
export interface AudioPayload {
  kind: "audio";
  rate: number;
  /** Base64 int16 little-endian PCM. Only sent while the squelch is open. */
  pcm: string;
}

/** One MJPEG frame. Whole independent frames rather than an encoded stream:
 *  a dropped frame costs one frame, there is no keyframe to wait for after a
 *  Starlink dropout, and it renders in an <img> with no player. See
 *  contract/schemas/video.schema.json for why that is right for this camera and
 *  wrong for smooth full-rate video. */
export interface VideoPayload extends Availability {
  kind: "video";
  format?: "mjpeg";
  jpeg?: string;
  width?: number;
  height?: number;
  /** When the frame was TAKEN, not when it arrived. */
  captured_at?: string;
}

export type EventPayload =
  | VideoPayload
  | AudioPayload
  | HealthPayload
  | AdsbPayload
  | WeatherPayload
  | PowerPayload
  | RadioPayload
  | LightPayload
  | { kind: string; [k: string]: unknown };

export interface Aircraft {
  icao: string;
  callsign: string | null;
  /** ADS-B reports position directly; these are what the map plots. Null while
   *  only a Mode S response has been heard and no position yet. */
  latitude: number | null;
  longitude: number | null;
  /* The unit is in the name, and was not until contract 2.0. Every other
     measured value carried its unit and the contact object was the exception:
     `altitude` (metres) sat beside `altitude_corrected_m`, and `speed` (knots)
     beside `vertical_speed` (metres per second). Aviation convention is feet,
     so an unsuffixed altitude was a 3.28x error waiting to happen in the
     highest-volume payload here. */
  altitude_m: number | null;
  track_deg: number | null;
  speed_kt: number | null;
  /** Kilometres from the station. */
  range_km: number;
  /** Degrees true, from the station to the aircraft. */
  bearing_deg: number;
  /** Set when the aircraft is close enough to be worth flagging. */
  alert?: boolean;

  /* Everything below is nullable because the receiver attaches a validity flag
     to each one, and the flag is it telling us which of "the value is zero" and
     "there is no value" it means. A squawk of 0000 is a real code and 0 kt is
     an aircraft that has stopped, so the console must never render a null as a
     zero — `field ?? 0` anywhere in here is a bug. */

  /** `pressure` (referenced to 1013.25 hPa, not local QNH) or `geometric`.
   *  Null when the receiver did not say, which is why `altitude_m` alone
   *  cannot be labelled. */
  altitude_type?: string | null;
  /** The pressure altitude re-referenced to the station's own barometer, when
   *  that correction is switched on and possible. Carried *beside*
   *  `altitude_m`,
   *  never instead of it: what the receiver said and what it means locally are
   *  two facts, and a panel that shows only one cannot show its working. */
  altitude_corrected_m?: number | null;
  /** Metres per second, positive climbing. */
  vertical_speed_ms?: number | null;
  /** `ADSB_EMITTER_TYPE` as reported, unmapped — naming it is this console's
   *  job (`emitterKind`). 0 means the receiver was not told, which is a
   *  different statement from a category it does not recognise. */
  emitter_type?: number | null;
  /** Mode A as an integer, so 7700 is 7700 and 0 is the code 0000. */
  squawk?: number | null;
  /** `tslc`. A contact still drawn at 30 seconds is a memory, not an aircraft. */
  seconds_since_contact?: number | null;
  on_ground?: boolean | null;
  /** The receiver flagged this as an injected test target. Shown, so a test
   *  transmission can never be read as traffic. */
  simulated?: boolean | null;
  /** `adsb` (1090ES) or `uat` (978 MHz). */
  source?: string | null;
}

/** Carried by every telemetry payload. `available: false` says the station has
 *  no source for this stream — no receiver fitted, or one that has failed — and
 *  is the only honest way to distinguish that from a working sensor with
 *  nothing to report. An empty `aircraft` array means clear airspace. */
export interface Availability {
  available?: boolean;
  unavailable_reason?: string;
  /** The same fact as `unavailable_reason`, in one word worth branching on.
   *  `not_fitted` is a complete station with nothing selected for this slot and
   *  is not a fault; the other two are. Carried per frame rather than only in
   *  health, because health is every 30 s and a console that has just switched
   *  station has to show something now. Absent means unknown. */
  unavailable_cause?: "not_fitted" | "not_detected" | "stopped";
}

export interface AdsbPayload extends Availability {
  kind: "adsb";
  /** Absent when the stream is unavailable. Never contains a contact without a
   *  position — ADS-B exists to transmit position, so a positionless return is
   *  counted station-side and dropped rather than sent with nulls. */
  aircraft?: Aircraft[];
}

export interface WeatherPayload extends Availability {
  kind: "weather";
  wind_kt: number;
  gust_kt: number;
  /** Degrees true, the direction the wind is coming FROM - meteorological
   *  convention, and the opposite of a movement vector. Getting this backwards
   *  is the classic wind-rose bug. */
  wind_dir_deg: number;
  temperature_c: number;
  /** Optional: the fitted instrument may have no humidity module. Absent means
   *  no sensor, which the console strikes through — it is not the same as a
   *  reading that has not arrived yet. */
  humidity_pct?: number | null;
  pressure_hpa: number | null;
  visibility_km: number | null;
  /** Present-weather state, reported by the station rather than inferred here -
   *  the console must not contradict the numbers beside it. */
  sky?: "clear" | "partly" | "cloudy" | "rain" | "fog";
  is_day?: boolean;
  /** Tipping-bucket gauge: instantaneous rate, and the running total since
   *  local midnight. The total is the one that matters for ground conditions. */
  rain_rate_mmh?: number;
  rain_mm_today?: number;
}

export interface PowerPayload extends Availability {
  kind: "power";
  /** Battery state of charge, 0-100. */
  soc_pct: number;
  battery_v: number;
  /** Watts in from the array. */
  pv_w: number;
  /** Watts out to the load. Negative means the battery is charging. */
  load_w: number;
  /** Hours of runtime left at the current draw, null while charging. */
  runtime_h: number | null;

  /** Signed: positive charging, negative discharging. Sent by the station
   *  rather than derived here — with four sources the console cannot work out
   *  the battery's direction without knowing conversion losses and which
   *  source is carrying the load, so anything it computed would be a guess. */
  battery_w?: number;

  /* Mains and generator are **absent when not fitted**, never zero. A site
     with no grid connection and a site whose grid has failed are completely
     different situations and `mains_w: 0` describes both — so `undefined` here
     means "no such source at this site" and a number, including zero, is a
     measurement from something that exists. Checking these with `??` or a
     falsy test collapses the distinction and makes every off-grid station look
     like it has lost power. */
  mains_w?: number;
  mains_present?: boolean;
  generator_w?: number;
  generator_running?: boolean;
}

export interface RadioPayload extends Availability {
  kind: "radio";
  freq_hz: number;
  rssi_db: number;
  noise_floor_db: number;
  threshold_db: number;
  squelch_open: boolean;
  /** Whether the station is riding the squelch above its noise floor. Comes
   *  from the station, not the console - the button reflects hardware state
   *  rather than what someone last clicked. */
  auto_squelch: boolean;
  /** Squelch defeated by an operator holding monitor. */
  monitor?: boolean;
  /** Per-bin power in dBFS across `span_hz`, centred on `freq_hz`. Present
   *  only while a console has asked for it — the array is around 150 MB a day
   *  at this stream's rate on a metered link, for a display open for minutes
   *  at commissioning. */
  spectrum?: number[];
  span_hz?: number;
  /** Tuner gain: "auto" or a fixed dB value, and what the hardware offers. */
  gain?: string | number;
  gains?: number[];
  /** Crystal correction in parts per million, trimmed once at commissioning. */
  ppm?: number;
  /** False until certified transmit hardware exists. Always false today. */
  tx_capable: boolean;
}

export type DeviceStatus =
  | "present"
  /** Nothing was ever meant to be here. Not a fault. */
  | "not_fitted"
  /** Specified and cannot be found. */
  | "configured_absent"
  /** Was found and has stopped answering. */
  | "stalled"
  | "unsupported";

export interface HealthDevice {
  slot: string;
  label?: string;
  status: DeviceStatus;
  detail?: string;
  simulated?: boolean;
  telemetry_kind?: string | null;
  absent?: string[];
}

export interface HealthCondition {
  id: string;
  severity?: "info" | "warning" | "critical";
  detail?: string;
}

/** The station describing itself rather than its surroundings. Carries the
 *  structured form of things the other streams can only say in prose — notably
 *  `devices[].status`, which separates "never fitted" from "stopped answering".
 *  Those need different reactions and must not be rendered the same way. */
export interface HealthPayload extends Availability {
  kind: "health";
  status?: "ok" | "degraded" | "failing";
  agent_version?: string;
  config_version?: number;
  uptime_s?: number;
  conditions?: HealthCondition[];
  uplink?: { connected?: boolean; dropped_frames?: number; offline_seconds?: number };
  credential?: { expires_at?: string; renewal_failures?: number };
  devices?: HealthDevice[];
  unsourced_streams?: string[];
  unsourced_fields?: Record<string, string[]>;
  storage?: Record<string, number>;
  /** How often this station publishes each stream, in seconds. The console
   *  derives its staleness thresholds from this rather than assuming the
   *  contract's defaults, because `weather_period_s` is a site setting a
   *  metered link may legitimately raise. Absent on an older agent, which
   *  falls back to the contract cadence. */
  cadence?: Partial<Record<string, number>>;
}

export interface LightPayload extends Availability {
  kind: "light";
  on: boolean;
}

export interface StatusPayload {
  online?: boolean;
  alarm?: string;
  severity?: "info" | "warning" | "critical";
  [k: string]: unknown;
}

export interface BasemapOption {
  key: string;
  label: string;
  max_zoom: number;
  attribution: string;
  /** Raster street maps are drawn for white paper and get inverted for the dark
   *  console. Imagery must never be inverted - it would show false colour. */
  invert_for_dark: boolean;
}

export interface MapConfig {
  latitude: number | null;
  longitude: number | null;
  min_zoom: number;
  max_zoom: number;
  radius_km: number;
  /** Null until an operator has run a deliberate prefetch. Tiles still arrive
   *  via cache-through when live_fetch is on. */
  cached_at: string | null;
  default_basemap: string;
  basemaps: BasemapOption[];
  live_fetch: boolean;
}

/* ---- Settings ---- */

export interface StationConfig {
  id: string;
  name: string;
  timezone: string;
  latitude: number | null;
  longitude: number | null;
  map_min_zoom: number;
  elevation_m: number | null;
  map_max_zoom: number;
  map_radius_km: number;
  is_simulated: boolean;
  config_version: number;
}

export interface EnrolmentStatus {
  station_id: string;
  enrolled: boolean;
  enrolled_at: string | null;
  /** What the box reported about itself at its last claim. Inventory only —
   *  nothing here decides what the station is allowed to do. */
  hardware: Record<string, string> | null;
  config_version: number;
  credential_expires_at: string | null;
  credential_valid: boolean;
  /** False means the credential works but the broker was never told about it —
   *  enrolment is fail-soft, so this is how an operator finds the ones to retry. */
  broker_provisioned: boolean;
  token_outstanding: boolean;
  /** Distinguishes "waiting for a technician" from "already used, retry window
   *  still open". */
  token_claimed: boolean;
  token_expires_at: string | null;
}

export interface IssuedToken {
  /** Shown once. The server keeps only a hash and cannot show it again. */
  token: string;
  expires_at: string;
  /** The same code with this platform's address and CA fingerprint folded in,
   *  as `CODE@host#sha256`, for `bootstrap.sh --enrol`. All three had to reach
   *  the box anyway; the fingerprint was the one easiest to skip, and it is
   *  the one that decides whether the code goes to the real platform.
   *
   *  Empty on a platform that pins no CA — then there is nothing to carry. */
  bootstrap: string;
}

export interface MemberGrant {
  ground_station_id: string;
  capabilities: Capability[];
  expires_at: string | null;
}

export interface Member {
  /** Has completed second-factor enrolment. Never the secret. */
  mfa_enabled?: boolean;
  user_id: string;
  email: string;
  display_name: string;
  is_active: boolean;
  roles: string[];
  grants: MemberGrant[];
}

export interface OrganizationDetail {
  /** Members must present a second factor to sign in. */
  mfa_required?: boolean;
  id: string;
  name: string;
  members: Member[];
  stations: { id: string; name: string; is_active: boolean }[];
  /** radio.transmit is deliberately absent and stays absent until certified
   *  transmit hardware exists. */
  grantable_capabilities: Capability[];
  roles: string[];
}

export interface OrganizationOption {
  id: string;
  name: string;
  is_platform: boolean;
  /** False when reached through platform access rather than a membership —
   *  it is someone else's tenant and you are working inside it. */
  is_member: boolean;
}

/* ---- Platform administration ---- */

export interface PlatformOrg {
  id: string;
  name: string;
  is_platform: boolean;
  member_count: number;
  station_count: number;
}

export interface PlatformUser {
  user_id: string;
  email: string;
  display_name: string;
  is_active: boolean;
  is_platform_admin: boolean;
  memberships: {
    organization_id: string;
    organization_name: string;
    roles: string[];
  }[];
}

export interface PlatformOverview {
  organizations: PlatformOrg[];
  users: PlatformUser[];
  roles: string[];
}

/**
 * Returned by login when a second factor is still outstanding.
 *
 * `mfa_required` - they have an authenticator; ask for the code.
 * `mfa_enrollment_required` - their organisation requires MFA and they have not
 * set it up; the QR and secret are for scanning, and enrolment completes when
 * they send back a working code.
 */
export interface LoginChallenge {
  status: "mfa_required" | "mfa_enrollment_required";
  secret?: string | null;
  otpauth_uri?: string | null;
  qr_svg?: string | null;
}

export function isChallenge(v: Me | LoginChallenge): v is LoginChallenge {
  return "status" in v;
}
