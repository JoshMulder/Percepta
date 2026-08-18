import { useEffect, useMemo, useRef, useState } from "react";

import {
  type StormGroup,
  groupStorm,
  onsetOf,
  since,
  split,
  withPosters,
} from "../quiet";
import { useDisplayPrefs } from "../displayPrefs";
import { weatherDisplay } from "../format";
import type { FleetStation } from "../types";
import { type StationAlertSummary } from "./TileWall";

/**
 * NOTHING TO REPORT: the wall is what is wrong, at a size you can act on.
 *
 * Three learned shapes, chosen purely by how many stations are in trouble —
 * QUIET, CASES, STORM. The decision lives in `quiet.ts` as pure functions, so
 * the parts that can be silently wrong (a healthy fleet putting cameras on
 * duty, a mass outage rendered as N separate problems) are testable without a
 * layout engine.
 *
 * WHAT THIS BREAKS ON PURPOSE, because it is worth saying out loud rather than
 * discovering:
 *
 * **The nominal reference frame goes.** The grid's docstring argues the nominal
 * tiles are what a coloured one reads as wrong against. This deletes them. A
 * case is not read as a deviation from its neighbours here; it is read as
 * "there is something on the wall at all", which is a stronger contrast than
 * any comparison within a grid can be.
 *
 * **There is one continuously moving thing**, the heartbeat in the muster band,
 * and the doctrine forbids it. The doctrine's reason is that continuous motion
 * trains people to ignore motion — but that only bites while the motion
 * competes with an alarm, and this beats ONLY while there are zero cases and
 * stops the instant one appears. The failure it defends against is the one
 * ODIN.md calls the biggest non-technical risk in the design: a frozen wall
 * that looks perfect. It is driven by frame arrival, never by a timer of its
 * own, so it is evidence rather than decoration — if the feed stops, it stops.
 *
 * **The wall changes shape.** Three discrete states, not a continuum, so an
 * operator learns three; and the shape itself becomes the loudest signal in the
 * room — "the wall is empty" reads from twenty metres, before a word.
 */

function tone(s: FleetStation): "neutral" | "warn" | "bad" {
  if (s.status === "never") return "neutral";
  if (s.dark) return "bad";
  if (s.status === "offline") return "warn";
  if (s.health === "failing") return "bad";
  if (s.health === "degraded") return "warn";
  return "neutral";
}

function stateWords(s: FleetStation): string {
  const onset = onsetOf(s);
  const age = onset == null ? "" : ` ${since(Date.now() - onset)}`;
  if (s.status === "never") return "never connected";
  if (s.dark) return `dark${age}`;
  if (s.status === "offline") return `offline${age}`;
  if (s.health === "failing") return "online, failing";
  if (s.health === "degraded") return "online, degraded";
  return "online";
}

/** The device sentences, WRITTEN OUT rather than hidden on hover. The tooltip
 *  strings on the grid's tile already say the right thing, and a wall display
 *  frequently has no pointer at all. */
const SLOT_WORDS: Record<string, string> = {
  not_fitted: "not fitted",
  present: "present",
  configured_absent: "configured, not found",
  stalled: "stopped answering",
  unsupported: "no driver in this build",
};

const DEVICES: { kind: string; label: string }[] = [
  { kind: "link", label: "Uplink" },
  { kind: "battery", label: "Battery" },
  { kind: "radio", label: "Radio" },
  { kind: "camera", label: "Camera" },
];

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

/** The worst thing on the wall, given the whole panel. */
function Lead({
  s,
  alert,
  onSelect,
}: {
  s: FleetStation;
  alert?: StationAlertSummary;
  onSelect: (id: string) => void;
}) {
  const prefs = useDisplayPrefs();
  const wind = weatherDisplay("wind", prefs);
  const temp = weatherDisplay("temp", prefs);
  const t = tone(s);
  const where = [s.locality, s.region].filter(Boolean).join(", ");
  const wx: string[] = [];
  if (s.wind_kt != null) {
    const gust =
      s.gust_kt != null && s.gust_kt > s.wind_kt
        ? `–${wind.convert(s.gust_kt).toFixed(wind.digits)}`
        : "";
    wx.push(`${wind.convert(s.wind_kt).toFixed(wind.digits)}${gust} ${wind.suffix}`);
  }
  if (s.temperature_c != null)
    wx.push(`${temp.convert(s.temperature_c).toFixed(temp.digits)}${temp.suffix}`);
  if (s.visibility_km != null) wx.push(`${s.visibility_km.toFixed(0)} km vis`);

  const demo = s.simulated_slots ?? [];

  return (
    <button
      type="button"
      className={`quiet-lead ${t}`}
      onClick={() => onSelect(s.id)}
      aria-label={`${s.name}, ${s.organization_name}, ${stateWords(s)}`}
    >
      <Poster s={s} className="quiet-lead-shot" />
      <span className="quiet-lead-body">
        <span className="quiet-lead-name">{s.name}</span>
        <span className="quiet-lead-org">
          {s.organization_name}
          {where && ` · ${where}`}
        </span>
        <span className={`quiet-lead-state ${t}`}>{stateWords(s)}</span>

        <span className="quiet-devices">
          {DEVICES.map((d) => {
            const raw = s.slots?.[d.kind];
            const state = raw ?? "not_fitted";
            const bad = state !== "present" && state !== "not_fitted";
            return (
              <span key={d.kind} className={`quiet-device${bad ? " warn" : ""}`}>
                <em>{d.label}</em>
                {SLOT_WORDS[state] ?? state}
              </span>
            );
          })}
        </span>

        <span className="quiet-lead-foot">
          {s.soc_pct != null && (
            <span className="quiet-soc">
              <span className={`quiet-soc-track${s.on_battery ? " warn" : ""}`}>
                <span
                  className="quiet-soc-fill"
                  style={{ width: `${Math.min(100, Math.max(0, s.soc_pct))}%` }}
                />
              </span>
              <em>{Math.round(s.soc_pct)}%</em>
            </span>
          )}
          {wx.length > 0 && <span className="quiet-wx">{wx.join(" · ")}</span>}
          {alert && alert.unackedCritical + alert.unackedWarning > 0 && (
            <span className={`quiet-alert ${alert.unackedCritical ? "bad" : "warn"}`}>
              {alert.unackedCritical + alert.unackedWarning} unacked
            </span>
          )}
          {(s.condition_count ?? 0) > 0 && (
            <span className="quiet-cond">
              {s.condition_count} condition{s.condition_count === 1 ? "" : "s"}
              {s.worst_condition && <em>{s.worst_condition}</em>}
            </span>
          )}
          {s.running_version && <span className="quiet-ver">{s.running_version}</span>}
          {/* Names which slots, rather than the 10x2px sliver it replaces. A
              wall must never show synthetic numbers as though they were real,
              and "DEMO" somewhere on a tile does not say WHICH readings. */}
          {demo.length > 0 && (
            <span className="quiet-demo">synthetic · {demo.join(", ")}</span>
          )}
        </span>
      </span>
    </button>
  );
}

/** A case that is not the worst. Picture hard left, words beside it, never on it. */
function FileRow({
  s,
  alert,
  onSelect,
}: {
  s: FleetStation;
  alert?: StationAlertSummary;
  onSelect: (id: string) => void;
}) {
  const t = tone(s);
  return (
    <button
      type="button"
      className={`quiet-file ${t}`}
      onClick={() => onSelect(s.id)}
      aria-label={`${s.name}, ${s.organization_name}, ${stateWords(s)}`}
    >
      <span className={`quiet-file-edge ${t}`} aria-hidden="true" />
      <Poster s={s} className="quiet-file-shot" />
      <span className="quiet-file-body">
        <span className="quiet-file-name">{s.name}</span>
        <span className="quiet-file-org">{s.organization_name}</span>
      </span>
      <span className={`quiet-file-state ${t}`}>{stateWords(s)}</span>
      {alert && alert.unackedCritical + alert.unackedWarning > 0 && (
        <span className={`quiet-alert ${alert.unackedCritical ? "bad" : "warn"}`}>
          {alert.unackedCritical + alert.unackedWarning}
        </span>
      )}
    </button>
  );
}

/** A storm, grouped by what is actually happening. */
function Group({
  g,
  onSelect,
}: {
  g: StormGroup;
  onSelect: (id: string) => void;
}) {
  const when = g.since == null ? "onset unknown" : `all since ${since(Date.now() - g.since)} ago`;
  return (
    <div className={`quiet-group ${g.cause === "dark" ? "bad" : "warn"}`}>
      <span className={`quiet-file-edge ${g.cause === "dark" ? "bad" : "warn"}`} aria-hidden="true" />
      <span className="quiet-group-count">{g.stations.length}</span>
      <span className="quiet-group-body">
        <span className="quiet-group-cause">
          {g.cause} · {g.organization}
        </span>
        <span className="quiet-group-when">{when}</span>
      </span>
      <span className="quiet-group-names">
        {g.stations.slice(0, 6).map((s) => (
          <button
            key={s.id}
            type="button"
            className="quiet-group-chip"
            onClick={() => onSelect(s.id)}
          >
            {s.name}
          </button>
        ))}
        {g.stations.length > 6 && <em>+{g.stations.length - 6}</em>}
      </span>
    </div>
  );
}

export function QuietWall({
  stations,
  selectedId,
  onSelect,
  alerts,
  onShowing,
  rank,
  lastFrameAt,
}: {
  stations: FleetStation[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  alerts?: Record<string, StationAlertSummary>;
  onShowing?: (stationIds: string[]) => void;
  rank: (a: FleetStation, b: FleetStation) => number;
  /** When the last digest frame arrived. The heartbeat is driven by this and
   *  never by a timer of its own — a beat from a `setInterval` would go on
   *  beating happily while the feed was dead, which is the exact failure the
   *  beat exists to disprove. */
  lastFrameAt: number | null;
}) {
  const ranked = useMemo(() => [...stations].sort(rank), [stations, rank]);
  const s = useMemo(() => split(ranked, { promote: selectedId }), [ranked, selectedId]);
  const groups = useMemo(
    () => (s.geometry === "storm" ? groupStorm(s.files) : []),
    [s],
  );

  const showing = withPosters(s);
  const showingKey = [...showing].sort().join(",");
  const showingRef = useRef(showing);
  showingRef.current = showing;
  useEffect(() => {
    onShowing?.(showingRef.current);
  }, [showingKey, onShowing]);

  /**
   * When the wall last went quiet, and what it was that cleared.
   *
   * BOTH ARE THIS PAGE'S OWN MEMORY, not the platform's — there is no history
   * endpoint for "when did the fleet last have a case", and inventing one would
   * be a bigger change than a prototype earns. So it says "quiet since you
   * opened this" until it has actually watched a transition, which is the
   * honest version of not knowing. A counter that claimed four hours because
   * the page had been open four hours would be a lie of exactly the kind this
   * wall exists to avoid.
   */
  const [quietSince, setQuietSince] = useState<number | null>(null);
  const [memory, setMemory] = useState<string>("");
  const wasCases = useRef<FleetStation[]>([]);
  useEffect(() => {
    const now = Date.now();
    const cases = s.geometry === "quiet" ? [] : [s.lead, ...s.files].filter(Boolean);
    if (cases.length === 0 && wasCases.current.length > 0) {
      setQuietSince(now);
      const last = wasCases.current[0] as FleetStation;
      setMemory(`last: ${last.name} recovered`);
    }
    if (cases.length > 0) setQuietSince(null);
    wasCases.current = cases as FleetStation[];
  }, [s]);

  // Re-rendered on frame arrival, which is what makes the beat evidence.
  const beat = lastFrameAt ?? 0;

  const total = ranked.length;
  const nominal = s.muster.length;

  return (
    <div className="quiet-wall" role="group" aria-label="What is wrong, worst first">
      <div className={`quiet-band ${s.geometry}`}>
        <span className="quiet-band-left">
          {/* Keyed on the frame so it remounts and replays once per digest.
              Only while nothing is wrong — the instant a case appears this
              stops, so the new case's own arrival is the only movement. */}
          {s.geometry === "quiet" && (
            <span key={beat} className="quiet-beat" aria-hidden="true" />
          )}
          <span className="quiet-band-count">
            watching · {total} station{total === 1 ? "" : "s"} ·{" "}
            {nominal === total ? "all nominal" : `${nominal} nominal`}
          </span>
        </span>

        {/* One mark per station, fixed order, NEVER coloured. A countable fleet
            is the thing a blank screen cannot give you: it is the difference
            between "nothing is wrong" and "nothing is connected". */}
        <span className="quiet-muster" aria-hidden="true">
          {total <= 150 ? (
            ranked.map((st) => <i key={st.id} className="quiet-mark" />)
          ) : (
            <em>{nominal} nominal</em>
          )}
        </span>

        <span className="quiet-band-right">
          {s.geometry === "quiet" ? (
            <>
              <span className="quiet-for">
                quiet {quietSince ? since(Date.now() - quietSince) : "since you opened this"}
              </span>
              {memory && <span className="quiet-memory">{memory}</span>}
            </>
          ) : (
            <span className="quiet-for warn">
              {s.geometry === "storm"
                ? `${s.files.length} stations in trouble`
                : `${1 + s.files.length} case${s.files.length ? "s" : ""}`}
            </span>
          )}
        </span>
      </div>

      {s.geometry === "cases" && s.lead && (
        <Lead s={s.lead} alert={alerts?.[s.lead.id]} onSelect={onSelect} />
      )}

      {s.geometry === "cases" &&
        s.files.map((f) => (
          <FileRow key={f.id} s={f} alert={alerts?.[f.id]} onSelect={onSelect} />
        ))}

      {s.geometry === "storm" && (
        <div className="quiet-storm">
          {groups.map((g) => (
            <Group key={g.key} g={g} onSelect={onSelect} />
          ))}
        </div>
      )}
    </div>
  );
}
