import { useMemo, useState } from "react";

import type { FleetStation } from "../types";
import type { WatchApi } from "../useWatchAudio";
import { panFor } from "../watchAudio";

/**
 * The listening watch, along the bottom of the wall.
 *
 * A dispatcher's channel strip, not a media player. What that means concretely:
 * every guarded channel is visible at once and none of them is "the current
 * one" — there is no selection here, because selecting is how a radio works and
 * a watch position is the opposite of a radio. The operator hears all of them
 * and the strip's whole job is to answer "which one is that" fast enough to be
 * useful while somebody is still talking.
 *
 * Three affordances per channel, and they are the three things people actually
 * do on a watch:
 *
 *   priority  Mark the channel that matters. The others duck under it rather
 *             than muting, so a second channel opening is still noticed.
 *   replay    The last few seconds again. "What did they just say" is the single
 *             most common thing asked of a monitored channel, and without this
 *             the answer is always "it's gone".
 *   release   Stop guarding. Frees a slot and stops the station sending.
 *
 * PAN POSITION IS SHOWN, not just applied. Stereo separation is what lets an
 * operator tell two simultaneous overs apart without looking — but only if they
 * know which side a channel lives on, so the strip states it. Position follows
 * strip order, so the picture and the sound agree.
 *
 * NO SOUND UNTIL THE VOLUME IS RAISED. Deliberate, and not only browser policy:
 * a wall display is in a shared room, and audio that starts because a page
 * loaded is audio that gets the wall muted at the operating system and then
 * never heard again.
 */

const MAX_CHANNELS = 8;

export function WatchStrip({
  stations,
  watch,
}: {
  /** The fleet, for the picker. Names come from the wall's own roster so a
   *  channel cannot be labelled differently here than on the tile it came from. */
  stations: FleetStation[];
  watch: WatchApi;
}) {
  const [adding, setAdding] = useState(false);

  const byId = useMemo(() => {
    const out: Record<string, FleetStation> = {};
    for (const s of stations) out[s.id] = s;
    return out;
  }, [stations]);

  const available = useMemo(
    () =>
      stations
        .filter((s) => !watch.guarded.includes(s.id))
        .sort((a, b) => a.name.localeCompare(b.name)),
    [stations, watch.guarded],
  );

  const full = watch.guarded.length >= MAX_CHANNELS;

  const guard = (stationId: string) => {
    if (full) return;
    watch.setGuarded([...watch.guarded, stationId]);
    setAdding(false);
  };

  const release = (stationId: string) => {
    watch.setGuarded(watch.guarded.filter((s) => s !== stationId));
    if (watch.priority === stationId) watch.setPriority(null);
  };

  return (
    <div className="odin-watch">
      <div className="odin-watch-head">
        <span className="odin-watch-label">WATCH</span>

        {/* The volume control is the only thing that starts audio. A slider
            moved off zero is both the browser's required gesture and an
            unambiguous request for sound in the room. */}
        <label className="odin-watch-vol">
          <span className="odin-watch-volicon" aria-hidden="true">
            {watch.volume > 0 ? "♪" : "∅"}
          </span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={watch.volume}
            onChange={(e) => watch.setVolume(Number(e.target.value))}
            aria-label="Watch volume"
          />
        </label>

        {watch.audioState === "unsupported" && (
          // Said rather than failed into silence: an operator who cannot hear a
          // channel needs to know it is their browser, not the site.
          <span className="odin-watch-note warn">
            no Opus decoder in this browser
          </span>
        )}
        {watch.audioState === "blocked" && watch.volume > 0 && (
          <span className="odin-watch-note warn">audio blocked by the browser</span>
        )}
        {watch.link === "denied" && (
          <span className="odin-watch-note warn">watch access refused</span>
        )}
        {watch.link === "closed" && (
          <span className="odin-watch-note warn">reconnecting…</span>
        )}

        <span className="odin-watch-count">
          {watch.guarded.length}/{MAX_CHANNELS}
        </span>
      </div>

      <div className="odin-watch-channels">
        {watch.guarded.map((stationId, index) => {
          const station = byId[stationId];
          const talking = watch.talking[stationId] === true;
          const isPriority = watch.priority === stationId;
          const pan = panFor(index, watch.guarded.length);
          return (
            <div
              key={stationId}
              className={`odin-chan${talking ? " talking" : ""}${
                isPriority ? " priority" : ""
              }`}
            >
              <div className="odin-chan-top">
                {/* The talk lamp. It is on because frames are ARRIVING — the
                    station builds audio only while its gate is open — rather
                    than because telemetry said squelch_open, which would have
                    cost this channel's ADS-B down the same queue as its
                    audio. */}
                <span className="odin-chan-lamp" aria-hidden="true" />
                <span className="odin-chan-name" title={station?.name ?? stationId}>
                  {station?.name ?? "unknown station"}
                </span>
                <button
                  type="button"
                  className="odin-chan-x"
                  onClick={() => release(stationId)}
                  title="Stop guarding this channel"
                  aria-label={`Release ${station?.name ?? "channel"}`}
                >
                  ×
                </button>
              </div>

              <div className="odin-chan-where">
                {station?.organization_name ?? ""}
                <span className="odin-chan-pan">
                  {pan < -0.05 ? "◀" : pan > 0.05 ? "▶" : "◆"}
                </span>
              </div>

              <div className="odin-chan-acts">
                <button
                  type="button"
                  className={`odin-chan-act${isPriority ? " on" : ""}`}
                  onClick={() =>
                    watch.setPriority(isPriority ? null : stationId)
                  }
                  title="Priority: the others duck under this one while it is talking"
                >
                  priority
                </button>
                <button
                  type="button"
                  className="odin-chan-act"
                  onClick={() => watch.replay(stationId)}
                  title="Play the last few seconds of this channel again"
                >
                  replay
                </button>
              </div>
            </div>
          );
        })}

        {watch.guarded.length === 0 && !adding && (
          <div className="odin-watch-empty">No channels guarded</div>
        )}

        {adding ? (
          <div className="odin-chan adding">
            <select
              className="odin-watch-pick"
              autoFocus
              defaultValue=""
              onChange={(e) => e.target.value && guard(e.target.value)}
              onBlur={() => setAdding(false)}
              aria-label="Guard a channel"
            >
              <option value="" disabled>
                Choose a station…
              </option>
              {available.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} — {s.organization_name}
                </option>
              ))}
            </select>
          </div>
        ) : (
          <button
            type="button"
            className="odin-watch-add"
            disabled={full || available.length === 0}
            onClick={() => setAdding(true)}
            title={full ? `At most ${MAX_CHANNELS} channels` : "Guard a channel"}
          >
            + guard
          </button>
        )}
      </div>
    </div>
  );
}
