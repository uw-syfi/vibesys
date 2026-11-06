"""Write or check ``rand.json``: the draws of ``vibesys_evaluator.Rand`` the Go SDK must equal.

The Python ``Rand`` is the reference; ``go/workload``'s ``TestRandIsThePythonRand``
replays every case with ``workload.Rand`` and compares bit for bit (floats as their IEEE 754
bits). ``python fixtures/rand_streams.py`` rewrites the file after a deliberate change of a stream
(a breaking change, ``AGENTS.md``); ``--check`` fails when the file is not what ``Rand`` draws.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path
from typing import Any

from vibesys_evaluator import Rand

FIXTURE = Path(__file__).with_name("rand.json")
_LABELS: list[list[str | int]] = [
    [],
    ["user", 3],
    ["phase", 0],
    ["phase", 7, "arrivals"],
    ["", 0],
    ["a", "b", 1 << 40, 5],
    [(1 << 64) - 1],
]
_SEEDS = [0, 7, 42, 407708418465, (1 << 64) - 1]
_INT_WEIGHTS = [[90.0, 10.0], [30.0, 24.0, 16.0, 11.0, 8.0, 6.0, 5.0], [0.0, 3.0, 0.0, 1.0]]
_FLOAT_WEIGHTS = [[0.1, 0.2, 0.7], [1.5, 0.0, 2.25], [1e-3, 1e3, 0.5, 0.25]]
_ZIPF = [(1, 0.99), (20, 1.0), (1000, 0.75), (1000, 0.5), (100_000, 0.99), (5000, 1.2), (300, 0.0), (64, 2.5)]


def _bits(value: float) -> str:
    return f"{struct.unpack('<Q', struct.pack('<d', value))[0]:016x}"


def _draws(rand: Rand) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"op": "uint64", "want": rand.below(1 << 64)}]
    out += [{"op": "below", "n": n, "want": rand.below(n)} for n in (1, 1000, 1 << 40, (1 << 63) - 1)]
    out += [{"op": "random", "want": _bits(rand.random())} for _ in range(3)]
    out += [{"op": "normal", "want": _bits(rand.normal())} for _ in range(2)]
    out += [{"op": "weighted", "weights": w, "want": rand.weighted(w)} for w in _INT_WEIGHTS + _FLOAT_WEIGHTS]
    out += [{"op": "zipf", "n": n, "s": s, "want": rand.zipf(n, s)} for n, s in _ZIPF for _ in range(3)]
    child = rand.child("child", 9)
    out.append({"op": "child", "labels": ["child", 9], "want": child.below(1 << 64)})
    return out


def cases() -> list[dict[str, Any]]:
    """Every case: a stream's seed and labels, and its draws in order."""
    return [
        {"seed": seed, "labels": labels, "draws": _draws(Rand(seed, *labels))}
        for seed in _SEEDS
        for labels in _LABELS
    ]


def text() -> str:
    """The fixture's contents."""
    about = "Draws of vibesys_evaluator.Rand (written by fixtures/rand_streams.py); floats are IEEE 754 bits."
    lines = ",\n".join(json.dumps(case, separators=(",", ":")) for case in cases())
    return f'{{"about":{json.dumps(about)},"cases":[\n{lines}\n]}}\n'


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        sys.exit(0 if FIXTURE.read_text() == text() else "rand.json is not what Rand draws")
    FIXTURE.write_text(text())
