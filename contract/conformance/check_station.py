#!/usr/bin/env python3
"""Check a running station against the contract.

    python contract/conformance/check_station.py --station <uuid>

Subscribes to what the station publishes, validates every message against the
schemas, and reports what is missing or wrong. Then issues each command and
checks the station reports the effect back — because a command that is accepted
and quietly ignored is the failure this platform is least able to notice.

Neutral about implementation: it talks to the broker, not to anyone's code. The
simulator passes it, and so must real hardware.
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

#: Every telemetry kind a station is expected to produce, and the default
#: seconds between frames from transport.md's cadence table. These are only the
#: defaults: a station reporting `health.cadence` is believed instead, because
#: the contract says a site may legitimately slow a stream down to save
#: bandwidth and must not be failed for it.
DEFAULT_CADENCE = {"adsb": 1.0, "power": 1.0, "radio": 1.0, "light": 1.0,
                   "weather": 5.0}

#: How many periods to wait before calling a stream absent, and the floor for a
#: fast one. Generous: a station on a slow tick is not a broken station.
PERIODS = 4
MIN_WAIT = 8.0

#: Longest this will listen for the opening survey, whatever the cadences say.
#: A station reporting a very slow weather period would otherwise stall the run.
MAX_WAIT = 45.0

#: Kinds the contract defines but does not require a station to send. Validated
#: when present - a station that reports health must report it correctly - and
#: never demanded, because a station is not less conformant for staying quiet
#: about itself.
OPTIONAL_KINDS = {"health"}

#: How long a command has to be reflected in telemetry.
COMMAND_TIMEOUT = 8.0

#: Each command, and the telemetry field that must reflect it. This pairing is
#: the contract's core promise: nothing is confirmed by the platform, so every
#: command has to be observable in what the station reports.
#:
#: Commands against a stream the station has declared unavailable are skipped -
#: there is no hardware to obey them, and failing a station for that would mean
#: the only way to pass is to pretend.
#:
#: Each pair that changes state is followed by one that puts it back, because
#: this runs against real commissioned hardware. `radio.gain` and `radio.ppm`
#: are deliberately absent for the same reason: they are calibration settings
#: trimmed once for a site, and a harness that left one changed would desense a
#: receiver in a way nobody would connect to having run a test. Tuning is the
#: one exception, and it is restored below from what the station first reported.
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


def read(pubsub, timeout: float) -> dict | None:
    """One decoded payload, or None if nothing arrived in time."""
    message = pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
    if not message:
        return None
    data = message.get("data")
    if isinstance(data, bytes):
        data = data.decode()
    try:
        frame = json.loads(data)
    except (TypeError, ValueError):
        return None
    return frame if isinstance(frame, dict) else None


def collect(pubsub, seconds: float) -> list[dict]:
    """Everything the station published in the window, in order.

    Order matters for the audio-gating check: it pairs each audio frame with
    the squelch state the station last reported, so the frames have to arrive
    in the sequence they were sent.
    """
    out: list[dict] = []
    end = time.time() + seconds
    while time.time() < end:
        frame = read(pubsub, 1.0)
        if frame is not None:
            out.append(frame)
    return out


def cadence_from(by_kind: dict[str, list[dict]]) -> dict[str, float]:
    """What this station says its cadences are, falling back to the defaults.

    `health.cadence` is authoritative per transport.md: a console deriving a
    staleness timeout must use it rather than the table, and so must this.
    """
    cadence = dict(DEFAULT_CADENCE)
    for frame in by_kind.get("health", []):
        reported = frame.get("cadence")
        if not isinstance(reported, dict):
            continue
        for kind, period in reported.items():
            if isinstance(period, (int, float)) and not isinstance(period, bool):
                if period > 0:
                    cadence[str(kind)] = float(period)
    return cadence


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


def await_report(pubsub, kind: str, field: str, expect, seconds: float):
    """Wait until the station reports `field` as `expect`, or time out.

    Returns the last value seen and whether it matched. Polling for the answer
    rather than sampling a window and taking the final value is what makes this
    check deterministic: the old version could miss a correct report simply
    because the window ended a moment too early.
    """
    got = None
    end = time.time() + seconds
    while time.time() < end:
        frame = read(pubsub, 0.5)
        if frame is None or frame.get("kind") != kind or field not in frame:
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
    sub.subscribe(f"gsu/{args.station}/telemetry", f"gsu/{args.station}/audio")
    cmd_channel = f"cmd/gsu/{args.station}"

    print(f"\nListening to station {args.station}\n")

    # Ask for audio before listening. A station that obeys the contract sends
    # none at all unless somebody has asked - so without this, the gating check
    # below would have nothing to judge and would silently pass every
    # lease-respecting station while only ever testing the ones that ignore it.
    # Renewed through the survey, exactly as the platform does it.
    audio_request = {"kind": "radio.audio", "on": True, "lease_seconds": 30}
    r.publish(cmd_channel, json.dumps(audio_request))

    print("1. Telemetry")
    # One pass at the defaults, then extend if the station reports slower
    # cadences of its own. Two short listens rather than one long one, so a
    # station running at the defaults is not made to wait for the worst case.
    seen = collect(sub, MIN_WAIT)
    by_kind: dict[str, list[dict]] = {}
    for payload in seen:
        by_kind.setdefault(str(payload.get("kind")), []).append(payload)

    cadence = cadence_from(by_kind)
    if cadence != DEFAULT_CADENCE:
        stated = ", ".join(
            f"{k} {v:g}s" for k, v in sorted(cadence.items())
            if DEFAULT_CADENCE.get(k) != v
        )
        notes.append(f"station reports its own cadence: {stated}")
    missing = [k for k in cadence if not by_kind.get(k)]
    if missing:
        extra = min(MAX_WAIT, max(cadence[k] * PERIODS for k in missing)) - MIN_WAIT
        if extra > 0:
            r.publish(cmd_channel, json.dumps(audio_request))
            for payload in collect(sub, extra):
                seen.append(payload)
                by_kind.setdefault(str(payload.get("kind")), []).append(payload)

    for kind in cadence:
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
                  f"nothing received in {cadence[kind] * PERIODS:g}s")

    print("\n2. Schema")
    # Every frame, not the first of each kind. A station that alternates valid
    # and invalid payloads used to pass this outright.
    for kind, payloads in sorted(by_kind.items()):
        if kind == "audio":
            schema = audio_schema
        elif kind in cadence or kind in OPTIONAL_KINDS:
            schema = telemetry_schema
        else:
            notes.append(f"unknown kind '{kind}' ignored, as the contract allows")
            continue
        first: list = []
        bad = 0
        for payload in payloads:
            errs = sorted(schema.iter_errors(payload), key=str)
            if errs:
                bad += 1
                first = first or errs
        detail = "; ".join(e.message for e in first[:2])
        if bad:
            detail += f"  ({bad} of {len(payloads)} frame(s))"
        check(f"{kind} matches schema ({len(payloads)} frame(s))", not bad, detail)

    print("\n3. Audio is gated")
    # Tests the rule directly: every audio frame must sit after a radio frame
    # reporting the squelch open. Earlier versions were weaker - looking only at
    # the last radio frame in the window failed an honest station whose squelch
    # closed before the window ended, and skipping whenever the squelch was ever
    # open never failed anyone.
    #
    # The tolerance below matters for real hardware. A station may publish audio
    # on a faster sub-tick than its 1 Hz radio telemetry, so a transmission
    # starting mid-interval legitimately puts audio on the wire before the radio
    # frame that announces the open gate. Frames are therefore judged against
    # the squelch state either side of them, and only audio that is surrounded
    # by a closed gate is a failure.
    if "radio" in unavailable_now(by_kind):
        notes.append("radio unavailable, so audio gating was not tested")
    elif not by_kind.get("radio"):
        check("audio only while squelch is open", False, "no radio telemetry")
    elif not by_kind.get("audio"):
        # Silence is conformant - the band may simply have been quiet - but it
        # means this check proved nothing, and saying so is the point.
        check("audio only while squelch is open", True)
        notes.append(
            "no audio in the window, so gating was not exercised; run again "
            "on a busy channel to test it"
        )
    else:
        def open_at(index: int, step: int) -> bool | None:
            """The nearest reported squelch state in one direction."""
            i = index + step
            while 0 <= i < len(seen):
                if seen[i].get("kind") == "radio":
                    return bool(seen[i].get("squelch_open") or seen[i].get("monitor"))
                i += step
            return None

        ungated = 0
        for index, payload in enumerate(seen):
            if payload.get("kind") != "audio":
                continue
            before, after = open_at(index, -1), open_at(index, 1)
            if before is False and after is not True:
                ungated += 1
        check("audio only while squelch is open", ungated == 0,
              f"{ungated} audio frame(s) sent with the squelch closed either side")

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
        got, ok = await_report(sub, report_kind, field, expect, COMMAND_TIMEOUT)
        check(f"{kind} -> {report_kind}.{field}", ok, f"reported {got!r}")

    # Put the receiver back where it was found. This runs against commissioned
    # hardware on a real site, and a station left listening to whatever
    # frequency a test chose is a station that stopped doing its job quietly.
    was = next((p.get("freq_hz") for p in by_kind.get("radio", [])
                if p.get("freq_hz")), None)
    if was and "radio" not in unavailable_kinds:
        r.publish(cmd_channel, json.dumps({"kind": "radio.tune", "freq_hz": was}))
        _, back = await_report(sub, "radio", "freq_hz", was, COMMAND_TIMEOUT)
        notes.append(
            f"receiver returned to {was / 1e6:.3f} MHz" if back else
            f"COULD NOT return the receiver to {was / 1e6:.3f} MHz - check it"
        )

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
