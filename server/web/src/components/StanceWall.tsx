import { useEffect, useMemo, useRef } from "react";

import { type Form, allocate, withPosters } from "../stance";
import { useDisplayPrefs } from "../displayPrefs";
import { weatherDisplay } from "../format";
import type { FleetStation } from "../types";
import { type StationAlertSummary } from "./TileWall";

/**
 * The STANCE wall: four forms, handed out worst-first.
 *
 * The decision of WHICH form each station gets lives in `stance.ts` as a pure
 * function, because jsdom computes no layout — anything that has to be tested
 * cannot be allowed to depend on a rendered size. This file is only the
 * drawing.
 *
 * TWO RULES RUN THROUGH EVERY FORM HERE, and both are corrections of the tile
 * this replaces:
 *
 * **Nothing is ever laid over a picture.** The old tile put the name, the
 * organisation and the weather on top of the still and fought the resulting
 * legibility problem with a scrim — which was wrong twice, first painting under
 * the image and dimming nothing, then multiplying with the image's own opacity
 * and leaving two per cent contrast. Here the picture is always a bounded
 * region with the words beside or beneath it. There is no scrim in this file.
 *
 * **The still is never enlarged past its source.** The station sends 480x270.
 * A strip renders it at about that; a panel at most a third larger, which is
 * the point where the JPEG starts to show at three metres. Extra width goes to
 * type — a 34px name and the state in plain words — because a bigger blur is
 * not more information.
 *
 * Everything else is inherited on purpose: the same rank order, the same tones,
 * the same "fine has no colour" ration, the same struck-through glyph for a
 * device that was never fitted.
 */

const VITALS: { kind: "link" | "battery" | "radio" | "camera"; label: string }[] = [
  { kind: "link", label: "Uplink" },
  { kind: "battery", label: "Battery" },
  { kind: "radio", label: "Radio" },
  { kind: "camera", label: "Camera" },
];

function tone(s: FleetStation): "neutral" | "warn" | "bad" {
  if (s.status === "never") return "neutral";
  if (s.dark) return "bad";
  if (s.status === "offline") return "warn";
  if (s.health === "failing") return "bad";
  if (s.health === "degraded") return "warn";
  return "neutral";
}

/** Short, and in words. "offline for 2h" is a sentence an operator can act on;
 *  "OFFLINE" plus a timestamp somewhere else is two things to assemble. */
function stateWords(s: FleetStation): string {
  const ago = (iso: string | null) => {
    if (!iso) return "";
    const ms = Date.now() - Date.parse(iso);
    if (!Number.isFinite(ms) || ms < 0) return "";
    const m = ms / 60000;
    if (m < 90) return ` for ${Math.round(m)}m`;
    const h = m / 60;
    if (h < 48) return ` for ${Math.round(h)}h`;
    return ` for ${Math.round(h / 24)}d`;
  };
  if (s.status === "never") return "never connected";
  if (s.dark) return `dark${ago(s.last_seen_at)}`;
  if (s.status === "offline") return `offline${ago(s.last_seen_at)}`;
  if (s.health === "failing") return "online, failing";
  if (s.health === "degraded") return "online, degraded";
  return "online";
}

function slotState(s: FleetStation, kind: string): "present" | "absent" | "fault" {
  const v = s.slots?.[kind];
  if (v === undefined || v === "not_fitted") return "absent";
  if (v === "present") return "present";
  return "fault";
}

function Glyph({ kind, struck }: { kind: string; struck: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {kind === "link" && (
        <>
          <circle cx="12" cy="18.6" r="1.5" fill="currentColor" stroke="none" />
          <path d="M8.1 15a5.6 5.6 0 0 1 7.8 0" />
          <path d="M4.6 11.4a10.6 10.6 0 0 1 14.8 0" />
        </>
      )}
      {kind === "battery" && (
        <>
          <rect x="2.4" y="7.4" width="16.2" height="9.2" rx="2" />
          <path d="M21.4 10.6v2.8" />
        </>
      )}
      {kind === "radio" && (
        <>
          <path d="M12 20.8V10.4" />
          <path d="M8.4 20.8h7.2" />
          <circle cx="12" cy="7.6" r="1.9" />
          <path d="M15.9 4.1a5.6 5.6 0 0 1 0 7M8.1 4.1a5.6 5.6 0 0 0 0 7" />
        </>
      )}
      {kind === "camera" && (
        <>
          <path d="M3 8.5A2 2 0 0 1 5 6.5h2.4l1.3-2h6.6l1.3 2H19a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" />
          <circle cx="12" cy="12.5" r="3.2" />
        </>
      )}
      {struck && <path d="M3.4 20.6 20.6 3.4" />}
    </svg>
  );
}

function Vitals({ s }: { s: FleetStation }) {
  return (
    <span className="stance-vitals">
      {VITALS.map((v) => {
        const state = slotState(s, v.kind);
        return (
          <span
            key={v.kind}
            className={`stance-glyph ${state === "fault" ? "warn" : ""} ${
              state === "absent" ? "absent" : ""
            }`}
            title={`${v.label}: ${state === "absent" ? "not fitted" : state}`}
          >
            <Glyph kind={v.kind} struck={state === "absent"} />
          </span>
        );
      })}
    </span>
  );
}

/** The picture, as a bounded region. Absent when there is none — a station
 *  whose still has aged out of the cache shows the empty frame, not a stale
 *  photograph, and the two must not look alike. */
function Poster({ s, className }: { s: FleetStation; className: string }) {
  if (!s.poster_at) {
    return (
      <div className={`${className} empty`} aria-hidden="true">
        <span>no picture</span>
      </div>
    );
  }
  return (
    <img
      className={className}
      src={`/api/odin/stations/${s.id}/poster?v=${encodeURIComponent(s.poster_at)}`}
      alt=""
      aria-hidden="true"
      draggable={false}
    />
  );
}

function Weather({ s }: { s: FleetStation }) {
  const prefs = useDisplayPrefs();
  const wind = weatherDisplay("wind", prefs);
  const temp = weatherDisplay("temp", prefs);
  const gusting =
    s.gust_kt != null && s.wind_kt != null && s.gust_kt > s.wind_kt;
  const parts: string[] = [];
  if (s.wind_kt != null) {
    parts.push(
      `${wind.convert(s.wind_kt).toFixed(wind.digits)}${
        gusting ? `–${wind.convert(s.gust_kt!).toFixed(wind.digits)}` : ""
      } ${wind.suffix}`,
    );
  }
  if (s.temperature_c != null) {
    parts.push(`${temp.convert(s.temperature_c).toFixed(temp.digits)}${temp.suffix}`);
  }
  if (s.visibility_km != null) parts.push(`${s.visibility_km.toFixed(0)} km vis`);
  if (!parts.length) return null;
  return <span className="stance-wx">{parts.join(" · ")}</span>;
}

/** Charge as a bar AND a number. The bar is read at distance, the number is
 *  read close up, and neither is drawn at all when the station has not reported
 *  one — a quiet station has no state of charge, it has not got a flat one. */
function Charge({ s, wide }: { s: FleetStation; wide?: boolean }) {
  if (s.soc_pct == null) return null;
  const pct = Math.min(100, Math.max(0, s.soc_pct));
  return (
    <span className="stance-soc" title={`${Math.round(pct)}% charge`}>
      <span className={`stance-soc-track${s.on_battery ? " warn" : ""}`}>
        <span className="stance-soc-fill" style={{ width: `${pct}%` }} />
      </span>
      {wide && <em>{Math.round(pct)}%</em>}
    </span>
  );
}

function Markers({ s }: { s: FleetStation }) {
  return (
    <>
      {s.maintenance_until && (
        <span
          className="stance-mark hush"
          title={`Silenced: ${s.maintenance_reason ?? "no reason given"}`}
        >
          HUSH
        </span>
      )}
      {s.is_simulated && (
        <span className="stance-mark demo" title="This station reports synthetic data">
          DEMO
        </span>
      )}
    </>
  );
}

function Station({
  s,
  form,
  selected,
  onSelect,
  alert,
}: {
  s: FleetStation;
  form: Form;
  selected: boolean;
  onSelect: (id: string) => void;
  /** What the rail knows about this station. Shown as a count rather than
   *  folded into the condition badge beside it: an alert is something a person
   *  raised or has to acknowledge, a condition is the station's own account of
   *  itself, and collapsing the two loses which one somebody is on the hook
   *  for. */
  alert?: StationAlertSummary;
}) {
  const t = tone(s);
  const where = [s.locality, s.region].filter(Boolean).join(", ");
  const spoken = [s.name, s.organization_name, stateWords(s)].join(", ");

  if (form === "chip") {
    return (
      <button
        type="button"
        className={`stance-chip ${t}`}
        onClick={() => onSelect(s.id)}
        aria-current={selected}
        aria-label={spoken}
      >
        <span className={`stance-edge ${t}`} aria-hidden="true" />
        <span className="stance-chip-name">{s.name}</span>
        <span className="stance-chip-state">{stateWords(s)}</span>
        <Charge s={s} />
      </button>
    );
  }

  return (
    <button
      type="button"
      className={`stance-${form} ${t}`}
      onClick={() => onSelect(s.id)}
      aria-current={selected}
      aria-label={spoken}
    >
      {/* The state band runs down the LEFT EDGE, full height, rather than
          across the top. At strip scale a 6px line along 1280px is a hairline;
          the same 6px down 290px of height puts three stations' states into one
          vertical column read in a single glance from across the room. Same
          variable, same three tones, far more presence. */}
      <span className={`stance-edge ${t}`} aria-hidden="true" />
      <Poster s={s} className={`stance-shot ${form}`} />
      <span className="stance-ledger">
        <span className="stance-head">
          <span className="stance-name">{s.name}</span>
          <Markers s={s} />
        </span>
        <span className="stance-org">
          {s.organization_name}
          {where && ` · ${where}`}
        </span>
        <span className={`stance-state ${t}`}>{stateWords(s)}</span>
        {form !== "card" && (
          <>
            <Charge s={s} wide />
            <Weather s={s} />
            <span className="stance-row">
              <Vitals s={s} />
              {alert && alert.unackedCritical + alert.unackedWarning > 0 && (
                <span
                  className={`stance-fault ${alert.unackedCritical > 0 ? "bad" : "warn"}`}
                  title={`${alert.unackedCritical} unacked critical, ${alert.unackedWarning} unacked warning`}
                >
                  {alert.unackedCritical + alert.unackedWarning}
                  <em>unacked</em>
                </span>
              )}
              {(s.condition_count ?? 0) > 0 && (
                <span
                  className={`stance-fault ${s.health === "failing" ? "bad" : "warn"}`}
                  title={s.worst_condition ?? undefined}
                >
                  {s.condition_count}
                  {s.worst_condition && <em>{s.worst_condition}</em>}
                </span>
              )}
            </span>
          </>
        )}
      </span>
    </button>
  );
}

export function StanceWall({
  stations,
  selectedId,
  onSelect,
  alerts,
  onShowing,
  rank,
}: {
  stations: FleetStation[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  alerts?: Record<string, StationAlertSummary>;
  onShowing?: (stationIds: string[]) => void;
  /** The wall's own comparator, passed in rather than duplicated — two sorts on
   *  one screen is two answers to "which is worst". */
  rank: (a: FleetStation, b: FleetStation) => number;
}) {
  const ranked = useMemo(() => [...stations].sort(rank), [stations, rank]);
  const forms = useMemo(
    () => allocate(ranked, { promote: selectedId }),
    [ranked, selectedId],
  );

  // Same discipline as the grid: compare membership on a sorted key so a
  // re-sort is not mistaken for a change, but send the wall's own order so a
  // server-side cap drops the calmest stations rather than whichever sort last.
  const showing = withPosters(ranked, forms);
  const showingKey = [...showing].sort().join(",");
  const showingRef = useRef(showing);
  showingRef.current = showing;
  useEffect(() => {
    onShowing?.(showingRef.current);
  }, [showingKey, onShowing]);

  const hidden = forms.filter((f) => f === "hidden").length;

  return (
    <div className="stance-wall" role="group" aria-label="Stations, worst first">
      {ranked.map((s, i) =>
        forms[i] === "hidden" ? null : (
          <Station
            key={s.id}
            s={s}
            form={forms[i]}
            selected={s.id === selectedId}
            onSelect={onSelect}
            alert={alerts?.[s.id]}
          />
        ),
      )}
      {hidden > 0 && (
        <div className="stance-rest" aria-label={`${hidden} nominal stations not shown`}>
          {hidden} nominal
        </div>
      )}
    </div>
  );
}
