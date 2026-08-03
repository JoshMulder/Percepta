import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type {
  AdsbPayload,
  HealthPayload,
  Capability,
  LightPayload,
  MapConfig,
  Me,
  PowerPayload,
  RadioPayload,
  ServerMessage,
  StationDetail,
  AudioPayload,
  StationSummary,
  StatusPayload,
  WeatherPayload,
} from "../types";
import { isForSelectedStation } from "../telemetryRouting";
import { useAudio } from "../useAudio";
import { useFitScale } from "../useFitScale";
import { useMediaQuery } from "../useMediaQuery";
import { useSocket } from "../useSocket";
import { useVideoStream } from "../useVideoStream";
import { AdsbMap } from "./AdsbMap";
import { SOC_WINDOWS, type SocSample, type SocWindowKey } from "./BatteryChart";
import {
  IconAirspace,
  IconAlert,
  IconCamera,
  IconExpand,
  IconLight,
  IconPower,
  IconRadio,
  IconSettings,
  IconWind,
} from "./Icons";
import { Logo } from "./Logo";
import { StationPicker } from "./StationPicker";
import { OrgSwitcher } from "./OrgSwitcher";
import { Settings } from "./Settings";
import { FloodlightPanel, has, NotPermitted, PowerPanel, VideoPanel } from "./Panels";
import { MapSkeleton, PanelState, panelStatus } from "./PanelState";
import { RadioPanel } from "./RadioPanel";
import { WeatherPanel } from "./WeatherPanel";

/**
 * Below this the two-column layout stops being worth defending. Rather than
 * shrinking past the point where the numeric readouts are still glanceable, or
 * letting the sidebar scroll, the panels become horizontal tabs and whichever is
 * selected gets the whole window.
 *
 * Height is in the query as well as width: a wide but short window - a browser
 * with devtools docked, a letterbox display - has the same problem and the same
 * answer.
 */
const COMPACT = "(max-width: 56rem), (max-height: 34rem)";

/** Missed publishes before a stream is called stale.
 *
 *  Three, so a brief dropout does not paint the console red. This is the only
 *  number here that is a judgement; everything else is arithmetic on the
 *  station's own cadence. */
const STALE_AFTER_PUBLISHES = 3;

/** Fallback cadences in seconds, from `contract/transport.md`.
 *
 *  Used only until a health frame arrives — the station reports what it is
 *  actually doing in `health.cadence`, and that wins. These were previously
 *  the whole mechanism, written out as milliseconds already multiplied, with
 *  no link back to the table they came from. `weather_period_s` is a site
 *  setting and is settable at runtime, so a site that slowed weather down to
 *  save bandwidth got a permanent red X on a healthy station and no
 *  explanation on either side. */
const DEFAULT_CADENCE_S = {
  adsb: 1,
  weather: 5,
  power: 1,
  radio: 1,
  light: 1,
} as const;

type StreamKind = keyof typeof DEFAULT_CADENCE_S;

/** Milliseconds of silence before `kind` is stale, given what the station says
 *  about itself. Weather at 0.2 Hz gets the same three publishes as ADS-B at
 *  1 Hz, which is why this is a multiplier rather than a table. */
function staleAfterMs(
  kind: StreamKind, cadence: Partial<Record<StreamKind, number>>,
): number {
  const seconds = cadence[kind] ?? DEFAULT_CADENCE_S[kind];
  // A station reporting nonsense must not switch staleness off altogether.
  const safe = Number.isFinite(seconds) && seconds > 0
    ? seconds : DEFAULT_CADENCE_S[kind];
  return safe * STALE_AFTER_PUBLISHES * 1000;
}

/** What the console's own connection to the server is doing. Distinct from the
 *  dot beside it, which is the *station's* link - the two fail separately and
 *  an operator needs to tell which one has. */
const LINK_LABEL: Record<string, string> = {
  connecting: "Connecting",
  open: "Connected",
  closed: "Reconnecting",
  unauthenticated: "Signed out",
};

interface Alert {
  id: number;
  stationId: string;
  message: string;
  severity: "info" | "warning" | "critical";
  at: Date;
}

export function Console({ me, onSignedOut }: { me: Me; onSignedOut: () => void }) {
  const compact = useMediaQuery(COMPACT);
  // Off in the compact layout: there is no sidebar to fit there, and the tab
  // panel already takes the whole window.
  // Declared before useFitScale, which consumes it.
  const [fitReady, setFitReady] = useState(false);
  const fit = useFitScale({ enabled: !compact, ready: fitReady });
  const audio = useAudio(true);

  // The console's one and only <video>, created outside React and handed to
  // whichever VideoPanel is on screen. Swapping the map and the camera
  // remounts that component; an element React owned would be destroyed with
  // it, taking the socket, the MSE buffer and the station's encoder down and
  // rebuilding all three for what is visually a resize. This one is merely
  // re-parented, which a browser does without interrupting playback.
  const videoElRef = useRef<HTMLVideoElement | null>(null);
  if (videoElRef.current === null && typeof document !== "undefined") {
    const el = document.createElement("video");
    el.autoplay = true;
    el.muted = true;
    el.playsInline = true;
    videoElRef.current = el;
  }

  const [stations, setStations] = useState<StationSummary[]>([]);
  const [stationId, setStationId] = useState<string | null>(null);
  /** The same value, readable from the socket callback.
   *
   *  `handleMessage` has an empty dependency list on purpose — recreating it
   *  re-subscribes the socket — so it cannot close over `stationId`. Written
   *  during render rather than in an effect: an effect runs *after* paint, and
   *  a frame arriving in that gap would be admitted against the station just
   *  left, which is the entire bug this guards. */
  const selectedRef = useRef<string | null>(null);
  selectedRef.current = stationId;
  const [detail, setDetail] = useState<StationDetail | null>(null);
  const [mapConfig, setMapConfig] = useState<MapConfig | null>(null);
  const [mainView, setMainView] = useState<"adsb" | "video">("adsb");
  const [tab, setTab] = useState("airspace");
  const [alertsOpen, setAlertsOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  // A rename in settings has to reach the header without a reload. `me` is a
  // prop, so the new name is held here and overlays it.
  const [displayName, setDisplayName] = useState(me.display_name);
  const [seenAlerts, setSeenAlerts] = useState(0);
  const [lightPending, setLightPending] = useState(false);

  const [adsb, setAdsb] = useState<AdsbPayload | null>(null);
  const [weather, setWeather] = useState<WeatherPayload | null>(null);
  const [power, setPower] = useState<PowerPayload | null>(null);
  const [radio, setRadio] = useState<RadioPayload | null>(null);
  const [light, setLight] = useState<LightPayload | null>(null);
  const [health, setHealth] = useState<HealthPayload | null>(null);
  // Conditions already raised, so one that stays true for hours does not refill
  // the drawer once a second. A ref rather than state: it is bookkeeping, and
  // nothing renders from it.
  const raisedConditions = useRef<Set<string>>(new Set());
  // Streams the station says it has no source for, with its reason. Distinct
  // from a fault: nothing has failed, the hardware was never fitted.
  /** Streams whose own frames say the slot has nothing selected. Separate from
   *  `unavailable` because "no sensor" and "sensor not working" are opposite
   *  conclusions, and this one arrives on the stream's cadence rather than
   *  waiting for a health frame. */
  const [unfitted, setUnfitted] = useState<Record<string, boolean>>({});
  /** Streams whose own frames declare a demo sensor behind them. */
  const [demoStreams, setDemoStreams] = useState<Record<string, boolean>>({});
  const [unavailable, setUnavailable] = useState<
    Partial<Record<StreamKind, string>>
  >({});
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const alertSeq = useRef(0);
  // The message handler is memoised without audio in its deps, so it reads the
  // player through a ref rather than tearing down the socket handler whenever
  // the audio context is rebuilt.
  const audioRef = useRef(audio);
  audioRef.current = audio;

  // When each stream last delivered, and when the station's streams became
  // available at all. Both are needed: "never arrived" and "stopped arriving"
  // are different failures, and only the second is unambiguously a fault.
  const [lastSeen, setLastSeen] = useState<Partial<Record<StreamKind, number>>>({});
  /**
   * State of charge over the selected window, from the server's recorded
   * history rather than a browser buffer - a buffer reset on every reload and
   * could never reach further back than the moment the tab was opened, which
   * makes a 12-hour or 7-day view meaningless. See services/power_history.py.
   */
  const [socHistory, setSocHistory] = useState<SocSample[]>([]);
  const [socLoading, setSocLoading] = useState(false);
  const [socWindow, setSocWindow] = useState<SocWindowKey>("12h");
  const [streamsSince, setStreamsSince] = useState<number | null>(null);
  // Staleness is a function of elapsed time, so nothing re-renders on its own
  // when data simply stops. This ticks so a panel can go faulty in silence.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((n) => n + 1), 2000);
    return () => window.clearInterval(id);
  }, []);

  const handleMessage = useCallback((message: ServerMessage) => {
    // Frames for the station being left are still in flight when a switch
    // happens, and the switch has just emptied every panel — so they land in a
    // clean one and stick. Harmless for a stream the new station also
    // publishes, permanent for one it does not: this is how a Pi with no
    // weather head came to be displaying wind and pressure. See
    // telemetryRouting.ts; a ref because this callback is deliberately stable.
    if (!isForSelectedStation(message as { station_id?: string }, selectedRef.current)) {
      return;
    }
    if (message.type === "status") {
      const payload = message.payload as StatusPayload;
      if (!payload.alarm && payload.online === undefined) return;
      alertSeq.current += 1;
      setAlerts((prev) =>
        [
          {
            id: alertSeq.current,
            stationId: message.station_id,
            message: payload.alarm ?? (payload.online ? "Back online" : "Went offline"),
            severity: payload.severity ?? (payload.online ? "info" : "warning"),
            at: new Date(),
          },
          ...prev,
        ].slice(0, 40),
      );
      return;
    }

    if (message.type !== "event") return;
    const payload = message.payload as { kind?: string };
    if (payload.kind === "audio") {
      const a = message.payload as AudioPayload;
      audioRef.current.push(a);
      return;
    }
    if (payload.kind && payload.kind in DEFAULT_CADENCE_S) {
      const kind = payload.kind as StreamKind;
      setLastSeen((prev) => ({ ...prev, [kind]: Date.now() }));
      // A stream declaring itself unavailable is still arriving, so it counts
      // as live for staleness - the station is talking to us, it just has
      // nothing to measure with.
      const declared = message.payload as {
        available?: boolean;
        unavailable_reason?: string;
        unavailable_cause?: "not_fitted" | "not_detected" | "stopped";
      };
      // A demo sensor says so on every frame it sends. Recorded per stream:
      // one station can have a live camera and a demo weather head, and the
      // badge belongs on the panel that is synthetic rather than on all of
      // them.
      const declaresDemo = (message.payload as { simulated?: boolean }).simulated;
      if (declaresDemo !== undefined) {
        setDemoStreams((prev) =>
          prev[kind] === declaresDemo ? prev : { ...prev, [kind]: declaresDemo });
      }
      setUnfitted((prev) => {
        // Only the frames that actually declare themselves say anything here.
        // An available stream clears it; anything else leaves it alone.
        const empty = declared.available === false
          && declared.unavailable_cause === "not_fitted";
        if ((prev[kind] ?? false) === empty) return prev;
        const next = { ...prev };
        if (empty) next[kind] = true;
        else delete next[kind];
        return next;
      });
      setUnavailable((prev) => {
        const reason =
          declared.available === false
            ? (declared.unavailable_reason ?? "No source for this data")
            : undefined;
        if (prev[kind] === reason) return prev;
        const next = { ...prev };
        if (reason === undefined) delete next[kind];
        else next[kind] = reason;
        return next;
      });
      // An unavailable frame carries a kind, a flag and a reason - no readings.
      // It must never reach the panels as data: they render fields the frame
      // does not have, and the first real station (whose radio has no driver
      // and whose power slot is empty) took the whole console down with
      // `undefined.toFixed()`. The simulator sends every field, always, which
      // is why this survived until real hardware.
      if (declared.available === false) return;
    }
    switch (payload.kind) {
      case "adsb":
        setAdsb(message.payload as AdsbPayload);
        break;
      case "weather":
        setWeather(message.payload as WeatherPayload);
        break;
      case "power":
        setPower(message.payload as PowerPayload);
        break;
      case "radio":
        setRadio(message.payload as RadioPayload);
        break;
      case "health": {
        const h = message.payload as HealthPayload;
        setHealth(h);
        // A station's own conditions have to reach an operator. The one that
        // matters most is credential renewal failing: it is invisible until the
        // credential actually expires, and by then the fix is a site visit
        // (contract/enrolment.md section 6). Raised and cleared by the station;
        // the console surfaces them and never infers them.
        const station = message.station_id;
        const active = h.conditions ?? [];
        const seen = raisedConditions.current;
        const fresh = active.filter((c) => !seen.has(`${station}:${c.id}`));
        if (fresh.length > 0) {
          setAlerts((prev) =>
            [
              ...fresh.map((c) => ({
                id: ++alertSeq.current,
                stationId: station,
                message: c.detail ? `${c.id} — ${c.detail}` : c.id,
                severity: c.severity ?? ("warning" as const),
                at: new Date(),
              })),
              ...prev,
            ].slice(0, 40),
          );
          for (const c of fresh) seen.add(`${station}:${c.id}`);
        }
        // Forget the ones no longer true, so the same condition recurring later
        // is reported again rather than swallowed as a duplicate.
        const stillTrue = new Set(active.map((c) => `${station}:${c.id}`));
        for (const key of [...seen]) {
          if (key.startsWith(`${station}:`) && !stillTrue.has(key)) seen.delete(key);
        }
        break;
      }
      case "light":
        setLight(message.payload as LightPayload);
        setLightPending(false);
        break;
    }
  }, []);

  const socket = useSocket(handleMessage, true);

  // The server is the authority on capabilities. Until it has confirmed a
  // station selection, fall back to what the REST detail said - they come from
  // the same function server-side, so they agree; this only avoids a flash of
  // missing controls between selecting and the socket replying.
  const caps: Capability[] =
    socket.capabilities.length > 0 ? socket.capabilities : (detail?.capabilities ?? []);

  // Devices the station itself says are synthetic. This matters more than the
  // platform's own record: a real station agent, properly enrolled and
  // publishing, can still be running simulated drivers because no hardware is
  // attached - which is exactly the bench case, and it showed as a live station
  // with no badge at all. The station knows and says so; believing the record
  // over the station was the mistake.
  const simulatedDevices =
    health?.devices?.filter((d) => d.simulated).map((d) => d.slot) ?? [];

  /**
   * Whether one panel's readings are synthetic.
   *
   * Per stream, because a station is routinely part real — a bench box with a
   * live camera and a demo weather head is the normal way to develop against
   * one, and a single station-wide flag had to be wrong about one half of it.
   *
   * Three sources, cheapest and freshest first: the stream's own `simulated`,
   * which arrives at the stream's cadence; the slot report in health, which is
   * every 30 s and covers a stream that is not currently publishing; and the
   * deployment-wide override for showing the whole platform off.
   */
  const isDemo = (kind: StreamKind) =>
    me.demo_mode || demoStreams[kind] === true
    || simulatedDevices.includes(kind);

  // Synthetic data is a property of the station being watched, not of the
  // deployment: a real station and a simulated one can sit side by side in the
  // same switcher. DEMO_MODE remains as a deployment-wide override for showing
  // the whole platform off, but it is no longer how this is normally decided.
  // No station-wide demo flag here any more. The DEMO chip in the switcher
  // comes from the station record, which the server now derives from what the
  // station reports about its own sensors; everything on this page is decided
  // per stream by isDemo, because one station can be half real.

  const loadStations = useCallback(() => {
    api
      .stations()
      .then((list) => {
        setStations(list);
        // Keep the current selection only while it still exists. `current ??`
        // kept it unconditionally, so after a station was deleted the console
        // went on pointing at an id the platform no longer knew: the switcher
        // showed nothing selected, every panel sat empty, and the socket was
        // subscribed to a station that could never send anything. Falling back
        // to the first one is what an operator expects from a list that just
        // lost an entry; null when the list is empty is the honest end of it.
        setStationId((current) =>
          current && list.some((s) => s.id === current)
            ? current
            : list[0]?.id ?? null,
        );
      })
      .catch(() => setStations([]));
  }, []);

  useEffect(() => {
    loadStations();
  }, [loadStations]);

  useEffect(() => {
    if (!stationId) return;
    let cancelled = false;
    setDetail(null);
    setMapConfig(null);
    setAdsb(null);
    setHealth(null);
    raisedConditions.current = new Set();
    setUnavailable({});
    setUnfitted({});
    setDemoStreams({});
    setWeather(null);
    setPower(null);
    setRadio(null);
    setLight(null);
    // A station's battery history is its own; carrying it across would draw one
    // site's curve under another's readings.
    setSocHistory([]);
    // And drop any audio still queued from the station being left - playing out
    // the previous site's traffic after switching is worse than a gap.
    audioRef.current.flush();
    // Switching station clears every reading, so the new one starts from
    // "loading" rather than inheriting the previous station's freshness.
    setLastSeen({});
    setStreamsSince(null);
    api
      .station(stationId)
      .then((d) => !cancelled && setDetail(d))
      .catch(() => !cancelled && setDetail(null));
    api
      .mapConfig(stationId)
      .then((m) => !cancelled && setMapConfig(m))
      .catch(() => !cancelled && setMapConfig(null));
    return () => {
      cancelled = true;
    };
  }, [stationId]);

  // Selecting over the socket is what authorises the stream subscriptions; the
  // REST call above is only for detail rendering.
  useEffect(() => {
    if (stationId && socket.state === "open") socket.selectStation(stationId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stationId, socket.state]);

  // Subscribe to exactly the streams this user is cleared for. Asking for one we
  // do not hold would simply be refused, but not asking keeps the server's error
  // log meaningful.
  useEffect(() => {
    if (socket.state !== "open" || caps.length === 0) return;
    setStreamsSince((current) => current ?? Date.now());
    if (has(caps, "station.view")) socket.subscribe("status");
    if (has(caps, "telemetry.view")) socket.subscribe("telemetry");
      if (has(caps, "radio.listen")) socket.subscribe("audio");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [socket.state, caps.join(",")]);

  // History is fetched per station and per window, and refreshed periodically -
  // a console left open all shift should not show a chart that stops at the
  // moment it was opened.
  useEffect(() => {
    if (!stationId || !has(caps, "telemetry.view")) return;
    const hours =
      SOC_WINDOWS.find((w) => w.key === socWindow)?.hours ?? 12;
    let cancelled = false;
    const load = () => {
      setSocLoading(true);
      api
        .powerHistory(stationId, hours)
        .then((points) => {
          if (cancelled) return;
          setSocHistory(points.map((p) => ({ t: Date.parse(p.t), soc: p.soc })));
        })
        .catch(() => !cancelled && setSocHistory([]))
        .finally(() => !cancelled && setSocLoading(false));
    };
    load();
    const id = window.setInterval(load, 60_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stationId, socWindow, caps.join(",")]);

  // Once the station and its capabilities are known the panels stop changing
  // shape, and the sidebar can be measured once and revealed.
  useEffect(() => {
    if (stationId && caps.length > 0) setFitReady(true);
  }, [stationId, caps.length]);

  // Alerts arrive for every station the user can see, including the ones they
  // are not currently viewing - that is the whole point of the org status
  // channel, and the badge is what makes it useful while the drawer is shut.
  const unseen = Math.max(0, alerts.length - seenAlerts);

  const signOut = async () => {
    try {
      await api.logout();
    } finally {
      onSignedOut();
    }
  };

  const toggleLight = (on: boolean) => {
    if (!stationId) return;
    setLightPending(true);
    // The spinner is the only optimism here: real state arrives on the telemetry
    // stream from the station itself, because what matters is what the hardware
    // did, not what we asked it to do. The timeout clears the spinner if the
    // station never reports back.
    api.setLight(stationId, on).catch(() => setLightPending(false));
    setTimeout(() => setLightPending(false), 5000);
  };

  if (socket.revoked) {
    return (
      <div className="curtain">
        <div className="curtain-card">
          <h2>Session ended</h2>
          <p>{socket.revoked}</p>
          <button type="button" className="btn primary" onClick={onSignedOut}>
            Sign in again
          </button>
        </div>
      </div>
    );
  }

  /**
   * Nothing is known yet: no station picked, or the server has not said what
   * this user may do there.
   *
   * This matters more than it looks. Without it every panel rendered its "no
   * access" line during the first moment after load - a few words each - so the
   * stack's natural height was tiny, the sidebar's fit scale pinned to its
   * maximum, and the whole bar appeared enormous until capabilities arrived and
   * it snapped back. Showing full-height skeletons instead keeps the natural
   * height stable across the transition, and is also just truthful: "no access"
   * was a claim the console had no basis for yet.
   */
  const bootstrapping = !stationId || caps.length === 0;
  const stateFor = (kind: StreamKind) =>
    bootstrapping ? ("loading" as const) : statusOf(kind);

  const canVideo = has(caps, "video.view");
  const canTelemetry = has(caps, "telemetry.view");
  const aircraft = adsb?.aircraft ?? [];
  const adsbDeviceStatus = health?.devices?.find((d) => d.slot === "adsb")?.status;

  /**
   * Whether the station has a sensor selected for the slot behind a stream.
   *
   * `undefined` until a health frame has arrived, which is the honest answer:
   * "nobody has told us yet" must keep showing skeletons rather than jumping
   * to "not fitted" and then back again when the truth turns up.
   *
   * Slot and stream share a name for every panel here. `adsb` is excluded
   * because it does not have a panel — it draws on the map, which is always
   * rendered and carries its own fault indication in the contact count.
   */
  // Takes a slot name, not a stream name. They were the same word for every
  // panel — except the camera, whose slot is `camera` while its stream was
  // `video`. That stream is gone, so the distinction is visible here instead of
  // hidden behind two names that happened to coincide.
  const fittedFor = (slotName: StreamKind | "camera"): boolean | undefined => {
    const slots = health?.devices;
    if (!slots) return undefined;
    const slot = slots.find((d) => d.slot === slotName);
    // A slot the station does not report at all is not evidence of absence.
    return slot ? slot.status !== "not_fitted" : undefined;
  };

  const statusOf = (kind: StreamKind) => {
    // The station's own statement comes first, because it is a statement
    // rather than an inference. A slot with nothing selected is Not fitted the
    // moment the first health frame lands — no grace period, and it never
    // becomes a fault however long you wait.
    // The stream's own frame first: it arrives at the stream's cadence, where
    // the health frame that carries the same fact is every 30 seconds. On a
    // console that has just switched station that is the difference between
    // knowing now and showing a red X for half a minute.
    if (unfitted[kind] || fittedFor(kind) === false) return "not-fitted" as const;

    // A stream that declares itself unavailable is arriving but carries no
    // readings. It used to count as live, on the reasoning that the station is
    // talking to us — true, and the wrong conclusion for a *panel*, which then
    // sat showing dashes indefinitely and looked identical to one still
    // waiting. Reaching here means the slot is configured, so no source for it
    // is a fault: something was specified and is not delivering.
    if (unavailable[kind] && !isDemo(kind)) return "fault" as const;

    // What the platform says about the station, which it knew before any
    // telemetry arrived. `undefined` until the station list lands — that is
    // not knowing, and must not read as "offline".
    const stationOnline = stationId
      ? stations.find((s) => s.id === stationId)?.online
      : undefined;

    return panelStatus(
      lastSeen[kind] ?? null, streamsSince,
      staleAfterMs(kind, health?.cadence ?? {}),
      fittedFor(kind),
      stationOnline,
    );
  };

  const renderMap = (small: boolean) => {
    if (bootstrapping) return <MapSkeleton />;
    if (!canTelemetry) return <NotPermitted what="telemetry" />;
    if (!stationId || !mapConfig) return <MapSkeleton />;
    // The basemap is ours and always renders; only the ADS-B overlay can fail,
    // so a dead receiver must not black out the map an operator is using for
    // everything else. The contact count in the header carries the fault
    // instead - see the panel head below.
    return (
      <AdsbMap
        key={`${stationId}-${small ? "s" : "m"}`}
        stationId={stationId}
        config={mapConfig}
        aircraft={aircraft}
        compact={small}
      />
    );
  };

  // One stream for the console, not one per panel: `enabled` must not change
  // when the panel moves between slots, or the swap tears the pipeline down
  // exactly as remounting used to.
  const streamState = useVideoStream(
    videoElRef,
    stationId,
    Boolean(canVideo && (detail?.online ?? false)),
  );

  const renderVideo = (small: boolean) =>
    bootstrapping ? (
      <MapSkeleton />
    ) : canVideo ? (
      <VideoPanel
        compact={small}
        fitted={fittedFor("camera")}
        stationId={stationId}
        // Live wherever the panel is mounted - main stage, sidebar preview,
        // or a phone's camera tab. Attaching is what starts the station
        // encoding, so this means the camera runs whenever it is on somebody's
        // screen; decided deliberately, because an operator with the console
        // open is exactly the demand the on-demand design exists to detect,
        // and a preview showing seconds-old snapshots beside a live main view
        // reads as a broken camera. Mounting already tracks visibility - the
        // compact layout unmounts hidden tabs - and the lease still stops the
        // encoder when the console closes.
        live
        streaming={streamState === "playing"}
        videoEl={videoElRef.current}
        streamState={streamState}
        canPtz={!small && has(caps, "video.ptz")}
        online={detail?.online ?? false}
        demo={me.demo_mode || simulatedDevices.includes("camera")}
        // The synthetic scene lights up when the floodlight does, so a demo can
        // show a command reaching the hardware rather than just a state flag
        // flipping in a panel.
        lightOn={light?.on ?? false}
      />
    ) : (
      <NotPermitted what="video" />
    );

  const alertList = (
    <div className="alerts">
      {alerts.length > 0 && (
        <div className="alerts-head">
          <span className="muted">
            {alerts.length} {alerts.length === 1 ? "alert" : "alerts"}
          </span>
          {/* Clears the list an operator has read, not the conditions behind
              it. A station condition that is still true is re-raised by the
              next health frame, which is the point: this dismisses what has
              been seen and cannot hide anything that is still happening. */}
          <button
            type="button"
            className="alerts-clear"
            onClick={() => {
              setAlerts([]);
              // Forget what has been reported, so a condition that is still
              // true comes back rather than being swallowed as a duplicate.
              raisedConditions.current = new Set();
              setSeenAlerts(0);
            }}
          >
            Clear
          </button>
        </div>
      )}
      {alerts.length === 0 && <div className="muted">Nothing to report</div>}
      {alerts.map((a) => (
        <div key={a.id} className={`alert ${a.severity}`}>
          <span className="alert-station">
            {stations.find((s) => s.id === a.stationId)?.name ?? "Station"}
          </span>
          <span className="alert-msg">{a.message}</span>
          <span className="alert-time">
            {a.at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
          </span>
        </div>
      ))}
    </div>
  );

  // One definition, both layouts. The sidebar and the tab bar render from this
  // same list, so a panel can never appear in one and be forgotten in the other.
  const sections = [
    {
      key: "airspace",
      label: "Airspace",
      Icon: IconAirspace,
      body: renderMap(false),
      fills: true,
    },
    {
      key: "camera",
      label: "Camera",
      Icon: IconCamera,
      // An empty host; the panel is portalled in below. Compact mounts the
      // panel directly, because that layout shows one tab at a time and
      // unmounting a hidden camera is exactly what should happen there.
      body: renderVideo(false),
      fills: true,
    },
    {
      key: "radio",
      label: "Radio",
      Icon: IconRadio,
      body: (
        <PanelState
          status={
            bootstrapping
              ? "loading"
              : has(caps, "radio.listen")
                ? statusOf("radio")
                : "live"
          }
          label="Airband receiver"
        >
          <RadioPanel
            stationId={stationId ?? ""}
            radio={radio}
            caps={caps}
            onVolume={audio.setVolume}
            onUnmute={audio.unmute}
            onRetune={audio.flush}
            audioState={audio.state}
          />
        </PanelState>
      ),
      fills: false,
    },
    {
      key: "weather",
      label: "Weather",
      Icon: IconWind,
      body: canTelemetry || bootstrapping ? (
        <PanelState
          status={stateFor("weather")}
          label="Weather station"
        >
          <WeatherPanel weather={weather} />
        </PanelState>
      ) : (
        <NotPermitted what="weather telemetry" />
      ),
      fills: false,
    },
    {
      key: "light",
      label: "Floodlight",
      Icon: IconLight,
      body: (
        <PanelState
          status={stateFor("light")}
          label="Floodlight"
        >
          <FloodlightPanel
            light={light}
            caps={caps}
            onToggle={toggleLight}
            pending={lightPending}
          />
        </PanelState>
      ),
      fills: false,
    },
    {
      key: "power",
      label: "Power",
      Icon: IconPower,
      body: canTelemetry || bootstrapping ? (
        <PanelState
          status={stateFor("power")}
          label="Solar array"
        >
          <PowerPanel
            power={power}
            history={socHistory}
            historyLoading={socLoading}
            windowKey={socWindow}
            onWindowChange={setSocWindow}
          />
        </PanelState>
      ) : (
        <NotPermitted what="power telemetry" />
      ),
      fills: false,
    },
  ];

  const header = (
    // Amber across the whole header whenever this session reaches beyond an
    // ordinary membership - god mode in the platform org, or working inside
    // somebody else's tenant. A small badge is easy to stop seeing after the
    // first hour; a header that is the wrong colour is not, and this is exactly
    // the state where acting on the wrong organisation does real damage.
    <header
      className={`topbar${me.is_platform_admin || me.is_guest ? " elevated" : ""}`}
      title={
        me.is_guest
          ? "You are in this organisation as a platform administrator, not as a member"
          : me.is_platform_admin
            ? "Platform administration — you can see and change every organisation"
            : undefined
      }
    >
      <Logo />
      <div className="station-select">
        <StationPicker
          stations={stations}
          stationId={stationId}
          onSelect={setStationId}
        />
        {/* Whether the *station* is reachable is a property of each station and
            is marked in the picker, on the trigger and in the list.

            What survives here is the console's own link to the server, and only
            while it is unhealthy. A permanent "Connected" is noise an operator
            stops seeing within an hour, but losing the server silently is not
            something to find out from stale numbers. The two failures are
            different and were previously side by side, which is what made the
            pair read as one status. */}
        {socket.state !== "open" && (
          <span className="station-status">
            <span className={`link-state ${socket.state}`} title="Console's link to the server">
              {LINK_LABEL[socket.state]}
            </span>
          </span>
        )}
      </div>
      <div className="topbar-right">
        <OrgSwitcher me={me} />
        <button
          type="button"
          className={`btn ghost alerts-toggle${alertsOpen ? " active" : ""}`}
          onClick={() => {
            setAlertsOpen(!alertsOpen);
            if (!alertsOpen) setSeenAlerts(alerts.length);
          }}
          aria-expanded={alertsOpen}
          title="Alerts from every station you can see"
        >
          <IconAlert />
          <span>Alerts</span>
          {unseen > 0 && <span className="badge">{unseen > 9 ? "9+" : unseen}</span>}
        </button>
        <button
          type="button"
          className={`btn ghost settings-toggle${settingsOpen ? " active" : ""}`}
          onClick={() => setSettingsOpen(true)}
          aria-haspopup="dialog"
          title="Settings"
        >
          <IconSettings />
          <span>Settings</span>
        </button>
        <span className="who">{displayName}</span>
      </div>
    </header>
  );

  const alertsDrawer = (
    <>
      <div
        className={`drawer-scrim${alertsOpen ? " open" : ""}`}
        onClick={() => setAlertsOpen(false)}
        aria-hidden
      />
      <aside
        className={`drawer${alertsOpen ? " open" : ""}`}
        aria-label="Alerts"
        aria-hidden={!alertsOpen}
      >
        <div className="drawer-head">
          <IconAlert />
          <h3>Alerts</h3>
          <span className="muted">all stations</span>
          <button
            type="button"
            className="btn tiny"
            onClick={() => setAlertsOpen(false)}
            aria-label="Close alerts"
          >
            ✕
          </button>
        </div>
        <div className="drawer-body">{alertList}</div>
      </aside>
    </>
  );

  /* ---------------------------------------------------------- compact --- */

  if (compact) {
    const active = sections.find((s) => s.key === tab) ?? sections[0];
    return (
      <div className="console">
        {header}
        <nav className="tabs" role="tablist" aria-label="Station panels">
          {sections.map(({ key, label, Icon }) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={key === active.key}
              className={`tab${key === active.key ? " active" : ""}`}
              onClick={() => setTab(key)}
            >
              <Icon />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <main className="tab-body" role="tabpanel">
          <div className={active.fills ? "tab-fill" : "tab-scroll"}>{active.body}</div>
        </main>
        {alertsDrawer}
      {settingsOpen && (
        <Settings
          me={me}
          stationId={stationId}
          stationName={stations.find((s) => s.id === stationId)?.name ?? null}
          radio={radio}
          capabilities={caps}
          onClose={() => setSettingsOpen(false)}
          onProfileChanged={setDisplayName}
          onStationsChanged={loadStations}
          onSignOut={signOut}
        />
      )}

      </div>
    );
  }

  /* ------------------------------------------------------------ wide --- */

  const main = mainView === "adsb" ? sections[0] : sections[1];
  const preview = mainView === "adsb" ? sections[1] : sections[0];
  const rest = sections.slice(2);

  return (
    <div className="console">
      {header}
      <main className="layout">
        <section className="main-panel">
          <div className="panel-body">
            {main.body}
            {/* Only the ADS-B fault surfaces here now. The contact count was
                removed as noise - the aircraft are drawn on the map, so
                counting them again in a corner told an operator nothing they
                could not already see. A dead receiver is different: an empty
                map and a failed receiver look identical, so that has to be
                said out loud. */}
            {/* Two different things, and an operator has to be able to tell
                them apart. Unavailable means the station has no ADS-B receiver
                at all; a fault means it had one and it stopped. */}
            {mainView === "adsb" && canTelemetry && unavailable.adsb && (
              <div className="view-status">
                {/* "Never fitted" and "fitted and stopped answering" both make
                    the stream unavailable and need different reactions, so the
                    badge is taken from health's structured status rather than
                    from the prose reason. */}
                <span className="no-source-badge" title={unavailable.adsb}>
                  {adsbDeviceStatus === "stalled" ||
                  adsbDeviceStatus === "configured_absent"
                    ? "ADS-B FAULT"
                    : "NO ADS-B"}
                </span>
              </div>
            )}
            {mainView === "adsb" &&
              canTelemetry &&
              !unavailable.adsb &&
              statusOf("adsb") === "fault" && (
                <div className="view-status">
                  <span className="fault-inline">ADS-B receiver — no data</span>
                </div>
              )}
          </div>
        </section>

        <aside
          className="sidebar"
          ref={fit.outerRef}
          // Hidden until the fit has run, so the console is never shown at the
          // wrong size. Width comes from the stylesheet in rem and follows the
          // root size the fit sets - there is no transform and no inline width.
          style={fitReady ? undefined : { visibility: "hidden" }}
        >
          <div className="sidebar-scale" ref={fit.innerRef}>
          {/* The whole preview is the control: clicking it promotes it to the
              main display. A <button> rather than a click handler on a div, so
              it is keyboard reachable and announced as an action. */}
          <section className="card swap-card">
            <div className="card-head">
              <preview.Icon />
              <h3>{preview.label}</h3>
              <span className="muted">click to enlarge</span>
            </div>
            <button
              type="button"
              className="swap-preview"
              onClick={() => setMainView(mainView === "adsb" ? "video" : "adsb")}
              aria-label={`Show ${preview.label} in the main display`}
            >
              <span className="swap-inner">
                {preview.key === "airspace" ? renderMap(true) : renderVideo(true)}
              </span>
              <span className="swap-hint">
                <IconExpand />
              </span>
            </button>
          </section>

          {rest.map(({ key, label, Icon, body }) => (
            <section key={key} className="card">
              <div className="card-head">
                <Icon />
                <h3>{label}</h3>
              </div>
              <div className="card-body">{body}</div>
            </section>
          ))}
          </div>
        </aside>
      </main>
      {alertsDrawer}
      {/* Outside the fit-scaled stack on purpose: useFitScale sizes the console
          from the sidebar's natural height, so anything inside it changes the
          scale of everything else simply by existing. */}
      {settingsOpen && (
        <Settings
          me={me}
          stationId={stationId}
          stationName={stations.find((s) => s.id === stationId)?.name ?? null}
          radio={radio}
          capabilities={caps}
          onClose={() => setSettingsOpen(false)}
          onProfileChanged={setDisplayName}
          onStationsChanged={loadStations}
          onSignOut={signOut}
        />
      )}
    </div>
  );
}
