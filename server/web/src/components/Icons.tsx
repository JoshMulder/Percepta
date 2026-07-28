/**
 * Inline stroke icons.
 *
 * Hand-drawn rather than an icon package: the console needs about a dozen, and
 * a dependency would ship hundreds. They inherit `currentColor` and size in em,
 * so they scale with the fluid root like everything else and pick up the colour
 * of whatever they sit in - a warning icon goes amber for free.
 *
 * Every one pairs with a text label. Icons here are for finding a panel at a
 * glance on a wall display, not for replacing the words.
 */

type Props = { className?: string };

const base = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
  focusable: false,
};

export function IconAirspace({ className = "icon" }: Props) {
  return (
    <svg {...base} className={className}>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="4.2" />
      <path d="M12 3v3.8M12 17.2V21M3 12h3.8M17.2 12H21" />
    </svg>
  );
}

export function IconCamera({ className = "icon" }: Props) {
  return (
    <svg {...base} className={className}>
      <path d="M3 8.5A2 2 0 0 1 5 6.5h2.4l1.3-2h6.6l1.3 2H19a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" />
      <circle cx="12" cy="12.5" r="3.4" />
    </svg>
  );
}

export function IconRadio({ className = "icon" }: Props) {
  return (
    <svg {...base} className={className}>
      <path d="M12 13v8" />
      <circle cx="12" cy="10.5" r="2.2" />
      <path d="M7.8 6.3a6 6 0 0 0 0 8.4M16.2 6.3a6 6 0 0 1 0 8.4" />
      <path d="M5 3.4a9.6 9.6 0 0 0 0 14.2M19 3.4a9.6 9.6 0 0 1 0 14.2" opacity="0.45" />
    </svg>
  );
}

export function IconLight({ className = "icon" }: Props) {
  return (
    <svg {...base} className={className}>
      <path d="M8.5 13.5 6 21h12l-2.5-7.5Z" />
      <path d="M12 3v3M6.5 5.2l1.9 2.2M17.5 5.2l-1.9 2.2" />
      <path d="M7.4 13.5h9.2" />
    </svg>
  );
}

export function IconPower({ className = "icon" }: Props) {
  return (
    <svg {...base} className={className}>
      <rect x="2.5" y="7.5" width="16" height="9" rx="2" />
      <path d="M21.5 11v2" />
      <path d="M11.6 9.4 8.9 12.6h3.6l-1.1 2.4" />
    </svg>
  );
}

export function IconWind({ className = "icon" }: Props) {
  return (
    <svg {...base} className={className}>
      <path d="M3 8.5h9.5a2.75 2.75 0 1 0-2.75-2.75" />
      <path d="M3 12.5h13a2.75 2.75 0 1 1-2.75 2.75" />
      <path d="M3 16.5h6.5" />
    </svg>
  );
}

export function IconAlert({ className = "icon" }: Props) {
  return (
    <svg {...base} className={className}>
      <path d="M18 9a6 6 0 1 0-12 0c0 5-2 6.5-2 6.5h16S18 14 18 9Z" />
      <path d="M10.3 19.5a2 2 0 0 0 3.4 0" />
    </svg>
  );
}

export function IconStation({ className = "icon" }: Props) {
  return (
    <svg {...base} className={className}>
      <path d="M9 21 12 9l3 12" />
      <path d="M10 16.5h4" />
      <path d="M8 6.4a5.4 5.4 0 0 1 8 0" />
      <path d="M5.6 3.6a8.8 8.8 0 0 1 12.8 0" opacity="0.5" />
    </svg>
  );
}

export function IconExpand({ className = "icon" }: Props) {
  return (
    <svg {...base} className={className}>
      <path d="M14 4h6v6M20 4l-7.5 7.5" />
      <path d="M10 20H4v-6M4 20l7.5-7.5" />
    </svg>
  );
}

export function IconLock({ className = "icon" }: Props) {
  return (
    <svg {...base} className={className}>
      <rect x="4.5" y="10.5" width="15" height="10" rx="2" />
      <path d="M8 10.5V7.8a4 4 0 0 1 8 0v2.7" />
    </svg>
  );
}

export function IconSpeaker({
  level,
  className = "icon",
}: {
  /** 0 muted, 1 low, 2 full. The waves are the level, so the icon reads at a
   *  glance without needing the slider beside it. */
  level: 0 | 1 | 2;
  className?: string;
}) {
  return (
    <svg {...base} className={className}>
      <path d="M4 9.5h3.5L12 5.5v13L7.5 14.5H4Z" />
      {level === 0 ? (
        <path d="M16 9.5l5 5M21 9.5l-5 5" />
      ) : (
        <>
          <path d="M15.6 9.6a3.6 3.6 0 0 1 0 4.8" />
          {level === 2 && <path d="M18.4 7a7.4 7.4 0 0 1 0 10" opacity="0.8" />}
        </>
      )}
    </svg>
  );
}

/** Filled dot for online/offline state, not a stroke icon. */
export function Dot({ ok }: { ok: boolean }) {
  return <span className={`dot${ok ? " ok" : ""}`} aria-hidden />;
}

/* --------------------------------------------------------------- sky ----- */

/** Present-weather icons. Larger and softer than the UI set: these are read at
 *  a glance from across a room, not scanned for in a toolbar. */
const sky = {
  viewBox: "0 0 48 48",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
  focusable: false,
};

const CLOUD = "M14 34a7 7 0 0 1 .6-13.9 10 10 0 0 1 19.1 2.2A6.4 6.4 0 0 1 33 34Z";

export function SkyIcon({
  state,
  isDay = true,
  className = "sky-icon",
}: {
  state?: "clear" | "partly" | "cloudy" | "rain" | "fog";
  isDay?: boolean;
  className?: string;
}) {
  if (state === "fog") {
    return (
      <svg {...sky} className={`${className} fog`}>
        <path d={CLOUD} opacity="0.55" />
        <path d="M10 39h28M14 44h20" />
      </svg>
    );
  }

  if (state === "rain") {
    return (
      <svg {...sky} className={`${className} rain`}>
        <path d={CLOUD} />
        <path d="M18 38l-2 5M25 38l-2 5M32 38l-2 5" />
      </svg>
    );
  }

  if (state === "cloudy") {
    return (
      <svg {...sky} className={`${className} cloudy`}>
        <path d={CLOUD} />
      </svg>
    );
  }

  if (state === "partly") {
    return (
      <svg {...sky} className={`${className} partly`}>
        {isDay ? (
          <>
            <circle cx="30" cy="15" r="6" />
            <path d="M30 4v3M30 23v3M39 15h3M18 15h3M36.4 8.6l2.1-2.1M21.5 23.5l2.1-2.1M36.4 21.4l2.1 2.1" opacity="0.7" />
          </>
        ) : (
          <path d="M34 9a8 8 0 1 0 6 12 9 9 0 0 1-6-12Z" />
        )}
        <path d={CLOUD} />
      </svg>
    );
  }

  // Clear.
  return isDay ? (
    <svg {...sky} className={`${className} clear`}>
      <circle cx="24" cy="24" r="8" />
      <path d="M24 6v5M24 37v5M42 24h-5M11 24H6M36.7 11.3l-3.5 3.5M14.8 33.2l-3.5 3.5M36.7 36.7l-3.5-3.5M14.8 14.8l-3.5-3.5" />
    </svg>
  ) : (
    <svg {...sky} className={`${className} clear night`}>
      <path d="M30 8a13 13 0 1 0 10 20A14 14 0 0 1 30 8Z" />
    </svg>
  );
}

export const SKY_LABEL: Record<string, string> = {
  clear: "Clear",
  partly: "Partly cloudy",
  cloudy: "Overcast",
  rain: "Rain",
  fog: "Fog",
};
