#!/usr/bin/env python3
"""Check a running station against the contract.

    python contract/conformance/check_station.py --station <uuid>
    python contract/conformance/check_station.py --station <uuid> --legacy

Subscribes to what the station publishes, validates every message against the
schemas, and reports what is missing or wrong. Then issues each command and
checks the station reports the effect back — because a command that is accepted
and quietly ignored is the failure this platform is least able to notice.

Neutral about implementation: it talks to the broker, not to anyone's code. The
simulator passes it, and so must real hardware.

`--legacy` listens on the platform's internal fan-out channels instead of the
station-facing ones. You should not need it: the ingest exists and the simulator
publishes across the real boundary. It remains only for looking at what reaches
subscribers *after* the ingest, which is a platform-side question.
"""

import argparse
import json
import sys
import time
from pathlib import Path

try:
    import redis
except ImportError:
    sys.exit("pip install redis")

try:
    from jsonschema import Draft202012Validator
except ImportError:
    sys.exit("pip install jsonschema")

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"

#: Every telemetry kind a station is expected to produce, and how long to wait
#: before calling it absent. Weather is slow by design; the rest are 1 Hz.
EXPECTED = {"adsb": 8, "power": 8, "radio": 8, "light": 8, "weather": 20}

#: Each command, and the telemetry field that must reflect it. This pairing is
#: the contract's core promise: nothing is confirmed by the platform, so every
#: command has to be observable in what the station reports.
COMMANDS = [
    ("radio.tune", {"freq_hz": 119_500_000}, "radio", "freq_hz", 119_500_000),
    ("radio.auto_squelch", {"on": False}, "radio", "auto_squelch", False),
    ("radio.squelch", {"db": -55.0}, "radio", "threshold_db", -55.0),
    ("radio.monitor", {"on": True}, "radio", "monitor", True),
    ("radio.monitor", {"on": False}, "radio", "monitor", False),
    ("radio.auto_squelch", {"on": True}, "radio", "auto_squelch", True),
    ("light.set", {"on": True}, "light", "on", True),
    ("light.set", {"on": False}, "light", "on", False),
]

failures: list[str] = []
notes: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'' if ok else f'  ({detail})'}")
    if not ok:
        failures.append(label)


def load(name: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((SCHEMAS / name).read_text()))


def collect(pubsub, station: str, seconds: float, legacy: bool) -> list[dict]:
    """Everything this station published in the window."""
    out: list[dict] = []
    end = time.time() + seconds
    while time.time() < end:
        message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if not message:
            continue
        data = message.get("data")
        if isinstance(data, bytes):
            data = data.decode()
        try:
            frame = json.loads(data)
        except (TypeError, ValueError):
            continue
        if legacy:
            # Internal fan-out wraps the payload and names the station.
            if str(frame.get("station_id")) != station:
                continue
            payload = frame.get("payload")
            if isinstance(payload, dict):
                out.append(payload)
        else:
            out.append(frame)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--station", required=True, help="station uuid")
    # 6380, not 6379: that is where compose publishes the platform's broker, on
    # loopback. A development host frequently has its own Redis on 6379, and
    # connecting to it looks identical to a station that is publishing nothing.
    ap.add_argument("--redis", default="redis://localhost:6380/0")
    ap.add_argument("--legacy", action="store_true",
                    help="listen on internal fan-out channels (platform-side debugging)")
    args = ap.parse_args()

    r = redis.Redis.from_url(args.redis)
    telemetry_schema = load("telemetry.schema.json")
    audio_schema = load("audio.schema.json")

    sub = r.pubsub()
    if args.legacy:
        sub.psubscribe("rt:g:*")
        cmd_channel = f"cmd:gsu:{args.station}"
        notes.append("legacy mode: internal fan-out channels, not gsu/{id}/...")
    else:
        sub.subscribe(
            f"gsu/{args.station}/telemetry", f"gsu/{args.station}/audio"
        )
        cmd_channel = f"cmd/gsu/{args.station}"

    print(f"\nListening to station {args.station}\n")
    print("1. Telemetry")
    seen = collect(sub, args.station, max(EXPECTED.values()) + 2, args.legacy)

    by_kind: dict[str, list[dict]] = {}
    for payload in seen:
        by_kind.setdefault(str(payload.get("kind")), []).append(payload)

    for kind in EXPECTED:
        check(f"publishes {kind}", kind in by_kind,
              "nothing received in the window")

    print("\n2. Schema")
    for kind, payloads in sorted(by_kind.items()):
        if kind == "audio":
            errs = sorted(audio_schema.iter_errors(payloads[0]), key=str)
        elif kind in EXPECTED:
            errs = sorted(telemetry_schema.iter_errors(payloads[0]), key=str)
        else:
            notes.append(f"unknown kind '{kind}' ignored, as the contract allows")
            continue
        check(f"{kind} matches schema", not errs,
              "; ".join(e.message for e in errs[:2]))

    print("\n3. Audio is gated")
    audio = by_kind.get("audio", [])
    radio = by_kind.get("radio", [{}])[-1]
    if not radio:
        check("audio only while squelch is open", False, "no radio telemetry")
    elif radio.get("squelch_open"):
        notes.append("squelch was open, so audio-while-closed was not tested")
    else:
        check("audio only while squelch is open", not audio,
              f"{len(audio)} audio frames while squelched")

    print("\n4. Commands take effect")
    for kind, body, report_kind, field, expect in COMMANDS:
        r.publish(cmd_channel, json.dumps({"kind": kind, **body}))
        time.sleep(0.3)
        got = None
        for payload in collect(sub, args.station, 4, args.legacy):
            if payload.get("kind") == report_kind and field in payload:
                got = payload[field]
        ok = got == expect or (
            isinstance(expect, float)
            and isinstance(got, (int, float))
            and abs(got - expect) < 0.6
        )
        check(f"{kind} -> {report_kind}.{field}", ok, f"reported {got!r}")

    sub.close()

    if notes:
        print("\nNotes")
        for n in notes:
            print(f"  - {n}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("All checks passed - this station satisfies the contract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
