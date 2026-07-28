import type { MapConfig, Me, StationDetail, StationSummary } from "./types";

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
  login: (email: string, password: string) =>
    request<Me>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  logout: () => request<void>("/api/auth/logout", { method: "POST" }),

  me: () => request<Me>("/api/auth/me"),

  stations: () => request<StationSummary[]>("/api/stations"),

  station: (id: string) => request<StationDetail>(`/api/stations/${id}`),

  mapConfig: (id: string) => request<MapConfig>(`/api/stations/${id}/map`),

  powerHistory: (id: string, hours: number) =>
    request<{ t: string; soc: number }[]>(
      `/api/stations/${id}/power/history?hours=${hours}`,
    ),

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
};
