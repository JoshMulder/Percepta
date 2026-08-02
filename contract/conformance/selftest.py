#!/usr/bin/env python3
"""Prove the conformance harness is right about stations it should reject.

    python contract/conformance/selftest.py
    python contract/conformance/selftest.py --only disconnect

Runs `check_station.py` against `_selfstation.py` — a minimal station written
from the contract — once conformant and once per deliberate violation, and
asserts the verdict each time.

**Why this exists.** A harness that only ever passes is worse than no harness,
because it converts "untested" into "certified". Every version of this tool so
far has traded a false failure for a false pass while looking fine: one acked
events it never stored, telling real stations to delete undelivered history;
one certified a station that declared all its hardware missing; one failed
correct stations for publishing more slowly than it waited; one certified a
station that answered every command and then vanished. None of those were
caught by reading. Each case below is one of those, kept as a test so it cannot
come back.

**A case has to be able to fail in the direction it names.** Two here could
not, and both looked fine for a round: `lying-cadence` also silenced the
floodlight, so it exited on the light checks and scored green with the entire
skipped-streams branch deleted; `slow-weather` published at t=0, so the
declared cadence was never consulted and the case could only pass. A case that
cannot fail is a comment with a runtime.

Exit codes under test: 0 conformant, 1 a real fault, 2 inconclusive — nothing
wrong and nothing proven, which must never read as a pass.
"""

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMAS = HERE.parent / "schemas"

#: What the schemas and the harness must all agree they are.
CONTRACT_VERSION = "2.0"


def check_schemas() -> list[str]:
    """Invariants the schemas cannot express about themselves.

    These guard the thing `check_station.py` reads, which the station cases
    below do not: they prove the *tool* is right about a station, and a bound
    that quietly stopped existing would leave every one of them green. All
    three are free to run and one of them has already failed in the field —
    `audio_lease_remaining_s` was added to `radio` and not to its
    declared-unavailable list, so a station could report no receiver and a
    live audio lease in the same frame and validate. Adding a field is the
    cheapest, most-encouraged change there is under this contract's version
    rules, and it is exactly the change that breaks this.
    """
    problems: list[str] = []
    loaded = {}
    for path in sorted(SCHEMAS.glob("*.schema.json")):
        loaded[path.name] = json.loads(path.read_text(encoding="utf-8"))

    # 1. One version, stated the same way everywhere. A previous round shipped
    #    a schema whose $id and contractVersion disagreed.
    for name, schema in loaded.items():
        stated = schema.get("contractVersion")
        in_id = schema.get("$id", "")
        if stated != CONTRACT_VERSION:
            problems.append(f"{name}: contractVersion is {stated!r}, "
                            f"expected {CONTRACT_VERSION!r}")
        if f"v{CONTRACT_VERSION}/" not in in_id:
            problems.append(f"{name}: $id {in_id!r} does not carry "
                            f"v{CONTRACT_VERSION}")

    telemetry = loaded.get("telemetry.schema.json", {})
    defs = telemetry.get("$defs", {})

    # 2. Every branch of the closed oneOf is reachable. A new branch written
    #    without a `kind` const matches every payload, so each one then
    #    matches two branches and the whole schema rejects everything that was
    #    previously valid — invisibly to whoever added it, because their own
    #    new kind validates fine.
    kinds: dict[str, str] = {}
    for branch in telemetry.get("oneOf", []):
        ref = branch.get("$ref", "")
        name = ref.rsplit("/", 1)[-1]
        body = defs.get(name, {})
        const = body.get("properties", {}).get("kind", {}).get("const")
        if const is None:
            problems.append(f"telemetry oneOf branch {name!r} has no kind const")
        elif const in kinds:
            problems.append(f"telemetry kind {const!r} is claimed by both "
                            f"{kinds[const]!r} and {name!r}")
        else:
            kinds[const] = name

    # 3. A stream that declares itself unavailable may report none of its own
    #    fields. The `then` branch has to name every one of them, and nothing
    #    but the schema author keeps the two lists in step.
    for name, body in defs.items():
        clauses = [c for c in body.get("allOf", []) if "then" in c and "if" in c]
        if not clauses:
            continue
        declared = set(body.get("properties", {})) - {"kind"}
        excluded = set(clauses[0]["then"].get("properties", {}))
        missing = declared - excluded
        if missing:
            problems.append(
                f"{name}: {sorted(missing)} can be reported while the stream "
                "is declared unavailable — a station could claim no hardware "
                "and a live reading in the same frame")
    return problems

#: (mode, expected verdict, what the harness must notice). The empty mode is
#: the conformant station: if that ever fails, the harness has become stricter
#: than the contract.
CASES = [
    ("", "PASS",
     "a station that follows the contract"),
    ("fragmented", "PASS",
     "WebSocket fragmentation, which RFC 6455 permits unconditionally and "
     "most client libraries do on large messages"),
    ("fragmented-ping", "PASS",
     "a ping arriving between two fragments of one message, which RFC 6455 "
     "§5.4 permits and any station with a keepalive eventually does. The "
     "accumulated fragments must survive it: discarding them left the next "
     "continuation frame with nothing to continue, desyncing the stream for "
     "the rest of the run and failing a conformant station on every check"),
    ("slow-weather", "PASS",
     "a metered site publishing weather slowly, which transport.md blesses — "
     "the window must stretch to the cadence the station declared"),
    ("fixed-squelch", "PASS",
     "a site running a hand-set squelch threshold, which must be put back: "
     "the command list ends on AUTO and would otherwise discard it"),
    ("nan", "FAIL",
     "NaN, which passes every numeric bound in the schemas because "
     "comparisons against it are false"),
    ("ungated-audio", "FAIL",
     "audio sent with no live lease — the expensive rule, and the one the "
     "reference implementation once got wrong"),
    ("no-report", "FAIL",
     "a command obeyed but never reported, which is indistinguishable from "
     "one ignored"),
    ("extra-key", "FAIL",
     "a legacy `topic` key in the envelope, i.e. a station still on the "
     "pre-2.0 wire format"),
    ("bad-stream", "FAIL",
     "an unknown stream code"),
    ("bad-stream-type", "FAIL",
     "a non-string stream, which used to raise TypeError and take the run "
     "down with it"),
    ("all-unavailable", "FAIL",
     "a blank unavailable_reason — the shape of pretending"),
    ("oversize", "FAIL",
     "a frame over 512 KiB, which the relay answers by closing 1009 — and "
     "which used to traceback on the next read, skipping the restore block "
     "and leaving a real receiver mistuned with no warning printed"),
    ("no-pong", "FAIL",
     "a station that never answers a ping, so neither end could ever notice "
     "a half-open socket"),
    ("transient-unavailable", "FAIL",
     "one `available: false` frame from a tuner warming up, then a working "
     "radio. Latching on it skipped every radio command and both audio gates "
     "while still exiting 0; this station also withholds a report, so a "
     "harness that skips the radio certifies it"),
    ("quiet-band", "INCONCLUSIVE",
     "a station whose squelch never opens: correct behaviour on a quiet "
     "channel, and no evidence at all about the squelch gate or the audio "
     "lease. Found by running the real agent against this harness — three "
     "runs reported no audio and exited 0, against a station that was "
     "publishing it perfectly well on a longer window"),
    ("honest-empty", "INCONCLUSIVE",
     "a station with no hardware at all: nothing wrong, nothing tested, and "
     "not a pass"),
    ("lying-cadence", "INCONCLUSIVE",
     "a station declaring one stream on an 86400 s cadence, opting itself "
     "out of that stream's checks while everything else works"),
    ("disconnect", "INCONCLUSIVE",
     "a station that answers all nine commands and then vanishes: every "
     "later check passes on the evidence of a dead socket, and the run used "
     "to certify a site in the same breath as noting it was left mistuned"),
]


def verdict(out: str, code: int) -> str:
    if "INCONCLUSIVE" in out:
        return "INCONCLUSIVE"
    if "All checks passed" in out and code == 0:
        return "PASS"
    return "FAIL"


def free_port() -> int:
    """A port nothing is using, chosen per case.

    Hardcoding one meant two people could not run this at once, and a port
    left in TIME_WAIT by the previous case could fail the next one for a
    reason that had nothing to do with the station.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def await_listening(port: int, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def run(mode: str) -> tuple[str, str]:
    """One case: start the harness, wait for its port, then start the station.

    In that order, and with the wait. The harness is the *server* — it binds
    and the station dials in — so spawning the station first raced the two:
    the station's connect went out about 0.8 s before the port existed, and
    only Windows loopback retransmitting the SYN made it work at all. Where
    connect refuses immediately the station exits, every case reports "no
    station connected", and eight of them still score green against a suite
    that is proving nothing.
    """
    port = free_port()
    checker = subprocess.Popen(
        [sys.executable, str(HERE / "check_station.py"),
         "--port", str(port), "--wait", "25"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    station = None
    try:
        if not await_listening(port, 15.0):
            checker.kill()
            return "FAIL", f"harness never listened on port {port}\n"
        cmd = [sys.executable, str(HERE / "_selfstation.py"),
               "--port", str(port), "--seconds", "200"]
        if mode:
            cmd += ["--break", mode]
        station = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        out, _ = checker.communicate(timeout=300)
        return verdict(out, checker.returncode), out
    except subprocess.TimeoutExpired:
        checker.kill()
        return "FAIL", "harness did not finish within 300 s\n"
    finally:
        for proc in (station, checker):
            if proc is None or proc.poll() is not None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", metavar="MODE",
                    help="run one case (use 'conformant' for the clean one)")
    args = ap.parse_args()

    cases = CASES
    if args.only:
        wanted = "" if args.only == "conformant" else args.only
        cases = [c for c in CASES if c[0] == wanted]
        if not cases:
            sys.exit(f"no such case: {args.only}")

    schema_problems = check_schemas()
    print(f"Schema invariants: "
          f"{'all hold' if not schema_problems else 'FAILED'}")
    for problem in schema_problems:
        print(f"  BAD  {problem}")
    print()

    print(f"Driving check_station.py against {len(cases)} stations.\n")
    bad = []
    for mode, expected, why in cases:
        got, out = run(mode)
        ok = got in expected.split("|")
        print(f"  {'ok  ' if ok else 'BAD '} {mode or 'conformant':<22}"
              f"{got:<14} expected {expected}")
        if not ok:
            bad.append((mode or "conformant", expected, got, why, out))

    print()
    for mode, expected, got, why, out in bad:
        print(f"--- {mode}: expected {expected}, got {got}")
        print(f"    the harness must notice {why}")
        for line in out.splitlines():
            if line.strip().startswith(("FAIL", "SKIP", "INCONCLUSIVE")):
                print(f"    {line.strip()}")
        print()

    if bad or schema_problems:
        if bad:
            print(f"{len(bad)} of {len(cases)} cases wrong.")
        if schema_problems:
            print(f"{len(schema_problems)} schema invariant(s) broken.")
        return 1
    print(f"All {len(cases)} cases correct — the harness passes what it should "
          "and rejects what it should, and the schemas hold their invariants.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
