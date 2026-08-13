import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { ApiError, api } from "../api";
import type {
  FleetAdsb,
  FleetStation,
  FleetView,
  Me,
  PlatformMapConfig,
} from "../types";
import { FleetMap } from "./FleetMap";
import { Logo } from "./Logo";
import { SettingsAccount } from "./SettingsAccount";
import { SettingsPlatform } from "./SettingsPlatform";
import { UserMenu } from "./UserMenu";
import { StationHostShell } from "./StationHostShell";

/**
 * The platform admin's home. A full-screen view of the whole estate, shown
 * instead of the station console when the signed-in session's active org is the
 * platform org.
 *
 * It does not share the console's layout on purpose: the console is one
 * operator watching one site, fit-scaled to a wall display; this is one person
 * watching every site, and it is a reading surface with its own sizing (see
 * .pdash in styles.css) rather than the fluid telemetry wall. Descending into a
 * customer org through the switcher reloads the page, the session comes back
 * with is_platform_admin off, and App falls through to the normal console — so
 * this component never has to hand over in place.
 */

type Tab = "overview" | "stations" | "orgs" | "account";

const TABS: { key: Tab; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "stations", label: "Stations" },
  { key: "orgs", label: "Organisations" },
  { key: "account", label: "Account" },
];

/** How worrying a station is, lowest-first, so problems sort to the top. */
const STATUS_RANK: Record<string, number> = { offline: 1, never: 2, online: 3 };
function stationRank(s: FleetStation): number {
  if (s.dark) return 0;
  return STATUS_RANK[s.status] ?? 4;
}

function ago(iso: string | null): string {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function statusLabel(s: FleetStation): string {
  if (s.status === "never") return "never seen";
  if (s.dark) return "dark";
  return s.status;
}
function statusTone(s: FleetStation): string {
  if (s.status === "online") return "ok";
  if (s.status === "never") return "neutral";
  return s.dark ? "bad" : "warn";
}

export function PlatformDashboard({
  me,
  onSignedOut,
  refreshMe,
}: {
  me: Me;
  onSignedOut: () => void;
  refreshMe: () => void;
}) {
  const [tab, setTab] = useState<Tab>("overview");
  const [fleet, setFleet] = useState<FleetView | null>(null);
  const [adsb, setAdsb] = useState<FleetAdsb | null>(null);
  const [mapConfig, setMapConfig] = useState<PlatformMapConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void api.platformMap().then(setMapConfig).catch(() => setMapConfig(null));
  }, []);

  // The estate refreshes on a slow poll — station state moves in minutes, not
  // seconds, and this is a wall to glance at, not a live scope.
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .platformFleet()
        .then((f) => {
          if (alive) {
            setFleet(f);
            setError(null);
          }
        })
        .catch((e) => {
          if (alive)
            setError(e instanceof ApiError ? e.message : "Could not load the fleet.");
        });
    void load();
    const id = window.setInterval(load, 15000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  // ADS-B is only drawn on the overview map, so only poll it there. It ages out
  // server-side, so a station that goes quiet drops off on its own.
  useEffect(() => {
    if (tab !== "overview") return;
    let alive = true;
    const load = () =>
      api.platformAdsb().then((a) => alive && setAdsb(a)).catch(() => {});
    void load();
    const id = window.setInterval(load, 6000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [tab]);

  return (
    <div className="pdash">
      <header className="pdash-head">
        {/* The same mark and wordmark as the console, not a text lockup of its
            own. No "Platform" chip beside it: the tabs below say what this view
            is, and the amber header the console shows for an elevated session is
            the thing that actually warns you where you are. */}
        <Logo />
        <nav className="pdash-tabs" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              role="tab"
              aria-selected={tab === t.key}
              className={`pdash-tab${tab === t.key ? " active" : ""}`}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <div className="pdash-head-right">
          {/* Same menu and same order as the console, so the two headers do not
              teach different habits. "Settings" here is this dashboard's own
              account tab rather than a dialog. */}
          <UserMenu
            me={me}
            displayName={me.display_name}
            onSettings={() => setTab("account")}
            onSignOut={onSignedOut}
          />
        </div>
      </header>

      <main className="pdash-main">
        {tab === "overview" && (
          <Overview fleet={fleet} adsb={adsb} mapConfig={mapConfig} error={error} />
        )}
        {tab === "stations" && <StationsTab fleet={fleet} error={error} />}
        {tab === "orgs" && (
          <div className="settings-pane pdash-settings">
            <SettingsPlatform />
          </div>
        )}
        {tab === "account" && (
          <div className="settings-pane pdash-settings">
            <div className="settings-sections">
              <SettingsAccount me={me} onProfileChanged={() => refreshMe()} />
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

function Kpi({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: number | string;
  sub?: string;
  tone?: string;
}) {
  return (
    <div className={`kpi${tone ? " " + tone : ""}`}>
      <span className="kpi-value">{value}</span>
      <span className="kpi-label">{label}</span>
      {sub && <span className="kpi-sub">{sub}</span>}
    </div>
  );
}

function Overview({
  fleet,
  adsb,
  mapConfig,
  error,
}: {
  fleet: FleetView | null;
  adsb: FleetAdsb | null;
  mapConfig: PlatformMapConfig | null;
  error: string | null;
}) {
  const stations = useMemo(() => fleet?.stations ?? [], [fleet]);
  const attention = useMemo(
    () =>
      [...stations]
        .filter((s) => s.status !== "online")
        .sort((a, b) => stationRank(a) - stationRank(b))
        .slice(0, 8),
    [stations],
  );

  if (!fleet) {
    return (
      <div className="pdash-pane">
        <p className="settings-note">{error ?? "Loading the fleet…"}</p>
      </div>
    );
  }
  const s = fleet.stats;
  const faults = s.faults_critical_24h + s.faults_warning_24h;

  return (
    <div className="pdash-pane pdash-overview">
      <div className="kpi-row">
        <Kpi
          label="Stations"
          value={s.stations_total}
          sub={`${s.stations_total - s.stations_no_location} located`}
        />
        <Kpi label="Online" value={s.stations_online} tone="ok" />
        <Kpi
          label="Offline"
          value={s.stations_offline}
          tone={s.stations_offline ? "warn" : undefined}
          sub={s.stations_dark ? `${s.stations_dark} dark` : undefined}
        />
        <Kpi
          label="Faults 24h"
          value={faults}
          tone={s.faults_critical_24h ? "bad" : faults ? "warn" : undefined}
          sub={s.faults_critical_24h ? `${s.faults_critical_24h} critical` : undefined}
        />
        <Kpi
          label="Aircraft"
          value={adsb ? adsb.aircraft.length : "—"}
          tone="gold"
          sub={adsb ? `${adsb.contributing_stations} stations` : undefined}
        />
        <Kpi
          label="Organisations"
          value={s.organizations_active}
          sub={
            s.organizations_total !== s.organizations_active
              ? `${s.organizations_total - s.organizations_active} removed`
              : undefined
          }
        />
      </div>

      <div className="pdash-overview-grid">
        <div className="pdash-map-wrap">
          {mapConfig ? (
            <FleetMap
              config={mapConfig}
              stations={stations}
              aircraft={adsb?.aircraft ?? []}
            />
          ) : (
            <div className="pdash-map-empty">Map unavailable</div>
          )}
        </div>

        <aside className="pdash-attention">
          <h3>Needs attention</h3>
          {attention.length === 0 ? (
            <p className="settings-note">Every station is online.</p>
          ) : (
            <ul className="attention-list">
              {attention.map((st) => (
                <li key={st.id}>
                  <span className={`status-dot ${statusTone(st)}`} />
                  <span className="attention-name">{st.name}</span>
                  <span className="attention-meta">
                    {st.organization_name} · {statusLabel(st)} · {ago(st.last_seen_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}

          <h3>Recent events</h3>
          {fleet.recent_events.length === 0 ? (
            <p className="settings-note">Nothing in the last while.</p>
          ) : (
            <ul className="attention-list">
              {fleet.recent_events.slice(0, 8).map((ev) => (
                <li key={ev.id}>
                  <span
                    className={`status-dot ${ev.severity === "critical" ? "bad" : "warn"}`}
                  />
                  <span className="attention-name">{ev.station_name}</span>
                  <span className="attention-meta">
                    {ev.message ?? ev.type} · {ago(ev.received_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </aside>
      </div>
    </div>
  );
}

function StationsTab({
  fleet,
  error,
}: {
  fleet: FleetView | null;
  error: string | null;
}) {
  // Which station's on-box console / host shell is open, if any. Local to this
  // tab: each is a reach into one box, opened from its row and closed back to
  // the list.
  const [console_, setConsole] = useState<FleetStation | null>(null);
  const [shell, setShell] = useState<FleetStation | null>(null);
  const rows = useMemo(
    () =>
      [...(fleet?.stations ?? [])].sort(
        (a, b) =>
          stationRank(a) - stationRank(b) ||
          a.organization_name.localeCompare(b.organization_name) ||
          a.name.localeCompare(b.name),
      ),
    [fleet],
  );

  if (!fleet) {
    return (
      <div className="pdash-pane">
        <p className="settings-note">{error ?? "Loading…"}</p>
      </div>
    );
  }

  return (
    <div className="pdash-pane pdash-stations">
      <div className="grant-grid-wrap">
        <table className="grant-grid pdash-table">
          <thead>
            <tr>
              <th scope="col">Station</th>
              <th scope="col">Organisation</th>
              <th scope="col">Status</th>
              <th scope="col">Last seen</th>
              <th scope="col">Location</th>
              <th scope="col">Model</th>
              <th scope="col">Access</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((st) => (
              <tr key={st.id}>
                <th scope="row">
                  {st.name}
                  {st.is_simulated && <span className="sim-tag"> ·demo</span>}
                </th>
                <td>{st.organization_name}</td>
                <td>
                  <span className={`status-dot ${statusTone(st)}`} />
                  {statusLabel(st)}
                </td>
                <td>{ago(st.last_seen_at)}</td>
                <td>{st.locality ?? (st.latitude === null ? "no position" : "—")}</td>
                <td>{st.model ?? "—"}</td>
                <td className="pdash-access">
                  {/* Enabled only for a station that could answer — the box has
                      to be online to open either socket back down the link, and
                      a button that reliably fails reads as broken. */}
                  <button
                    type="button"
                    className="btn ghost tiny"
                    disabled={st.status !== "online"}
                    onClick={() => setConsole(st)}
                    title={
                      st.status === "online"
                        ? "Open this station's on-box setup console"
                        : "The station is offline; its console cannot be opened"
                    }
                  >
                    Console
                  </button>
                  {/* A root shell on the host — the biggest escalation here, so
                      it is its own control and needs the box opted in on both the
                      profile and the agent flag, or it simply will not answer. */}
                  <button
                    type="button"
                    className="btn ghost tiny danger"
                    disabled={st.status !== "online"}
                    onClick={() => setShell(st)}
                    title={
                      st.status === "online"
                        ? "Open a shell on this station's host (audited)"
                        : "The station is offline; a host shell cannot be opened"
                    }
                  >
                    Shell
                  </button>
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={7}>No stations yet.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {console_ && (
        <StationConsole station={console_} onClose={() => setConsole(null)} />
      )}
      {shell && (
        <StationHostShell station={shell} onClose={() => setShell(null)} />
      )}
    </div>
  );
}

/**
 * The station's own setup console, reached over the platform and shown in an
 * iframe. The src is the reverse proxy in `api/console.py`, served same-origin
 * so the platform-admin session cookie authorises it and the box's console —
 * enrolment, device inventory, radio, location, factory reset, logs — renders
 * inside the frame. Opening it makes the station dial its console socket back to
 * the platform (audited there); closing the panel stops the polling, and the
 * station's own idle window then closes the socket.
 */
function StationConsole({
  station,
  onClose,
}: {
  station: FleetStation;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return createPortal(
    <div className="station-console-scrim" role="presentation" onClick={onClose}>
      <div
        className="station-console"
        role="dialog"
        aria-label={`${station.name} setup console`}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="station-console-head">
          <div>
            <strong>{station.name}</strong>
            <span className="station-console-org">{station.organization_name}</span>
          </div>
          <button type="button" className="btn ghost tiny" onClick={onClose}>
            Close
          </button>
        </header>
        <iframe
          className="station-console-frame"
          title={`${station.name} setup console`}
          src={`/api/platform/stations/${station.id}/console/`}
        />
      </div>
    </div>,
    document.body,
  );
}
