import { useEffect, useRef, type ReactNode } from "react";
import type { FleetStation } from "../types";

/**
 * One station opened out beside the wall, and nothing more.
 *
 * Deliberately a dead end. It renders the fleet projection the wall already
 * holds and reaches for nothing else — no fetch, no route, no modal, and above
 * all no way into the station's own console. The only path to a customer
 * station today is UserMenu -> switchOrganization -> window.location.reload(),
 * which revokes the session server-side on the way through; that is right when a
 * platform admin means to go and work inside a tenancy, and ruinous here,
 * because the operator glancing at one site would lose every other site with it.
 * The wall is the job. This shows what is already known and closes again.
 *
 * The wall therefore stays lit behind it: no scrim, and no `aria-modal`. An
 * operator watching for a second tile to turn amber must still be able to see it
 * happen with this open, and a screen reader must still be able to reach it.
 *
 * Every field the projection carries is rendered, and one the platform does not
 * know reads as "not reported" rather than as an empty row. On this wall a blank
 * cannot be told apart from a zero, and "0 % state of charge" and "we have no
 * idea what the battery is doing" are the two most different sentences here.
 *
 * Colour follows the wall's rule rather than decorating the panel. A section
 * takes a tone only when it has something to say — amber for trouble, red for a
 * site we have lost or a station calling itself failing — and a nominal section
 * carries no colour at all, so any colour at all is worth reading. Green appears
 * exactly once, in the liveness pip, where it means the station is talking to
 * us. It never means "fine".
 *
 * Every changing number sits in a `dd`, which is the single hook the stylesheet
 * needs to put tabular figures under all of them at once. A digit that changes
 * width reflows the row, and reflow is motion; on this wall motion is the alarm
 * channel and must not be spent on a battery ticking from 79 to 80.
 */

/** No "ok" on purpose. Green belongs to the liveness pip alone — a panel that
 *  can go green teaches an operator that colour is decoration, and then the one
 *  amber section that matters is furniture too. */
type Tone = "warn" | "bad" | "neutral";

/** The one place a null becomes visible text, so there is exactly one wording
 *  for it and no row below can invent a second. */
function Absent() {
  return <em>not reported</em>;
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

function Section({
  title,
  tone,
  children,
}: {
  title: string;
  /** Omitted on a nominal section, which is the point: "fine" is the absence of
   *  colour, not a colour of its own. */
  tone?: Tone;
  children: ReactNode;
}) {
  return (
    <section className={`odin-drawer-section${tone ? ` ${tone}` : ""}`}>
      <h3>{title}</h3>
      <dl>{children}</dl>
    </section>
  );
}

/** Coarse and human, because nobody reads a duration on a wall to the second.
 *  Shared by the uplink's offline count and the time since contact so the two
 *  cannot drift into different vocabularies for the same span. */
function spell(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ${m % 60} min`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}

/** No ticking clock of its own. This recomputes when the wall re-renders on its
 *  poll, which is as fresh as the fact behind it; a timer here would move a
 *  number on a surface where movement is supposed to mean something. */
function ago(iso: string): string {
  return `${spell((Date.now() - new Date(iso).getTime()) / 1000)} ago`;
}

/** The reader's own locale and timezone. The station's local time is a different
 *  question and an honest one, but the fleet projection carries no timezone, so
 *  answering it here would mean guessing from a latitude. */
function stamp(iso: string): string {
  return new Date(iso).toLocaleString();
}

/** Hemisphere letters rather than a minus sign, which is one pixel wide and the
 *  easiest character on a wall to lose. Five decimals is about a metre — enough
 *  to find a mast down a farm track, and past it the digits are noise. */
function coordinates(lat: number, lon: number): string {
  const ns = `${Math.abs(lat).toFixed(5)}° ${lat >= 0 ? "N" : "S"}`;
  const ew = `${Math.abs(lon).toFixed(5)}° ${lon >= 0 ? "E" : "W"}`;
  return `${ns}  ${ew}`;
}

/** Slot keys in the reader's words. Anything unlisted falls through to its own
 *  key with the underscores taken out, so a station fitted with something this
 *  console has never heard of is still listed rather than quietly dropped. */
const SLOT_NAMES: Record<string, string> = {
  adsb: "ADS-B",
  radio: "Radio",
  camera: "Camera",
  weather: "Weather",
  power: "Power",
  light: "Light",
  gps: "GPS",
};

/** `DeviceStatus`, spelled out. The distinction the station went to the trouble
 *  of making is the whole value of this row: nothing was ever meant to be in
 *  that slot, versus something was specified and cannot be found, versus
 *  something answered for months and has stopped. Rendering all three as
 *  "absent" would throw away the only thing that decides whether anyone drives
 *  out there. */
const SLOT_STATES: Record<string, string> = {
  present: "present",
  not_fitted: "nothing fitted",
  configured_absent: "specified, not found",
  stalled: "stopped answering",
  unsupported: "unsupported",
};

/** Faults, and `not_fitted` is deliberately not among them: an empty slot at a
 *  site that never had that sensor is a fact about the build, not a callout. */
const SLOT_FAULTS = new Set(["configured_absent", "stalled", "unsupported"]);

export function StationPreviewDrawer({
  station,
  onClose,
}: {
  station: FleetStation | null;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  // Hooks run on every render including the closed one, so the early return
  // below stays legal; the id is what they actually depend on.
  const id = station?.id ?? null;

  // Escape closes, handled in the CAPTURE phase and stopped there, so one press
  // closes the drawer and nothing behind it also acts on the same key. The wall
  // is a long-lived surface with its own bindings and must not be disturbed by
  // somebody dismissing a panel.
  useEffect(() => {
    if (!id) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopImmediatePropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [id, onClose]);

  // Focus lands on the way out. Focus returns to the document body rather than
  // to the tile that opened this: the wall re-sorts on every poll and a station
  // can leave the projection entirely while the drawer is open, so the element
  // we came from may no longer be the element it was. Focusing nothing is
  // better than focusing the wrong site.
  useEffect(() => {
    if (!id) return;
    closeRef.current?.focus();
    return () => document.body.focus();
  }, [id]);

  if (!station) return null;

  const {
    name,
    organization_name,
    status,
    dark,
    last_seen_at,
    is_simulated,
    model,
    config_version,
    health,
    worst_condition,
    condition_count,
    uplink_connected,
    uplink_offline_seconds,
    soc_pct,
    on_battery,
    load_w,
    slots,
    simulated_slots,
    running_version,
    latitude,
    longitude,
    locality,
    region,
  } = station;

  const statusWord =
    status === "never" ? "never seen" : dark ? "dark" : status;
  // The pip is the wall's own vocabulary, repeated here unchanged so the drawer
  // does not become a second language for the same fact.
  const pipTone =
    status === "online" ? "ok" : status === "never" ? "neutral" : dark ? "bad" : "warn";
  const stateTone: Tone | undefined =
    status === "online" ? undefined : status === "never" ? "neutral" : dark ? "bad" : "warn";

  const healthTone: Tone | undefined =
    health === "failing" ? "bad" : health === "degraded" ? "warn" : undefined;

  // Red is reserved for a site we have lost and for a station that calls itself
  // failing. A dead sensor on a station that is otherwise talking to us is amber
  // however annoying it is, because someone has to be able to tell the two apart
  // across a room.
  const slotEntries = Object.entries(slots ?? {});
  const devicesTone: Tone | undefined = slotEntries.some(([, s]) => SLOT_FAULTS.has(s))
    ? "warn"
    : undefined;

  // Thresholds this drawer owns, not the station's judgement — nothing in the
  // fleet projection grades a state of charge. Below 40 % a solar site is headed
  // somewhere overnight; below 20 % it is a callout. If the projection ever
  // carries a power condition, defer to it and delete this.
  const socTone: Tone | undefined =
    soc_pct === null || soc_pct === undefined
      ? undefined
      : soc_pct < 20
        ? "bad"
        : soc_pct < 40
          ? "warn"
          : undefined;

  const uplinkTone: Tone | undefined = uplink_connected === false ? "warn" : undefined;

  return (
    <aside
      className="odin-drawer"
      role="dialog"
      aria-label={`${name}, ${organization_name}`}
    >
      <div className="odin-drawer-head">
        <div>
          <h2>{name}</h2>
          <p>{organization_name}</p>
        </div>
        {/* Decorative: the word it stands for is the first row below, so
            nothing is lost by hiding a coloured dot from a screen reader. */}
        <span className={`odin-pip ${pipTone}`} aria-hidden="true" />
        <button
          ref={closeRef}
          type="button"
          className="odin-drawer-close"
          onClick={onClose}
          aria-label="Close station detail"
        >
          ×
        </button>
      </div>

      {/* Keyed on the station so selecting a different tile starts at the top of
          its own detail rather than inheriting the last one's scroll position. */}
      <div className="odin-drawer-body" key={station.id}>
        <Section title="State" tone={stateTone}>
          <Row label="Status">{statusWord}</Row>
          <Row label="Last contact">
            {/* A station that has never called in is not an unknown: it is a
                known and specific state, and saying "not reported" here would
                make a box that was never commissioned look like a gap in our
                telemetry. */}
            {last_seen_at === null ? (
              "never heard from"
            ) : (
              <>
                {ago(last_seen_at)} · <time dateTime={last_seen_at}>{stamp(last_seen_at)}</time>
              </>
            )}
          </Row>
          {/* Before any number below it, because it governs how all of them
              should be read. */}
          <Row label="Readings">
            {is_simulated ? "synthetic — this station reports simulated data" : "live"}
          </Row>
        </Section>

        <Section title="Health" tone={healthTone}>
          <Row label="Reported">{health ?? <Absent />}</Row>
          {/* The station names its own conditions and the platform does not
              re-judge them, so the worst is quoted verbatim. The projection
              carries only that one and a count — not the list — so the count is
              how this says there is more to see in the station's own console. */}
          <Row label="Worst condition">{worst_condition ?? <Absent />}</Row>
          <Row label="Open conditions">
            {condition_count === undefined ? (
              <Absent />
            ) : condition_count === 0 ? (
              "none open"
            ) : (
              condition_count
            )}
          </Row>
        </Section>

        <Section title="Uplink" tone={uplinkTone}>
          {/* Not the same question as Status above, and they disagree in exactly
              the interesting cases: Status is whether we have heard from the
              station, this is whether the station believes it is connected. A
              box that thinks it is online while nothing arrives here is a
              different fault from one that knows it has been cut off — and this
              answer is itself as old as the last frame, so a dark station's
              "connected" is a memory, not a claim about now. */}
          <Row label="Station's view">
            {uplink_connected === null || uplink_connected === undefined ? (
              <Absent />
            ) : uplink_connected ? (
              "connected"
            ) : (
              "disconnected"
            )}
          </Row>
          <Row label="Offline for">
            {uplink_offline_seconds === null || uplink_offline_seconds === undefined ? (
              <Absent />
            ) : (
              spell(uplink_offline_seconds)
            )}
          </Row>
        </Section>

        <Section title="Power" tone={socTone}>
          <Row label="State of charge">
            {soc_pct === null || soc_pct === undefined ? (
              <Absent />
            ) : (
              `${Math.round(soc_pct)} %`
            )}
          </Row>
          <Row label="Source">
            {on_battery === null || on_battery === undefined ? (
              <Absent />
            ) : on_battery ? (
              "stored power"
            ) : (
              "mains or generator carrying the load"
            )}
          </Row>
          <Row label="Load">
            {load_w === null || load_w === undefined ? <Absent /> : `${Math.round(load_w)} W`}
          </Row>
        </Section>

        <Section title="Devices" tone={devicesTone}>
          {slotEntries.length === 0 ? (
            <Row label="Slots">
              <Absent />
            </Row>
          ) : (
            slotEntries.map(([slot, state]) => (
              <Row key={slot} label={SLOT_NAMES[slot] ?? slot.replace(/_/g, " ")}>
                {/* The ingest writes "unknown" for a device the station listed
                    without a status, which is the same statement as a missing
                    field and gets the same wording. */}
                {state === "unknown" || !state ? (
                  <Absent />
                ) : (
                  SLOT_STATES[state] ?? state.replace(/_/g, " ")
                )}
                {/* Marked per slot, not per station, because a site is routinely
                    part real — a live camera beside a simulated weather head —
                    and a station-wide badge would be lying about one of them. */}
                {simulated_slots?.includes(slot) ? <em> synthetic</em> : null}
              </Row>
            ))
          )}
        </Section>

        <Section title="Position">
          <Row label="Coordinates">
            {latitude === null || longitude === null ? (
              // Worth more than the missing numbers themselves: a station with
              // no position is not drawn on the wall's map at all, so this row
              // is the only place its absence is ever explained.
              <>
                <Absent /> — not plotted on the map
              </>
            ) : (
              coordinates(latitude, longitude)
            )}
          </Row>
          <Row label="Locality">{locality ?? <Absent />}</Row>
          <Row label="Region">{region ?? <Absent />}</Row>
        </Section>

        <Section title="Identity">
          <Row label="Model">{model ?? <Absent />}</Row>
          <Row label="Running version">{running_version ?? <Absent />}</Row>
          <Row label="Config version">{config_version}</Row>
          {/* The one identifier worth carrying out of here: it is what a support
              conversation, a log search and the host shell all key on. The
              organisation's id is not shown — its name is at the top of the
              panel, and a second UUID would be noise in the place a reader is
              scanning for the first. */}
          <Row label="Station id">{station.id}</Row>
        </Section>
      </div>
    </aside>
  );
}
