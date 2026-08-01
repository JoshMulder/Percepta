import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type { StationConfig, StationSummary } from "../types";
import { NewStationEnrolment, SettingsEnrolment } from "./SettingsEnrolment";

/**
 * Everything about one station: pick it, configure it, enrol it.
 *
 * The selector is deliberately independent of the console's station switcher.
 * Configuring a site is an administrative job you do for whichever station
 * needs it, not for whichever one you happen to be watching — and a new record
 * created here has no telemetry to look at yet, so following the console's
 * selection would mean it could never be configured at all.
 */
export function SettingsStation({
  initialStationId,
  canCreate,
  onSaved,
}: {
  initialStationId: string | null;
  canCreate: boolean;
  onSaved: () => void;
}) {
  const [stations, setStations] = useState<StationSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(initialStationId);
  const [adding, setAdding] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  // Bumped whenever the enrolment section changes a credential, so the Delete
  // section re-asks whether it should be showing. The two are siblings reading
  // the same status, and only one of them was refetching it.
  const [credentialSeq, setCredentialSeq] = useState(0);

  const loadStations = useCallback(async (prefer?: string) => {
    try {
      const list = await api.stations();
      setStations(list);
      setSelected((current) => prefer ?? current ?? list[0]?.id ?? null);
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
            onChange={(e) => {
              setSelected(e.target.value);
              setAdding(false);
            }}
            disabled={adding}
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
        {canCreate && (
          <button
            type="button"
            className={`btn ${adding ? "ghost" : "primary"}`}
            onClick={() => setAdding((a) => !a)}
          >
            {adding ? "Cancel" : "Add new"}
          </button>
        )}
      </div>

      {adding ? (
        <NewStation
          onCancel={() => setAdding(false)}
          // Refreshed at creation rather than when the page is left, so the new
          // station is in the list and selected from that moment. Leaving it
          // until "Done" meant backing out of the code step lost sight of a
          // station that had already been created.
          onCreated={async (created) => {
            await loadStations(created.id);
            onSaved();
          }}
          onDone={() => setAdding(false)}
        />
      ) : station ? (
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
            ? "No stations yet. Add one to get started."
            : "No stations available to configure."}
        </p>
      )}
    </div>
  );
}

function NewStation({
  onCreated,
  onCancel,
  onDone,
}: {
  /** Called as soon as the record exists, so the list behind this page is
   *  correct even if the code step is abandoned. */
  onCreated: (station: StationSummary) => void | Promise<void>;
  onCancel: () => void;
  onDone: () => void;
}) {
  const [created, setCreated] = useState<StationSummary | null>(null);
  const [name, setName] = useState("");
  const [timezone, setTimezone] = useState(
    // The browser knows where the person creating it is, which is right far more
    // often than "UTC" is. It stays editable because a remote site frequently is
    // not in the same zone as whoever is setting it up.
    Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      // The record exists from here on. We stay on this page rather than
      // returning to the list, because handing the installer a code is the next
      // thing that happens and making them go and find the station to do it is
      // a step that exists for no reason.
      const station = await api.createStation({
        name: name.trim(),
        timezone,
        latitude: null,
        longitude: null,
      });
      setCreated(station);
      await onCreated(station);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the station.");
    } finally {
      setSaving(false);
    }
  }

  if (created) {
    return (
      <div className="settings-sections">
        <section className="settings-section">
          <h3>{created.name} created</h3>
          <p className="settings-note">
            Issue a code below if there is a box ready to enrol. There is no
            hurry — the station's own page keeps offering one until it has
            connected, and a code only lasts 24 hours, so issuing it before
            somebody is standing in front of the hardware wastes it.
          </p>
        </section>

        <NewStationEnrolment stationId={created.id} />

        <div className="settings-actions">
          <button type="button" className="btn primary" onClick={onDone}>
            Done
          </button>
        </div>
      </div>
    );
  }

  return (
    <section className="settings-section">
      <h3>New station</h3>
      <form onSubmit={submit}>
        <label className="field">
          <span>Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Kaikoura Ridge"
            maxLength={255}
            autoFocus
            required
          />
        </label>
        <label className="field">
          <span>Timezone</span>
          <input
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
            placeholder="Pacific/Auckland"
            required
          />
        </label>
        <p className="settings-note">
          The record belongs to your organisation and can be granted to users and
          configured straight away — the hardware does not have to exist yet.
          Position is set once the site is known, or reported by the station.
        </p>
        <div className="settings-actions">
          <button type="submit" className="btn primary" disabled={saving || !name.trim()}>
            {saving ? "Creating…" : "Create station"}
          </button>
          <button type="button" className="btn ghost" onClick={onCancel}>
            Cancel
          </button>
          {error && <span className="settings-error">{error}</span>}
        </div>
      </form>
    </section>
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
        is_simulated: config.is_simulated,
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
        {/* Read-only. Position is owned by the station: it is set on the box
            by whoever is standing at the site, reported up in the health
            frame, and written here. Two places to set one fact is two places
            for it to be wrong, and the one with a person at it wins. */}
        {/* Elevation sits with the position because it is part of it: the
            station's ADS-B barometric correction is computed from it, and a
            correction referenced to the wrong height is out by that height on
            every aircraft. Read-only here for the same reason the coordinates
            are — settled at commissioning, frozen after enrolment. */}
        <div className="field">
          <span>Elevation</span>
          <div className="reported-value">
            {config.elevation_m === null ? (
              <em>Not set. The altitude correction will not run.</em>
            ) : (
              <code>{config.elevation_m} m</code>
            )}
          </div>
        </div>
        <div className="field">
          <span>Position</span>
          <div className="reported-value">
            {config.latitude === null || config.longitude === null ? (
              <em>Not set. Set it on the station's own setup page.</em>
            ) : (
              <code>
                {config.latitude.toFixed(5)}, {config.longitude.toFixed(5)}
              </code>
            )}
          </div>
        </div>
        <label className="field checkbox-field">
          <input
            type="checkbox"
            checked={config.is_simulated}
            onChange={(e) => set("is_simulated", e.target.checked)}
          />
          <span>This station's data is synthetic</span>
        </label>
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
