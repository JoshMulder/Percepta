import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type { StationConfig } from "../types";

/** Where the station is, what it is called, and how much basemap to hold.
 *
 *  The zoom and radius controls are the ones originally asked for and never
 *  built: a fixed site has a finite tile set, so these decide how much of it the
 *  platform keeps. */
export function SettingsStation({
  stationId,
  stationName,
  onSaved,
}: {
  stationId: string;
  stationName: string | null;
  onSaved: () => void;
}) {
  const [config, setConfig] = useState<StationConfig | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setConfig(null);
    setLoadError(null);
    api
      .stationConfig(stationId)
      .then((c) => !cancelled && setConfig(c))
      .catch((err) => {
        if (cancelled) return;
        setLoadError(
          err instanceof ApiError ? err.message : "Could not load configuration.",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [stationId]);

  function set<K extends keyof StationConfig>(key: K, value: StationConfig[K]) {
    setConfig((c) => (c ? { ...c, [key]: value } : c));
    setMessage(null);
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!config) return;
    setSaving(true);
    setMessage(null);
    setError(null);
    try {
      const saved = await api.saveStationConfig(stationId, {
        name: config.name,
        timezone: config.timezone,
        latitude: config.latitude,
        longitude: config.longitude,
        map_min_zoom: config.map_min_zoom,
        map_max_zoom: config.map_max_zoom,
        map_radius_km: config.map_radius_km,
      });
      setConfig(saved);
      setMessage("Saved.");
      // The station list in the header carries the name, so a rename has to
      // reach it or the console keeps showing the old one until a reload.
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save.");
    } finally {
      setSaving(false);
    }
  }

  if (loadError) return <p className="settings-error">{loadError}</p>;
  if (!config) return <p className="settings-note">Loading…</p>;

  const zoomInverted = config.map_min_zoom > config.map_max_zoom;

  return (
    <div className="settings-sections">
      <form onSubmit={save}>
        <section className="settings-section">
          <h3>{stationName ?? "Station"}</h3>
          <label className="field">
            <span>Name</span>
            <input
              value={config.name}
              onChange={(e) => set("name", e.target.value)}
              maxLength={255}
              required
            />
          </label>
          <label className="field">
            <span>Timezone</span>
            <input
              value={config.timezone}
              onChange={(e) => set("timezone", e.target.value)}
              placeholder="Pacific/Auckland"
              required
            />
            <small>
              An IANA zone. The station is remote and an operator may be
              elsewhere, so local time is a property of the site.
            </small>
          </label>
          <div className="field-row">
            <label className="field">
              <span>Latitude</span>
              <input
                type="number"
                step="0.00001"
                min={-90}
                max={90}
                value={config.latitude ?? ""}
                onChange={(e) =>
                  set("latitude", e.target.value === "" ? null : Number(e.target.value))
                }
              />
            </label>
            <label className="field">
              <span>Longitude</span>
              <input
                type="number"
                step="0.00001"
                min={-180}
                max={180}
                value={config.longitude ?? ""}
                onChange={(e) =>
                  set("longitude", e.target.value === "" ? null : Number(e.target.value))
                }
              />
            </label>
          </div>
          <small className="settings-note">
            The map centres here and range rings are measured from it. Moving it
            moves everything the operator judges distance by.
          </small>
        </section>

        <section className="settings-section">
          <h3>Basemap cache</h3>
          <div className="field-row">
            <label className="field">
              <span>Minimum zoom</span>
              <input
                type="number"
                min={3}
                max={19}
                value={config.map_min_zoom}
                onChange={(e) => set("map_min_zoom", Number(e.target.value))}
                required
              />
            </label>
            <label className="field">
              <span>Maximum zoom</span>
              <input
                type="number"
                min={3}
                max={19}
                value={config.map_max_zoom}
                onChange={(e) => set("map_max_zoom", Number(e.target.value))}
                required
              />
            </label>
            <label className="field">
              <span>Radius (km)</span>
              <input
                type="number"
                min={1}
                max={200}
                step="1"
                value={config.map_radius_km}
                onChange={(e) => set("map_radius_km", Number(e.target.value))}
                required
              />
            </label>
          </div>
          {zoomInverted && (
            <p className="settings-error">
              Minimum zoom cannot be greater than maximum zoom.
            </p>
          )}
          <p className="settings-note">
            The station does not move, so its basemap is a finite set of tiles.
            Tile count grows fourfold per zoom level and with the square of the
            radius, so maximum zoom is by far the expensive control — going from
            17 to 19 is roughly sixteen times the tiles.
          </p>
        </section>

        <div className="settings-actions">
          <button
            type="submit"
            className="btn primary"
            disabled={saving || zoomInverted || !config.name.trim()}
          >
            {saving ? "Saving…" : "Save"}
          </button>
          {message && <span className="settings-ok">{message}</span>}
          {error && <span className="settings-error">{error}</span>}
        </div>
      </form>
    </div>
  );
}
