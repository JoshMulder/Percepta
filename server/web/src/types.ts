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

export type StreamName = "status" | "telemetry" | "video" | "audio";

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

export type EventPayload =
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
  /** Metres. */
  altitude: number | null;
  /** Degrees true. */
  track: number | null;
  /** Knots. */
  speed: number | null;
  /** Kilometres from the station. */
  range_km: number;
  /** Degrees true, from the station to the aircraft. */
  bearing: number;
  /** Set when the aircraft is close enough to be worth flagging. */
  alert?: boolean;
}

/** Carried by every telemetry payload. `available: false` says the station has
 *  no source for this stream — no receiver fitted, or one that has failed — and
 *  is the only honest way to distinguish that from a working sensor with
 *  nothing to report. An empty `aircraft` array means clear airspace. */
export interface Availability {
  available?: boolean;
  unavailable_reason?: string;
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
}

export interface MemberGrant {
  ground_station_id: string;
  capabilities: Capability[];
  expires_at: string | null;
}

export interface Member {
  user_id: string;
  email: string;
  display_name: string;
  is_active: boolean;
  roles: string[];
  grants: MemberGrant[];
}

export interface OrganizationDetail {
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
