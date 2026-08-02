#!/usr/bin/env python3
"""Prove the conformance harness is right about stations it should reject.

    python contract/conformance/selftest.py

Runs `check_station.py` against `_selfstation.py` — a minimal station written
from the contract — once conformant and once per deliberate violation, and
asserts the verdict each time.

**Why this exists.** A harness that only ever passes is worse than no harness,
because it converts "untested" into "certified". Every version of this tool so
far has traded a false failure for a false pass while looking fine: one acked
events it never stored, telling real stations to delete undelivered history;
one certified a station that declared all its hardware missing; one failed
correct stations for publishing more slowly than it waited. None of those were
caught by reading. Each case below is one of those, kept as a test so it cannot
come back.

Exit codes under test: 0 conformant, 1 a real fault, 2 inconclusive — nothing
wrong and nothing proven, which must never read as a pass.
"""

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 8137

#: (mode, expected verdict, what the harness must notice). The empty mode is
#: the conformant station: if that ever fails, the harness has become stricter
#: than the contract.
CASES = [
    ("", "PASS",
     "a station that follows the contract"),
    ("fragmented", "PASS",
     "WebSocket fragmentation, which RFC 6455 permits unconditionally and "
     "most client libraries do on large messages"),
    ("slow-weather", "PASS",
     "a metered site publishing weather slowly, which transport.md blesses"),
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
    ("honest-empty", "INCONCLUSIVE",
     "a station with no hardware at all: nothing wrong, nothing tested, and "
     "not a pass"),
    ("lying-cadence", "INCONCLUSIVE|FAIL",
     "a station declaring a cadence longer than any run, opting itself out "
     "of its own telemetry checks"),
]


def verdict(out: str, code: int) -> str:
    if "INCONCLUSIVE" in out:
        return "INCONCLUSIVE"
    if "All checks passed" in out and code == 0:
        return "PASS"
    return "FAIL"


def run(mode: str) -> tuple[str, str]:
    station = [sys.executable, str(HERE / "_selfstation.py"),
               "--port", str(PORT), "--seconds", "200"]
    if mode:
        station += ["--break", mode]
    proc = subprocess.Popen(station, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.4)
        done = subprocess.run(
            [sys.executable, str(HERE / "check_station.py"),
             "--port", str(PORT), "--wait", "25"],
            capture_output=True, text=True, timeout=300)
        return verdict(done.stdout, done.returncode), done.stdout
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(1.0)


def main() -> int:
    print(f"Driving check_station.py against {len(CASES)} stations "
          f"on port {PORT}.\n")
    bad = []
    for mode, expected, why in CASES:
        got, out = run(mode)
        ok = got in expected.split("|")
        print(f"  {'ok  ' if ok else 'BAD '} {mode or 'conformant':<17}"
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

    if bad:
        print(f"{len(bad)} of {len(CASES)} cases wrong.")
        return 1
    print(f"All {len(CASES)} cases correct — the harness passes what it should "
          "and rejects what it should.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
