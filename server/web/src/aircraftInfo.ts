import { useEffect, useState } from "react";
import { api } from "./api";

export interface AircraftInfo {
  icao: string;
  registration: string | null;
  type_code: string | null;
  model: string | null;
  manufacturer: string | null;
  operator: string | null;
}

/**
 * The registration/model lookup, cached once for the whole console.
 *
 * A tail number and a model do not change for an airframe, so a hex fetched once
 * is kept for the life of the tab. Shared between the contact card (which asks
 * on open) and the map (which asks for every visible contact, but only when the
 * operator has put registration on the label) so the two never fetch the same
 * hex twice. `inflight` dedupes concurrent asks for the same hex — the map opens
 * a card for a contact it is already looking up, and only one request goes out.
 */
const cache = new Map<string, AircraftInfo>();
const inflight = new Map<string, Promise<AircraftInfo>>();

/** The cached record for a hex, or undefined if it has not been fetched. Sync,
 *  for the map's marker loop, which cannot await. */
export function cachedAircraftInfo(icao: string): AircraftInfo | undefined {
  return cache.get(icao);
}

export function fetchAircraftInfo(icao: string): Promise<AircraftInfo> {
  const hit = cache.get(icao);
  if (hit) return Promise.resolve(hit);
  const pending = inflight.get(icao);
  if (pending) return pending;
  const request = api
    .aircraftInfo(icao)
    .then((data) => {
      cache.set(icao, data);
      inflight.delete(icao);
      return data;
    })
    .catch((error) => {
      inflight.delete(icao);
      throw error;
    });
  inflight.set(icao, request);
  return request;
}

/** The record for a contact card, fetched on open. Null until it resolves; a
 *  failed lookup stays null and the card falls back to the emitter category. */
export function useAircraftInfo(icao: string): AircraftInfo | null {
  const [info, setInfo] = useState<AircraftInfo | null>(
    () => cache.get(icao) ?? null,
  );
  useEffect(() => {
    const hit = cache.get(icao);
    if (hit) {
      setInfo(hit);
      return;
    }
    let cancelled = false;
    setInfo(null);
    fetchAircraftInfo(icao)
      .then((data) => !cancelled && setInfo(data))
      .catch(() => {
        /* silence — the card shows the category regardless */
      });
    return () => {
      cancelled = true;
    };
  }, [icao]);
  return info;
}

/** Test-only: empty the cache so one test's lookup does not answer another's. */
export function _resetAircraftInfo(): void {
  cache.clear();
  inflight.clear();
}
