import type {
  EnrolmentStatus,
  IssuedToken,
  LoginChallenge,
  MapConfig,
  Me,
  OrganizationDetail,
  OrganizationOption,
  PlatformOrg,
  PlatformOverview,
  PlatformUser,
  StationConfig,
  StationDetail,
  StationSummary,
} from "./types";

/** Thrown for any non-2xx. `status` is carried so callers can tell an expired
 *  session (401) from a station that is not available (404) without parsing
 *  message text. */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    // Same-origin, so the HttpOnly session cookie rides along and no token ever
    // touches JavaScript.
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...init,
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body; the status is the useful part */
    }
    throw new ApiError(response.status, detail);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  /** Either a session or a challenge. The password was correct in both cases -
   *  a 200 carrying `status` means the second factor is outstanding, which the
   *  caller must be able to tell apart from a rejected password. */
  login: (email: string, password: string, mfaCode?: string) =>
    request<Me | LoginChallenge>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password, mfa_code: mfaCode ?? null }),
    }),

  logout: () => request<void>("/api/auth/logout", { method: "POST" }),

  me: () => request<Me>("/api/auth/me"),

  organizations: () => request<OrganizationOption[]>("/api/auth/organizations"),

  /** Mints a new session and revokes the current one, so every socket open on
   *  the old organisation is closed server-side. The caller must re-bootstrap. */
  switchOrganization: (organizationId: string) =>
    request<Me>("/api/auth/organization", {
      method: "POST",
      body: JSON.stringify({ organization_id: organizationId }),
    }),

  stations: () => request<StationSummary[]>("/api/stations"),

  station: (id: string) => request<StationDetail>(`/api/stations/${id}`),

  mapConfig: (id: string) => request<MapConfig>(`/api/stations/${id}/map`),

  powerHistory: (id: string, hours: number) =>
    request<
      {
        t: string;
        soc: number;
        // Absent (null) at a site without that source — see the API's PowerPoint.
        pv?: number | null;
        load?: number | null;
        mains?: number | null;
        gen?: number | null;
      }[]
    >(`/api/stations/${id}/power/history?hours=${hours}`),

  /** Registration and type for an ADS-B contact, by its ICAO hex — the fields
   *  the transponder does not broadcast. Every field but `icao` is null for an
   *  aircraft no registry has; the platform caches the answer, so calling this
   *  per card open is cheap. */
  aircraftInfo: (icao: string) =>
    request<{
      icao: string;
      registration: string | null;
      type_code: string | null;
      model: string | null;
      manufacturer: string | null;
      operator: string | null;
    }>(`/api/aircraft/${encodeURIComponent(icao)}`),

  /* Commands. Each returns 202: the station has been told, and what it actually
     did arrives on the telemetry stream. Nothing here reports success on the
     hardware's behalf. */

  tune: (id: string, freqHz: number) =>
    request<{ accepted: boolean; freq_hz: number }>(
      `/api/stations/${id}/radio/tune`,
      { method: "POST", body: JSON.stringify({ freq_hz: freqHz }) },
    ),

  squelch: (id: string, db: number) =>
    request<{ accepted: boolean }>(`/api/stations/${id}/radio/squelch`, {
      method: "POST",
      body: JSON.stringify({ db }),
    }),

  autoSquelch: (id: string, on: boolean) =>
    request<{ accepted: boolean }>(`/api/stations/${id}/radio/auto-squelch`, {
      method: "POST",
      body: JSON.stringify({ on }),
    }),

  monitor: (id: string, on: boolean) =>
    request<{ accepted: boolean }>(`/api/stations/${id}/radio/monitor`, {
      method: "POST",
      body: JSON.stringify({ on }),
    }),

  setGain: (id: string, gain: string | number) =>
    request<{ accepted: boolean }>(`/api/stations/${id}/radio/gain`, {
      method: "POST",
      body: JSON.stringify({ gain }),
    }),

  /** Ask a station to include its spectrum in radio telemetry, or stop. Must
   *  be re-sent while the display is open; the station's window lapses on its
   *  own so a console that crashes stops the traffic without saying goodbye. */
  wantSpectrum: (id: string, on: boolean) =>
    request<{ accepted: boolean }>(`/api/stations/${id}/radio/spectrum`, {
      method: "POST",
      body: JSON.stringify({ on }),
    }),

  setPpm: (id: string, ppm: number) =>
    request<{ accepted: boolean }>(`/api/stations/${id}/radio/ppm`, {
      method: "POST",
      body: JSON.stringify({ ppm }),
    }),

  setLight: (id: string, on: boolean) =>
    request<{ accepted: boolean }>(`/api/stations/${id}/light`, {
      method: "POST",
      body: JSON.stringify({ on }),
    }),

  /* Settings. Everything below needs config.write at the station, or org admin
     for the organisation routes. A 404 here means "not yours", the same as
     everywhere else — the API never distinguishes that from "does not exist". */

  updateProfile: (displayName: string) =>
    request<{ user_id: string; email: string; display_name: string }>(
      "/api/account/profile",
      { method: "PATCH", body: JSON.stringify({ display_name: displayName }) },
    ),

  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ other_sessions_ended: number }>("/api/account/password", {
      method: "POST",
      body: JSON.stringify({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    }),

  createStation: (body: {
    name: string;
    timezone: string;
    latitude: number | null;
    longitude: number | null;
  }) =>
    request<StationSummary>("/api/stations", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  stationConfig: (id: string) =>
    request<StationConfig>(`/api/stations/${id}/config`),

  /** `is_simulated` and `enrolled` are read-only: the first is written from
   *  the station's own health frame, the second is a fact about the record.
   *  Sending either would be a value the server ignores. */
  saveStationConfig: (
    id: string,
    body: Omit<
      StationConfig,
      "id" | "config_version" | "is_simulated" | "enrolled"
    >,
  ) =>
    request<StationConfig>(`/api/stations/${id}/config`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  /** Only ever succeeds before a station has enrolled; the server refuses with
   *  409 afterwards. See the endpoint for why that line is where it is. */
  deleteStation: (id: string) =>
    request<void>(`/api/stations/${id}/config`, { method: "DELETE" }),

  enrolmentStatus: (id: string) =>
    request<EnrolmentStatus>(`/api/stations/${id}/enrolment`),

  issueEnrolmentToken: (id: string) =>
    request<IssuedToken>(`/api/stations/${id}/enrolment/token`, { method: "POST" }),

  revokeEnrolmentToken: (id: string) =>
    request<{ revoked: number }>(`/api/stations/${id}/enrolment/token`, {
      method: "DELETE",
    }),

  revokeStationCredentials: (id: string) =>
    request<{ revoked: number; broker_principal_removed: boolean }>(
      `/api/stations/${id}/enrolment/revoke`,
      { method: "POST" },
    ),

  /** Sixty seconds, single use, bound to one station. A browser cannot set
   *  headers on a WebSocket, so this is how the media socket is authorised. */
  streamTicket: (id: string) =>
    request<{ ticket: string; expires_in: number; url: string }>(
      `/api/stations/${id}/stream-ticket`,
      { method: "POST" },
    ),

  organization: () => request<OrganizationDetail>("/api/organization"),

  setMemberGrant: (userId: string, stationId: string, capabilities: string[]) =>
    request<{ ground_station_id: string; capabilities: string[] }>(
      `/api/organization/members/${userId}/grants`,
      {
        method: "PUT",
        body: JSON.stringify({
          ground_station_id: stationId,
          capabilities,
        }),
      },
    ),

  platform: () => request<PlatformOverview>("/api/platform"),

  createOrganization: (name: string) =>
    request<PlatformOrg>("/api/platform/organizations", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  createUser: (body: { email: string; display_name: string; password: string | null }) =>
    request<PlatformUser>("/api/platform/users", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  setMembership: (userId: string, organizationId: string, roles: string[]) =>
    request<{ user_id: string; organization_id: string; roles: string[] }>(
      `/api/platform/users/${userId}/memberships`,
      {
        method: "PUT",
        body: JSON.stringify({ organization_id: organizationId, roles }),
      },
    ),

  removeMembership: (userId: string, organizationId: string) =>
    request<{ removed: boolean; station_grants_removed: number }>(
      `/api/platform/users/${userId}/memberships/${organizationId}`,
      { method: "DELETE" },
    ),

  setMemberRoles: (userId: string, roles: string[]) =>
    request<{ user_id: string; roles: string[] }>(
      `/api/organization/members/${userId}/roles`,
      { method: "PUT", body: JSON.stringify({ roles }) },
    ),

  setOrgMfaRequired: (required: boolean) =>
    request<{ mfa_required: boolean }>("/api/organization/mfa", {
      method: "PUT",
      body: JSON.stringify({ mfa_required: required }),
    }),

  inviteMember: (email: string, displayName: string, roles: string[]) =>
    request<{
      user_id: string;
      email: string;
      roles: string[];
      invitation_sent: boolean;
    }>("/api/organization/members", {
      method: "POST",
      body: JSON.stringify({ email, display_name: displayName, roles }),
    }),

  sendPasswordReset: (userId: string) =>
    request<{ user_id: string; sent_to: string }>(
      `/api/organization/members/${userId}/password-reset`,
      { method: "POST" },
    ),

  /** Redeeming an emailed reset link. The only call made while signed out. */
  redeemPasswordReset: (token: string, newPassword: string) =>
    request<{ reset: boolean }>("/api/auth/password-reset/redeem", {
      method: "POST",
      body: JSON.stringify({ token, new_password: newPassword }),
    }),
};
