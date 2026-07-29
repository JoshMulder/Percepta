import { useEffect, useRef, useState } from "react";
import type { StationSummary } from "../types";

/**
 * The station switcher.
 *
 * A custom listbox rather than a native select, for one reason: a native
 * `<option>` can hold text and nothing else, and the DEMO marker needs to be a
 * badge in the list where a station is chosen. " · DEMO" appended to a name is
 * read as part of the name.
 *
 * Everything a native select gives away by not being one is put back
 * deliberately - keyboard navigation, Escape and outside-click to dismiss,
 * `role="listbox"` with `aria-selected`, and focus returning to the trigger.
 * A picker that only works with a mouse is worse than the ugly option.
 */
export function StationPicker({
  stations,
  stationId,
  onSelect,
}: {
  stations: StationSummary[];
  stationId: string | null;
  onSelect: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const current = stations.find((s) => s.id === stationId) ?? null;

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function choose(id: string) {
    onSelect(id);
    setOpen(false);
    trigger.current?.focus();
  }

  return (
    <div className="station-picker" ref={wrap}>
      <button
        ref={trigger}
        type="button"
        className="station-trigger"
        onClick={() => setOpen((o) => !o)}
        disabled={stations.length === 0}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="Ground station"
      >
        <span className="station-trigger-name">
          {current?.name ?? (stations.length === 0 ? "No stations available" : "Select a station")}
        </span>
        {/* Also on the closed control, not only in the open list. In the list it
            answers "which of these is synthetic" while choosing; here it answers
            "is what I am looking at right now real", which is the question an
            operator has for the rest of the session. */}
        {current?.is_simulated && (
          <span
            className="demo-chip"
            title="This station's data is synthetic — not a live site"
          >
            DEMO
          </span>
        )}
        <span className="station-caret" aria-hidden>
          ▾
        </span>
      </button>

      {open && (
        <ul className="station-list" role="listbox" aria-label="Ground stations">
          {stations.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                role="option"
                aria-selected={s.id === stationId}
                className={`station-option${s.id === stationId ? " selected" : ""}`}
                onClick={() => choose(s.id)}
              >
                <span className="station-option-name">{s.name}</span>
                {/* Which stations are synthetic matters most at the moment of
                    choosing one. Finding out afterwards, from a badge on a
                    panel you are already reading, is finding out too late. */}
                {s.is_simulated && (
                  <span
                    className="demo-chip"
                    title="This station's data is synthetic — not a live site"
                  >
                    DEMO
                  </span>
                )}
                {!s.online && <span className="station-offline">offline</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
