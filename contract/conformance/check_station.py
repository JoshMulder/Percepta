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
import os
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

#: Kinds the contract defines but does not require a station to send. Validated
#: when present - a station that reports health must report it correctly - and
#: never demanded, because a station is not less conformant for staying quiet
#: about itself.
OPTIONAL_KINDS = {"health"}

#: How long a command has to be reflected in telemetry. Generous: a station on a
#: slow tick is not a broken station, and this waits only as long as it needs to.
COMMAND_TIMEOUT = 8.0

#: Streams a station may declare unavailable instead of reporting. Commands
#: against an unavailable stream are not checked - there is no hardware to obey
#: them, and failing a station for that would mean the only way to pass is to
#: pretend.
#:
#: Each command below, and the telemetry field that must reflect it. This
#: pairing is the contract's core promise: nothing is confirmed by the platform,
#: so every command has to be observable in what the station reports.
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
    """Everything this station published in the window, in order.

    Order matters for the audio-gating check: it pairs each audio frame with the
    squelch state the station last reported, so the frames have to arrive in the
    sequence they were sent.
    """
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


def unavailable_now(by_kind: dict[str, list[dict]]) -> set[str]:
    """Streams the station has declared it has no source for."""
    return {
        k for k, payloads in by_kind.items()
        if any(p.get("available") is False for p in payloads)
    }


def matches(got, expect) -> bool:
    if got == expect:
        return True
    # Frequencies and thresholds are snapped and rounded station-side, so an
    # exact float match would fail a station doing the right thing.
    return (
        isinstance(expect, float)
        and isinstance(got, (int, float))
        and not isinstance(got, bool)
        and abs(got - expect) < 0.6
    )


def await_report(pubsub, station: str, kind: str, field: str, expect,
                 seconds: float, legacy: bool):
    """Wait until the station reports `field` as `expect`, or time out.

    Returns the last value seen and whether it matched. Polling for the answer
    rather than sampling a window and taking the final value is what makes this
    check deterministic: the old version could miss a correct report simply
    because the window ended a moment too early.
    """
    got = None
    end = time.time() + seconds
    while time.time() < end:
        message = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
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
            if str(frame.get("station_id")) != station:
                continue
            frame = frame.get("payload")
            if not isinstance(frame, dict):
                continue
        if frame.get("kind") != kind or field not in frame:
            continue
        got = frame[field]
        if matches(got, expect):
            return got, True
    return got, False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--station", required=True, help="station uuid")
    # 6380, not 6379: that is where compose publishes the platform's broker, on
    # loopback. A development host frequently has its own Redis on 6379, and
    # connecting to it looks identical to a station that is publishing nothing.
    # rediss by default: the broker is TLS-only, and a plaintext default would
    # send a station credential in clear the first time someone pasted one in.
    ap.add_argument(
        "--redis",
        default=os.environ.get("PERCEPTA_BROKER_URL", "rediss://localhost:6380/0"),
        help="broker URL; PERCEPTA_BROKER_URL is used if set",
    )
    ap.add_argument(
        "--ca",
        default=os.environ.get("PERCEPTA_CA_FILE"),
        help="CA certificate to verify the broker against; PERCEPTA_CA_FILE if set",
    )
    ap.add_argument(
        "--insecure", action="store_true",
        help="skip certificate verification (development only)",
    )
    ap.add_argument("--legacy", action="store_true",
                    help="listen on internal fan-out channels (platform-side debugging)")
    args = ap.parse_args()

    # TLS: the broker requires it, and the certificate is verified against the
    # platform's CA rather than the system trust store - the same CA a station
    # is given to pin at enrolment. --insecure exists for a developer poking at
    # a stack they built five minutes ago, and says so loudly, because a harness
    # that quietly skips verification teaches people that skipping is normal.
    kwargs = {}
    if args.redis.startswith("rediss://"):
        if args.insecure:
            kwargs["ssl_cert_reqs"] = None
            print("  ! certificate verification DISABLED (--insecure)\n")
        elif args.ca:
            kwargs["ssl_ca_certs"] = args.ca
        else:
            sys.exit(
                "This broker uses TLS. Pass --ca <path to ca.crt> so the "
                "certificate can be verified, or --insecure to skip it."
            )
    r = redis.Redis.from_url(args.redis, **kwargs)
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
        payloads = by_kind.get(kind, [])
        unavailable = [p for p in payloads if p.get("available") is False]
        if unavailable:
            # A station saying "I have no receiver for this" is conformant. The
            # alternative - demanding a payload it cannot honestly fill - is
            # what makes a station invent numbers, which is the one outcome
            # this harness exists to prevent.
            reason = unavailable[-1].get("unavailable_reason") or "no reason given"
            check(f"publishes {kind}", True)
            notes.append(f"{kind} declared unavailable: {reason}")
        else:
            check(f"publishes {kind}", bool(payloads),
                  "nothing received in the window")

    print("\n2. Schema")
    for kind, payloads in sorted(by_kind.items()):
        if kind == "audio":
            errs = sorted(audio_schema.iter_errors(payloads[0]), key=str)
        elif kind in EXPECTED or kind in OPTIONAL_KINDS:
            errs = sorted(telemetry_schema.iter_errors(payloads[0]), key=str)
        else:
            notes.append(f"unknown kind '{kind}' ignored, as the contract allows")
            continue
        check(f"{kind} matches schema", not errs,
              "; ".join(e.message for e in errs[:2]))

    print("\n3. Audio is gated")
    # Tests the rule directly: every audio frame must sit after a radio frame
    # reporting the squelch open. Two earlier versions were weaker. Looking at
    # the last radio frame in the window failed an honest station whose squelch
    # closed before the window ended; skipping whenever the squelch was ever
    # open never failed anyone, but also never tested anything - a station could
    # regress into ungated audio and pass run after run.
    #
    # Walking the stream in order has neither problem. It needs no quiet
    # channel, and every audio frame the station sends is checked.
    if "radio" in unavailable_now(by_kind):
        notes.append("radio unavailable, so audio gating was not tested")
    elif not by_kind.get("radio"):
        check("audio only while squelch is open", False, "no radio telemetry")
    else:
        squelch_open: bool | None = None
        ungated = 0
        unknown = 0
        for payload in seen:
            if payload.get("kind") == "radio":
                squelch_open = bool(
                    payload.get("squelch_open") or payload.get("monitor")
                )
            elif payload.get("kind") == "audio":
                if squelch_open is None:
                    # Audio before any radio frame: nothing to judge it against.
                    unknown += 1
                elif not squelch_open:
                    ungated += 1
        detail = f"{ungated} audio frame(s) sent while the squelch was closed"
        check("audio only while squelch is open", ungated == 0, detail)
        if unknown:
            notes.append(
                f"{unknown} audio frame(s) arrived before any radio telemetry "
                "and could not be judged"
            )

    print("\n4. Commands take effect")
    unavailable_kinds = unavailable_now(by_kind)
    for kind, body, report_kind, field, expect in COMMANDS:
        if report_kind in unavailable_kinds:
            notes.append(f"{kind} not checked - {report_kind} is unavailable")
            continue
        r.publish(cmd_channel, json.dumps({"kind": kind, **body}))
        # Wait for the reported value rather than sampling a fixed window and
        # taking whatever was last. A station that obeys promptly now passes
        # promptly, and one that never obeys still fails - but a slow tick or a
        # burst of audio frames no longer decides the outcome.
        got, ok = await_report(sub, args.station, report_kind, field, expect,
                               COMMAND_TIMEOUT, args.legacy)
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
