#!/usr/bin/env python3
"""Word error rate over a hand-labelled corpus of airband overs.

    python3 tools/score_overs.py ~/overs

Reads the sidecars written by `gsu/radio/corpus.py`, ignores the ones nobody has
labelled yet, and prints a WER — overall, per frequency, and per duration bucket.

WHY PER FREQUENCY AND PER DURATION, not just one number. At Timaru the two
frequencies fail for different reasons: the CTAF at 119.500 is close, strong and
mangles PLACE NAMES, while 129.300 is a controller 150 km away and fails on
signal. A single number averages a vocabulary problem with a radio problem and
moves for reasons nobody can attribute. The duration split matters for the same
reason: short overs hallucinate, long ones do not, and a change that helps one
can hurt the other invisibly.

NORMALISATION IS DELIBERATELY MILD. Case, punctuation and stray whitespace go;
nothing else. It is tempting to also fold "one one" into "11", or strip filler —
but every such rule is a judgement about what counts as correct, and once they
accumulate the score measures the normaliser rather than the model. If digits
need their own treatment, score them separately rather than folding them in.

No dependencies: this runs on the box, or on a laptop, with nothing installed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_PUNCT = re.compile(r"[^\w\s]")


def normalise(text: str) -> list[str]:
    return _PUNCT.sub(" ", (text or "").lower()).split()


def distance(reference: list[str], hypothesis: list[str]) -> int:
    """Levenshtein over words — the edits to turn one into the other.

    Iterative with two rows: a long over is a few dozen words, but this runs
    over a whole corpus and the full matrix buys nothing.
    """
    if not reference:
        return len(hypothesis)
    previous = list(range(len(reference) + 1))
    for j, h in enumerate(hypothesis, 1):
        current = [j]
        for i, r in enumerate(reference, 1):
            current.append(
                previous[i - 1] if r == h
                else 1 + min(previous[i - 1], previous[i], current[i - 1])
            )
        previous = current
    return previous[-1]


def bucket(seconds: float) -> str:
    if seconds < 2:
        return "1s"
    if seconds < 4:
        return "2-3s"
    if seconds < 7:
        return "4-6s"
    return "7s+"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    directory = Path(argv[1])

    groups: dict[str, list[tuple[int, int]]] = {}
    labelled = unlabelled = 0

    for sidecar in sorted(directory.glob("over-*.json")):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        truth = data.get("truth")
        if truth is None:
            unlabelled += 1
            continue
        labelled += 1
        reference = normalise(truth)
        hypothesis = normalise(data.get("machine", ""))
        errors = distance(reference, hypothesis)
        # An over where nothing was said and nothing was transcribed is a
        # correct answer, not a divide-by-zero. One where nothing was said and
        # something WAS transcribed is a hallucination, and every word of it is
        # an error — which is the behaviour that matters most here.
        for key in ("all", f"{data.get('frequency_hz', 0) / 1e6:.3f} MHz",
                    bucket(float(data.get("duration_s") or 0))):
            groups.setdefault(key, []).append((errors, len(reference)))

    if not labelled:
        print(f"No labelled sidecars in {directory}.")
        print(f"{unlabelled} waiting: set \"truth\" in each .json to what was said.")
        return 1

    print(f"{labelled} labelled, {unlabelled} still unlabelled\n")
    width = max(len(k) for k in groups)
    for key in ["all"] + sorted(k for k in groups if k != "all"):
        pairs = groups[key]
        errors = sum(e for e, _ in pairs)
        words = sum(w for _, w in pairs)
        # Guarded: a bucket of pure hallucination has no reference words at all,
        # and reporting that as 0% would be exactly backwards.
        rate = f"{100.0 * errors / words:5.1f}%" if words else "  n/a"
        print(f"  {key:<{width}}  WER {rate}   ({len(pairs):>3} overs, {words:>4} words)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
