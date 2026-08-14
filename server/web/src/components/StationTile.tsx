import { useEffect, useRef, useState } from "react";
import type { FleetStation } from "../types";

/**
 * One station on the ODIN wall, meant to be read from three metres away by
 * somebody who is not looking directly at it.
 *
 * The whole tile is built around one rule: **"fine" has no colour.** A nominal
 * station is a flat neutral face, and every accent on this wall — amber, red,
 * gold, the green pip — is spent on something an operator should walk towards.
 * That is why nothing here ever renders the " ok" tone, and why there is no
 * green anywhere in this file. A wall of green teaches an operator that colour
 * is decoration, and the first hour of that training costs more than the tile
 * ever gave back. Absence of colour is the good news.
 *
 * Colour therefore arrives in a strict order of loudness:
 *
 *   the band     6px across the top, the one carrier of station-level state
 *   the halo     60 seconds of decay after the state *changes*, so a wall
 *                catches the eye at the moment something happens
 *   a glyph      one device in trouble, on a station we can still reach
 *   the badge    a count of the conditions the station itself has raised
 *
 * The four vitals distinguish three things that look alike and are not:
 * **present**, **not fitted**, and **failed**. An operator does something
 * different about each — nothing, nothing, and drive four hours — and the tile
 * that collapses them is the tile that sends somebody to a site with no radio
 * to look for a radio fault. Not-fitted is carried by *form* (the glyph is
 * struck through, as `NoSource` does in PanelState) rather than by colour,
 * because it is not a fault and must not appear in a scan for colour.
 *
 * Values that are merely unknown never render as zero. A station that has gone
 * quiet stops having a state of charge; it does not acquire a flat battery.
 *
 * For the stylesheet, three things this file cannot express and depends on:
 * `odin-tile-name` at 15px or more, tabular figures on `odin-tile-fault` so a
 * count going 9 → 10 does not reflow, and `aria-current="true"` as the hook for
 * the selected tile — selection is a state, not a tone, so it has no class of
 * its own and rides the accessible attribute that already means it.
 */

/** Only three of the four shared tones are reachable from here. See above. */
type Tone = "neutral" | "warn" | "bad";

/** How long a tile stays lit after its state changes. Long enough that an
 *  operator who looked away still sees it; short enough that a shift change
 *  does not inherit a wall of halos from an hour ago. */
const HALO_MS = 60_000;

/* ------------------------------------------------------------- vitals --- */

type VitalKind = "link" | "battery" | "radio" | "camera";

/** present = there and answering; absent = nothing is meant to be here;
 *  fault = something is meant to be here and is not answering. */
type VitalState = "present" | "absent" | "fault";

interface Vital {
  kind: VitalKind;
  label: string;
  state: VitalState;
  tone: Tone;
  /** Said in the operator's words, on hover. */
  detail: string;
  /** The station says this slot's readings are synthetic. */
  demo: boolean;
}

/**
 * The device-status vocabulary is the contract's, not ours — see
 * `devices[].status` in contract/schemas/telemetry.schema.json, which also
 * gives the rule for the last line here: **treat an unrecognised value as
 * `stalled`**, the conservative reading, rather than inventing a state or
 * quietly showing nothing.
 *
 * A slot missing from the map entirely is a different fact and gets the other
 * answer. The platform builds `slots` from the station's own device report, so
 * a missing key means the station has never mentioned this slot — which is
 * "the platform knows of no device here", not "the device has failed".
 */
function fromSlot(status: string | undefined): { state: VitalState; detail: string } {
  switch (status) {
    case undefined:
    case "not_fitted":
      return { state: "absent", detail: "not fitted" };
    case "present":
      return { state: "present", detail: "present" };
    case "configured_absent":
      return { state: "fault", detail: "configured, not found" };
    case "stalled":
      return { state: "fault", detail: "stopped answering" };
    case "unsupported":
      return { state: "fault", detail: "no driver in this build" };
    default:
      return { state: "fault", detail: `unrecognised state: ${status}` };
  }
}

/** Amber, not red, for a device fault. Red on this wall means the station is
 *  gone; a stalled receiver on a station we can still talk to is a job for
 *  tomorrow, and spending red on it leaves nothing louder for the site that has
 *  actually dropped off the map. */
function toneFor(state: VitalState): Tone {
  return state === "fault" ? "warn" : "neutral";
}

function linkVital(s: FleetStation): Vital {
  const base = { kind: "link" as const, label: "Uplink", demo: false };
  if (s.status === "never") {
    // Enrolled and not yet installed. Nothing is wrong; nothing has happened.
    return { ...base, state: "absent", tone: "neutral", detail: "never connected" };
  }
  if (s.dark) {
    return { ...base, state: "fault", tone: "bad", detail: `dark${elapsed(s.last_seen_at)}` };
  }
  if (s.status === "offline") {
    return { ...base, state: "fault", tone: "warn", detail: `offline${elapsed(s.last_seen_at)}` };
  }
  // The interesting disagreement: we are hearing from it, and it says its own
  // link home is down. `status` is whether WE have heard from it; this is
  // whether IT believes it is connected, and a station reporting a broken
  // uplink over that same uplink is worth a second look.
  if (s.uplink_connected === false) {
    const been =
      s.uplink_offline_seconds != null ? ` for ${shortDuration(s.uplink_offline_seconds)}` : "";
    return { ...base, state: "fault", tone: "warn", detail: `station reports its link down${been}` };
  }
  return { ...base, state: "present", tone: "neutral", detail: "connected" };
}

function batteryVital(s: FleetStation, demo: boolean): Vital {
  let { state, detail } = fromSlot(s.slots?.power);

  // A slot reported empty while readings are still arriving is a contradiction,
  // and it is resolved here the way PanelState resolves the same one: believe
  // the data that is actually turning up. Otherwise a wall would strike out the
  // battery on a site that is at that moment telling us its charge.
  if (state === "absent" && s.soc_pct != null) {
    state = "present";
    detail = "reporting";
  }

  const parts = [detail];
  if (s.soc_pct != null) parts.push(`${Math.round(clamp(s.soc_pct))}% charge`);
  // Named here as well as marked on the bar, because the bar is a shape and
  // this is the sentence that explains it.
  if (s.on_battery) parts.push("on battery");

  return {
    kind: "battery",
    label: "Battery",
    state,
    tone: toneFor(state),
    detail: parts.join(", "),
    demo,
  };
}

function slotVital(kind: "radio" | "camera", label: string, s: FleetStation): Vital {
  const { state, detail } = fromSlot(s.slots?.[kind]);
  return {
    kind,
    label,
    state,
    tone: toneFor(state),
    detail,
    demo: (s.simulated_slots ?? []).includes(kind),
  };
}

/* ------------------------------------------------------------- glyphs --- */

/**
 * Drawn here rather than taken from Icons.tsx, and that is a distance decision
 * rather than a stylistic one. Those are em-sized panel-header marks with fine
 * detail — IconRadio's outer arc is at 0.45 opacity — and detail that thin
 * disappears on a wall and leaves a smudge. These four are heavier, simpler,
 * and drawn as one family so the row reads as a row. The camera keeps the shape
 * IconCamera uses, so a camera still looks like a camera across the product.
 */
const stroke = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2.2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
  focusable: false,
};

function VitalMark({ kind, struck }: { kind: VitalKind; struck: boolean }) {
  return (
    <svg {...stroke}>
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
      {/* Not fitted, struck through: the same idiom as NoSource, and the reason
          absence needs no colour of its own. Position in the row still says
          which slot it is, so the strike is free to say only "nothing here". */}
      {struck && <path d="M3.4 20.6 20.6 3.4" />}
    </svg>
  );
}

/* -------------------------------------------------------------- state --- */

/**
 * The band's colour, and the only station-level colour decision in the file.
 *
 * The brief's four cases are reachability — nominal, offline, dark, never — and
 * health is folded in underneath them because "nominal" means *nothing is
 * wrong*, and a station reporting itself as failing is not that. Without this a
 * site that is online and falling over would carry no band colour at all, and
 * the only thing saying so would be a badge you have to be close enough to read.
 *
 * `never` stays neutral deliberately. A station enrolled last week and not yet
 * installed has not failed at anything.
 */
function bandTone(s: FleetStation): Tone {
  if (s.status === "never") return "neutral";
  if (s.dark) return "bad";
  if (s.status === "offline") return "warn";
  if (s.health === "failing") return "bad";
  if (s.health === "degraded") return "warn";
  return "neutral";
}

function stateWord(s: FleetStation): string {
  if (s.status === "never") return "never connected";
  if (s.dark) return "dark";
  if (s.status === "offline") return "offline";
  if (s.health === "failing") return "online, failing";
  if (s.health === "degraded") return "online, degraded";
  return "online";
}

/* -------------------------------------------------------------- units --- */

function clamp(pct: number): number {
  return Math.min(100, Math.max(0, pct));
}

/** One significant unit, because this is read at a glance and "2h" answers the
 *  question that "2 h 14 m 08 s" only decorates. */
function shortDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  const minutes = seconds / 60;
  if (minutes < 90) return `${Math.round(minutes)}m`;
  const hours = minutes / 60;
  if (hours < 48) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}

function elapsed(iso: string | null): string {
  if (!iso) return "";
  const ms = Date.now() - Date.parse(iso);
  // Negative means the station's clock ran ahead of ours, which says nothing
  // useful about how long it has been quiet. Saying nothing is the honest
  // version of not knowing.
  if (!Number.isFinite(ms) || ms < 0) return "";
  return ` for ${shortDuration(ms / 1000)}`;
}

/* --------------------------------------------------------------- tile --- */

export function StationTile({
  station,
  selected,
  onSelect,
}: {
  station: FleetStation;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  const tone = bandTone(station);

  // State changed recently, so the tile pulses. Motion is the loudest channel
  // on a wall — it is caught in peripheral vision by somebody facing away — so
  // it is spent only on the moment a station's state actually turns over, not
  // on it merely continuing to be broken.
  //
  // The key is the three facts the halo is about, so a state that changes and
  // changes back within the window fires twice, as it should.
  const stateKey = `${station.status}|${station.dark}|${station.health ?? ""}`;
  const previous = useRef(stateKey);
  const [changedAt, setChangedAt] = useState<number | null>(null);

  useEffect(() => {
    // Seeded with the first state we ever saw, so opening the wall does not
    // light up every tile on it. A halo has to mean "this just happened", and a
    // hundred of them on load means nothing at all.
    if (previous.current === stateKey) return;
    previous.current = stateKey;
    setChangedAt(Date.now());
    const timer = window.setTimeout(() => setChangedAt(null), HALO_MS);
    return () => window.clearTimeout(timer);
  }, [stateKey]);

  const vitals: Vital[] = [
    linkVital(station),
    batteryVital(station, (station.simulated_slots ?? []).includes("power")),
    slotVital("radio", "Radio", station),
    slotVital("camera", "Camera", station),
  ];

  const conditions = station.condition_count ?? 0;

  // Null is not zero. A station that has gone quiet has no state of charge, and
  // a zero-width bar in the track is a picture of a dead battery — the loudest
  // possible way to say "we do not know". Nothing is drawn instead. A genuine
  // 0% does render, empty, because that is what it is.
  const soc = station.soc_pct == null ? null : clamp(station.soc_pct);

  const where = [station.locality, station.region].filter(Boolean).join(", ");
  const summary = [
    `${station.name} — ${station.organization_name}`,
    where,
    `${stateWord(station)}${station.status === "online" ? "" : elapsed(station.last_seen_at)}`,
    soc != null ? `${Math.round(soc)}% charge${station.on_battery ? ", on battery" : ""}` : "",
    conditions > 0
      ? `${conditions} open condition${conditions === 1 ? "" : "s"}${
          station.worst_condition ? ` — worst: ${station.worst_condition}` : ""
        }`
      : "",
    station.running_version ? `running ${station.running_version}` : "",
  ]
    .filter(Boolean)
    .join("\n");

  // Spelled out rather than left to the tile's contents: read aloud, the
  // contents are a name, an organisation and four wordless marks, which is not
  // what the tile says.
  const spoken = [
    station.name,
    station.organization_name,
    stateWord(station),
    station.is_simulated ? "simulated station" : "",
    soc != null ? `${Math.round(soc)} per cent charge` : "",
    conditions > 0 ? `${conditions} open condition${conditions === 1 ? "" : "s"}` : "",
  ]
    .filter(Boolean)
    .join(", ");

  return (
    <button
      type="button"
      // The tone rides the tile as well as the band, but only so the stylesheet
      // can let a dead station's whole face go quiet. The band stays the colour
      // carrier; this must not become a second flood of it.
      className={`odin-tile ${tone}`}
      onClick={() => onSelect(station.id)}
      aria-current={selected}
      aria-label={spoken}
      title={summary}
    >
      <span className={`odin-tile-band ${tone}`} aria-hidden="true" />

      {/* Keyed on the moment of change so a second change inside the window
          remounts the element and restarts the decay from full. Without the key
          the element persists and the animation, already spent, never replays.
          It carries the tone so the pulse says which way the state went: a site
          coming back pulses neutral, one going dark pulses red. */}
      {changedAt !== null && (
        <span key={changedAt} className={`odin-tile-halo ${tone}`} aria-hidden="true" />
      )}

      <span className="odin-tile-name">{station.name}</span>
      <span className="odin-tile-org">
        {station.organization_name}
        {/* The platform's own flag, and a weaker statement than the per-slot
            one: the station is authoritative about which of its sensors are
            synthetic, while this only says the record was created as a demo.
            Same marker, and its placement is its scope — here it covers the
            station, on a glyph it covers that one device. */}
        {station.maintenance_until && (
        // Deliberately quiet, not healthy. A silenced station raises nothing,
        // so without this marker it looks exactly like a site with nothing
        // wrong — and "we know about it" is not "it is fine".
        <span
          className="odin-tile-maint"
          title={`Silenced: ${station.maintenance_reason ?? "no reason given"}`}
        >
          HUSH
        </span>
      )}
      {station.is_simulated && (
          // Spelled out. It was an empty span styled for text, which measured
          // 10x2px — an amber sliver nobody would read at three metres, guarding
          // the one rule that says a wall must never show synthetic numbers as
          // though they were real.
          <span className="odin-tile-demo" title="This station reports synthetic data">
            DEMO
          </span>
        )}
      </span>

      {soc != null && (
        // Marked amber when discharging, on the track rather than the tile:
        // a solar site runs on stored power every night, and a mark that turns
        // the whole wall amber at dusk is a mark nobody reads by midwinter.
        // No thresholds on the fill. The bar's length is the value, and whether
        // that length is a problem is a judgement the station makes and
        // publishes as a condition — the platform does not invent one for it.
        <span
          className={`odin-tile-soc${station.on_battery ? " warn" : ""}`}
          aria-hidden="true"
          title={`${Math.round(soc)}% charge${station.on_battery ? ", on battery" : ""}`}
        >
          {/* The one inline style in the file, for the same reason ChartFrame
              has one: a measured value has to reach the DOM as a number, and
              there is no class that carries 63%. */}
          <span className="odin-tile-soc-fill" style={{ width: `${soc}%` }} />
        </span>
      )}

      <span className="odin-tile-vitals">
        {vitals.map((v) => (
          <span
            key={v.kind}
            className={`odin-tile-glyph ${v.tone}`}
            title={`${v.label}: ${v.detail}${v.demo ? " (simulated)" : ""}`}
          >
            <VitalMark kind={v.kind} struck={v.state === "absent"} />
            {v.demo && (
              // Scoped to this slot, and now actually inside it: .odin-tile-glyph
              // is a positioned ancestor, so this lands on the glyph rather than
              // stacking in the tile's corner with every other marker.
              <span className="odin-tile-demo slot" aria-label="simulated" />
            )}
          </span>
        ))}
      </span>

      {conditions > 0 && (
        // Amber unless the station has called itself failing. The count is the
        // glanceable part and the naming is on hover, because a condition code
        // is unreadable at three metres and the number is not.
        <span
          className={`odin-tile-fault ${station.health === "failing" ? "bad" : "warn"}`}
          title={`${conditions} open condition${conditions === 1 ? "" : "s"}${
            station.worst_condition ? ` — worst: ${station.worst_condition}` : ""
          }`}
        >
          {conditions}
        </span>
      )}
    </button>
  );
}
