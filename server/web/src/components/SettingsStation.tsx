import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type {
  LatestRelease,
  StationConfig,
  StationHealth,
  StationSummary,
} from "../types";
import { SettingsEnrolment } from "./SettingsEnrolment";

/**
 * Every station in the organisation: add one, pick it, configure it, enrol it.
 *
 * Lives under the Organisation tab, because a station record is an org-wide
 * thing rather than a property of whoever happens to be watching one. The
 * selector is deliberately independent of the console's station switcher:
 * configuring a site is an administrative job you do for whichever station
 * needs it, not for the one in front of you.
 */
export function SettingsStation({
  initialStationId,
  onSaved,
}: {
  initialStationId: string | null;
  onSaved: () => void;
}) {
  const [stations, setStations] = useState<StationSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(initialStationId);
  const [listError, setListError] = useState<string | null>(null);
  // Bumped whenever the enrolment section changes a credential, so the Delete
  // section re-asks whether it should be showing. The two are siblings reading
  // the same status, and only one of them was refetching it.
  const [credentialSeq, setCredentialSeq] = useState(0);
  // The latest published release drives the per-row update pill; `updating` marks
  // the stations a one-click update was just requested for, so their pill reads
  // "Updating…" until the station reports the new version and the row refreshes.
  const [latest, setLatest] = useState<LatestRelease | null>(null);
  const [updating, setUpdating] = useState<Record<string, boolean>>({});

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

  // The latest release, for the update pills.
  useEffect(() => {
    api.latestRelease().then(setLatest).catch(() => setLatest(null));
  }, []);

  // Keep the list current while the panel is open, so online status, the running
  // version and the update pills track without a manual reload.
  useEffect(() => {
    const timer = window.setInterval(() => void loadStations(), 30000);
    return () => window.clearInterval(timer);
  }, [loadStations]);

  if (listError) return <p className="settings-error">{listError}</p>;
  if (!stations) return <p className="settings-note">Loading…</p>;

  const station = stations.find((s) => s.id === selected) ?? null;

  const updateAvailable = (s: StationSummary): boolean =>
    Boolean(latest?.tag && s.running_version && s.running_version !== latest.tag);

  async function updateToLatest(id: string) {
    setUpdating((u) => ({ ...u, [id]: true }));
    try {
      await api.updateStationToLatest(id);
      // Leave the pill reading "Updating…"; the station reports the new version
      // on its next health frame, and the periodic refresh clears the pill once
      // running_version matches the latest tag.
    } catch {
      // Refused — drop back to an actionable pill rather than a stuck "Updating…".
      setUpdating((u) => {
        const next = { ...u };
        delete next[id];
        return next;
      });
    }
  }

  return (
    <div className="member-layout station-layout">
      {/* The enrolled stations, scrollable, with Add pinned to the bottom of the
          column — the same list-and-detail shape as People. Picking one is
          independent of the console's own station switcher: you configure
          whichever site needs it, not the one you happen to be watching. */}
      <div className="station-list-col">
        <ul className="member-list station-list">
          {stations.length === 0 && (
            <li className="station-list-empty settings-note">No stations yet.</li>
          )}
          {stations.map((s) => (
            <li key={s.id} className="station-row">
              <button
                type="button"
                className={`member-item${s.id === selected ? " active" : ""}`}
                onClick={() => setSelected(s.id)}
              >
                <span className="member-name">
                  {s.name}
                  {s.is_simulated && <em> · DEMO</em>}
                </span>
                <span className={`member-roles station-status${s.online ? " online" : ""}`}>
                  {s.online ? "online" : "offline"}
                </span>
              </button>
              {updateAvailable(s) && (
                <button
                  type="button"
                  className="update-pill"
                  disabled={Boolean(updating[s.id])}
                  onClick={() => void updateToLatest(s.id)}
                  title={`Update ${s.name} to ${latest?.tag}`}
                >
                  {updating[s.id] ? "Updating…" : `Update to ${latest?.tag}`}
                </button>
              )}
            </li>
          ))}
        </ul>
        <div className="station-list-add">
          <AddStation
            onCreated={async (id) => {
              // Refresh the list and select the new record in place, so the
              // operator lands on it ready to set position and issue a code.
              await loadStations(id);
              onSaved();
            }}
          />
        </div>
      </div>

      <div className="member-detail station-detail">
        {station ? (
          <>
            <StationStats key={`stats-${station.id}`} stationId={station.id} />
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
          <p className="settings-note">No stations yet. Add one to get started.</p>
        )}
      </div>
    </div>
  );
}

/**
 * Create a station — name only.
 *
 * Naming is the whole job. Everything else (position, enrolment code) belongs on
 * the station's own configuration below, so on create the picker simply selects
 * the new record in place. The timezone is taken from the browser — right far
 * more often than UTC — and stays editable on the configuration form afterwards.
 */
function AddStation({
  onCreated,
}: {
  onCreated: (stationId: string) => void | Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Escape closes only this modal, not the settings dialog behind it. The
  // capture phase runs before the settings' own window-level Escape handler, and
  // stopping the event there keeps one press from closing both.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopImmediatePropagation();
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const station = await api.createStation({
        name: name.trim(),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        latitude: null,
        longitude: null,
      });
      setOpen(false);
      setName("");
      await onCreated(station.id);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not create the station.",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <button
        type="button"
        className="btn primary"
        onClick={() => {
          setName("");
          setError(null);
          setOpen(true);
        }}
      >
        Add station
      </button>

      {open && (
        <div className="modal-scrim" onClick={() => setOpen(false)}>
          <div
            className="modal-card"
            role="dialog"
            aria-modal="true"
            aria-label="Add station"
            onClick={(e) => e.stopPropagation()}
          >
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
              <p className="settings-note">
                The record belongs to your organisation — the hardware does not
                have to exist yet. It opens below, ready for its position and an
                enrolment code.
              </p>
              <div className="settings-actions">
                <button
                  type="submit"
                  className="btn primary"
                  disabled={saving || !name.trim()}
                >
                  {saving ? "Creating…" : "Create station"}
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => setOpen(false)}
                >
                  Cancel
                </button>
                {error && <span className="settings-error">{error}</span>}
              </div>
            </form>
          </div>
        </div>
      )}
    </>
  );
}

function formatUptime(seconds: number): string {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

/**
 * The selected station's live host stats — uptime, CPU, load, temperature and
 * memory — read from the platform's cache of the station's last health frame,
 * and polled while the panel is open. Best-effort on the station, so any single
 * field may be absent; the panel says so plainly when the station is offline or
 * has not reported within the snapshot's lifetime.
 */
function StationStats({ stationId }: { stationId: string }) {
  const [health, setHealth] = useState<StationHealth | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setHealth(null);
    setError(null);
    const load = () => {
      api
        .stationHealth(stationId)
        .then((h) => {
          if (!cancelled) {
            setHealth(h);
            setError(null);
          }
        })
        .catch((err) => {
          if (!cancelled) {
            setError(err instanceof ApiError ? err.message : "Could not load stats.");
          }
        });
    };
    load();
    // Health lands about every 30s; refresh at half that so the numbers track
    // without hammering, and stop when the panel or the station changes.
    const timer = window.setInterval(load, 15000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [stationId]);

  const sys = health?.system;

  return (
    <section className="settings-section">
      <h3>System</h3>
      {error && <p className="settings-error">{error}</p>}
      {!health ? (
        <p className="settings-note">Loading…</p>
      ) : !health.online ? (
        <p className="settings-note">This station is offline.</p>
      ) : !sys ? (
        <p className="settings-note">No recent telemetry from this station.</p>
      ) : (
        <dl className="settings-facts">
          {sys.uptime_s != null && (
            <>
              <dt>Host uptime</dt>
              <dd>{formatUptime(sys.uptime_s)}</dd>
            </>
          )}
          {sys.cpu_percent != null && (
            <>
              <dt>CPU</dt>
              <dd>{sys.cpu_percent}%</dd>
            </>
          )}
          {sys.load_1m != null && (
            <>
              <dt>Load (1 min)</dt>
              <dd>{sys.load_1m}</dd>
            </>
          )}
          {sys.temperature_c != null && (
            <>
              <dt>Temperature</dt>
              <dd>{sys.temperature_c} °C</dd>
            </>
          )}
          {sys.memory?.used_percent != null && (
            <>
              <dt>Memory</dt>
              <dd>
                {sys.memory.used_percent}%
                {sys.memory.used_mb != null && sys.memory.total_mb != null
                  ? ` (${sys.memory.used_mb} / ${sys.memory.total_mb} MB)`
                  : ""}
              </dd>
            </>
          )}
        </dl>
      )}
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
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      setConfig(await api.stationConfig(stationId));
    } catch (err) {
      setLoadError(
        err instanceof ApiError && err.status === 404
          ? "You do not have configuration access to this station."
          : "Could not load configuration.",
      );
    }
  }, [stationId]);

  useEffect(() => {
    // A fresh station: drop the old config, leave edit mode, clear the last
    // save message, and reload.
    setConfig(null);
    setEditing(false);
    setMessage(null);
    void load();
  }, [load]);

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
      setEditing(false);
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

  if (!editing) {
    return (
      <section className="settings-section">
        <div className="member-detail-head">
          <h3>Configuration</h3>
          <button
            type="button"
            className="btn primary"
            onClick={() => {
              setMessage(null);
              setEditing(true);
            }}
          >
            Edit
          </button>
        </div>
        <dl className="settings-facts">
          <dt>Name</dt>
          <dd>{config.name}</dd>
          <dt>Timezone</dt>
          <dd>{config.timezone}</dd>
          <dt>Position</dt>
          <dd>
            {config.latitude != null && config.longitude != null
              ? `${config.latitude.toFixed(5)}, ${config.longitude.toFixed(5)}`
              : "not set"}
          </dd>
          <dt>Elevation</dt>
          <dd>
            {config.elevation_m != null ? `${config.elevation_m} m` : "not set"}
          </dd>
          <dt>Basemap cache</dt>
          <dd>
            zoom {config.map_min_zoom}–{config.map_max_zoom}, {config.map_radius_km} km
            radius
          </dd>
        </dl>
        {message && <span className="settings-ok">{message}</span>}
      </section>
    );
  }

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
        <button
          type="button"
          className="btn ghost"
          disabled={saving}
          onClick={() => {
            setError(null);
            setEditing(false);
            void load();
          }}
        >
          Cancel
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
