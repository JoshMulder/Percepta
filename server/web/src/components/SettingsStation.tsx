import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type { StationConfig, StationSummary } from "../types";
import { SettingsEnrolment } from "./SettingsEnrolment";

/**
 * Everything about one station: pick it, configure it, enrol it.
 *
 * The selector is deliberately independent of the console's station switcher.
 * Configuring a site is an administrative job you do for whichever station
 * needs it, not for whichever one you happen to be watching.
 *
 * Creating a station lives on the Organisation tab, not here: a new record has
 * no telemetry and nothing to configure until it exists, so the flow is to name
 * it there and be brought straight to this tab, selected, to finish setup.
 */
export function SettingsStation({
  initialStationId,
  canCreate,
  onSaved,
}: {
  initialStationId: string | null;
  /** Only whether to point an empty pane at where stations are created. The
   *  create action itself is on the Organisation tab. */
  canCreate: boolean;
  onSaved: () => void;
}) {
  const [stations, setStations] = useState<StationSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(initialStationId);
  const [listError, setListError] = useState<string | null>(null);
  // Bumped whenever the enrolment section changes a credential, so the Delete
  // section re-asks whether it should be showing. The two are siblings reading
  // the same status, and only one of them was refetching it.
  const [credentialSeq, setCredentialSeq] = useState(0);

  const loadStations = useCallback(async (prefer?: string) => {
    try {
      const list = await api.stations();
      setStations(list);
      // The current selection survives only while the station does. `prefer ??
      // current ??` kept a deleted station's id, which is not in the new list,
      // so `stations.find` came back empty and the pane announced "No stations
      // yet" beside a picker holding several. Deleting one now moves to the
      // next; deleting the last leaves null, and the picker says so.
      setSelected((current) =>
        prefer ??
        (current && list.some((s) => s.id === current)
          ? current
          : list[0]?.id ?? null),
      );
      setListError(null);
    } catch (err) {
      setListError(err instanceof ApiError ? err.message : "Could not load stations.");
    }
  }, []);

  useEffect(() => {
    void loadStations();
  }, [loadStations]);

  if (listError) return <p className="settings-error">{listError}</p>;
  if (!stations) return <p className="settings-note">Loading…</p>;

  const station = stations.find((s) => s.id === selected) ?? null;

  return (
    <div className="settings-sections">
      <div className="station-picker">
        <label className="field">
          <span>Station</span>
          <select
            value={selected ?? ""}
            onChange={(e) => setSelected(e.target.value)}
          >
            {stations.length === 0 && <option value="">No stations yet</option>}
            {stations.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
                {s.is_simulated ? " · DEMO" : ""}
              </option>
            ))}
          </select>
        </label>
      </div>

      {station ? (
        <>
          <StationConfigForm
            key={station.id}
            stationId={station.id}
            onSaved={onSaved}
          />
          <SettingsEnrolment
            stationId={station.id}
            stationName={station.name}
            onCredentialChanged={() => setCredentialSeq((n) => n + 1)}
          />
          <DeleteStation
            key={`del-${station.id}`}
            credentialSeq={credentialSeq}
            station={station}
            onDeleted={async () => {
              await loadStations();
              onSaved();
            }}
          />
        </>
      ) : (
        <p className="settings-note">
          {canCreate
            ? "No stations yet. Add one from the Organisation tab to get started."
            : "No stations available to configure."}
        </p>
      )}
    </div>
  );
}

function StationConfigForm({
  stationId,
  onSaved,
}: {
  stationId: string;
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
          err instanceof ApiError && err.status === 404
            ? "You do not have configuration access to this station."
            : "Could not load configuration.",
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
        elevation_m: config.elevation_m,
        map_min_zoom: config.map_min_zoom,
        map_max_zoom: config.map_max_zoom,
        map_radius_km: config.map_radius_km,
      });
      setConfig(saved);
      setMessage("Saved.");
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
    <form onSubmit={save}>
      <section className="settings-section">
        <h3>Configuration</h3>
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
        </label>
        {/* Set here, before enrolment, and frozen by it.

            These were read-only with a note saying to set them on the
            station's own setup page. That page does not offer them: it says
            coordinates are "settled at commissioning and frozen with the
            enrolment" and has no field for either. So each side pointed at
            the other and there was nowhere at all to say where a station is —
            which left every bearing computed from nothing.

            This is that place. The API has always accepted these before
            enrolment and refused them after (409), for the owner's reason: a
            box that has moved is recommissioned rather than edited, or its
            history silently describes two sites. The form follows that rule
            rather than discovering it from an error.

            Elevation sits with the coordinates because it is part of the same
            fact. It is stored and reported but not currently used by any
            calculation — the ADS-B altitude correction that once used it has
            been removed. */}
        <label className="field">
          <span>Latitude</span>
          <input
            type="number"
            step="0.00001"
            min={-90}
            max={90}
            value={config.latitude ?? ""}
            disabled={config.enrolled}
            onChange={(e) =>
              set("latitude", e.target.value === "" ? null : Number(e.target.value))
            }
            placeholder="-43.48972"
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
            disabled={config.enrolled}
            onChange={(e) =>
              set("longitude", e.target.value === "" ? null : Number(e.target.value))
            }
            placeholder="172.53194"
          />
        </label>
        <label className="field">
          <span>Elevation</span>
          <input
            type="number"
            step="1"
            min={-500}
            max={100000}
            value={config.elevation_m ?? ""}
            disabled={config.enrolled}
            onChange={(e) =>
              set("elevation_m", e.target.value === "" ? null : Number(e.target.value))
            }
            placeholder="metres above sea level"
          />
          <span className="settings-note">
            {config.enrolled
              ? "Settled at enrolment. Re-enrol the station to move it."
              : config.elevation_m === null
                ? "Optional. Stored with the position but not currently used."
                : "Carried to the box in its enrolment response."}
          </span>
        </label>
        {/* No "this station's data is synthetic" checkbox. The station reports
            per device whether its data is synthetic, and the ingest writes the
            row from the health frame whenever it changes — so anything typed
            here was overwritten by the box within half a minute. It was a
            control that silently did nothing. */}
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
  );
}


/**
 * Removing a record that never became a station.
 *
 * Records are created in the console before anyone is standing at the
 * hardware, so typos, abandoned plans and duplicates accumulate. Until a
 * station has enrolled there is nothing behind the row — no telemetry, no
 * history, nothing anybody will look up — and deleting one is tidying.
 *
 * Hidden entirely once it has enrolled, rather than shown and refused. A
 * disabled destructive control invites someone to go looking for how to enable
 * it, and the honest answer is that this is the wrong tool: a station that has
 * enrolled has history and may have a box on a hill still holding a working
 * credential, which needs revoking first. The server refuses it too — this is
 * the affordance, not the enforcement.
 */
function DeleteStation({
  credentialSeq,
  station,
  onDeleted,
}: {
  station: StationSummary;
  onDeleted: () => void | Promise<void>;
  /** Bumped by the enrolment section whenever it changes a credential. Only a
   *  trigger — the value is never read. Whether this section offers itself is
   *  an answer that goes stale the moment somebody revokes, and it was only
   *  ever computed on mount. */
  credentialSeq: number;
}) {
  const [enrolled, setEnrolled] = useState<boolean | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setEnrolled(null);
    setConfirming(false);
    setError(null);
    api
      .enrolmentStatus(station.id)
      .then((s) => !cancelled && setEnrolled(s.enrolled))
      // Unknown is treated as enrolled: the failure mode of guessing wrong in
      // the other direction is offering to delete a live station.
      .catch(() => !cancelled && setEnrolled(true));
    return () => {
      cancelled = true;
    };
  }, [station.id, credentialSeq]);

  if (enrolled !== false) return null;

  return (
    <section className="settings-section">
      <h3>Delete</h3>
      {!confirming ? (
        <div className="settings-actions">
          <button
            type="button"
            className="btn danger"
            onClick={() => setConfirming(true)}
          >
            Delete station
          </button>
          <span className="settings-note">Never enrolled.</span>
        </div>
      ) : (
        <div className="settings-actions">
          {/* The name is in the button, so the click that deletes says what it
              deletes. A bare "Confirm" is the same keystroke wherever the list
              happened to have scrolled to. */}
          <button
            type="button"
            className="btn danger"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                await api.deleteStation(station.id);
                await onDeleted();
              } catch (err) {
                setError(
                  err instanceof ApiError ? err.message : "Could not delete.",
                );
                setBusy(false);
                setConfirming(false);
              }
            }}
          >
            {busy ? "Deleting…" : `Delete ${station.name}`}
          </button>
          <button
            type="button"
            className="btn ghost"
            onClick={() => setConfirming(false)}
            disabled={busy}
          >
            Cancel
          </button>
        </div>
      )}
      {error && <span className="settings-error">{error}</span>}
    </section>
  );
}
