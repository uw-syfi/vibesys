#!/usr/bin/env python3
"""Measure the per-commit cost of the vs-core run loop on each conformance subject.

Runs the smallest scripted world of every subject in `tests/composition/_loop/subjects.py`
to completion with no crash, and reports commits and milliseconds per commit (best of
`--repeat` runs, after one warm-up). Run from the repository root:

    PYTHONPATH=$PWD uv run python scripts/benchmark_core_commit.py [--repeat N] [SUBJECT ...]
"""

from __future__ import annotations

import argparse
import time

from tests.composition._loop.dst_world import SubjectWorld
from tests.composition._loop.subjects import SUBJECTS

from vs_dst.api import simulate


def _measure(name: str, repeat: int) -> tuple[int, float]:
    subject = SUBJECTS[name]
    best = float("inf")
    commits = 0
    for attempt in range(repeat + 1):  # attempt 0 warms import-time and value caches
        started = time.perf_counter()
        run = simulate(SubjectWorld(subject, subject.smallest_world()))
        elapsed = time.perf_counter() - started
        commits = run.commits
        if attempt:
            best = min(best, elapsed)
    return commits, best * 1000 / commits


def main() -> None:
    """Print one line per subject."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subjects", nargs="*", default=list(SUBJECTS))
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    print(f"{'subject':<20}{'commits':>8}{'ms/commit':>12}")
    for name in args.subjects:
        commits, per_commit = _measure(name, args.repeat)
        print(f"{name:<20}{commits:>8}{per_commit:>12.3f}")


if __name__ == "__main__":
    main()
