#!/usr/bin/env python3
"""Measure the per-commit cost of the vs-core run loop on each searched world.

Runs the default scripted world of the dynamic, evolve and issue-queue searches (vs-dst worlds)
to completion with no crash, and reports commits and milliseconds per commit (best of
`--repeat` runs, after one warm-up). Run from the repository root:

    PYTHONPATH=$PWD uv run python scripts/benchmark_core_commit.py [--repeat N] [SUBJECT ...]
"""

from __future__ import annotations

import argparse
import time
from typing import TYPE_CHECKING

from tests.vibesys.dynamic_world import DynamicWorld
from tests.vibesys.evolve_world import EvolveWorld
from tests.vibesys.orchestration.issue_queue.world import IssueQueueWorld

from vs_dst.api import simulate

if TYPE_CHECKING:
    from collections.abc import Callable

WORLDS: dict[str, Callable[[], object]] = {
    "dynamic": DynamicWorld,
    "evolve": EvolveWorld,
    "issue_queue": IssueQueueWorld,
}


def _measure(name: str, repeat: int) -> tuple[int, float]:
    best = float("inf")
    commits = 0
    for attempt in range(repeat + 1):  # attempt 0 warms import-time and value caches
        started = time.perf_counter()
        run = simulate(WORLDS[name]())  # ty: ignore[invalid-argument-type]
        elapsed = time.perf_counter() - started
        commits = run.commits
        if attempt:
            best = min(best, elapsed)
    return commits, best * 1000 / commits


def main() -> None:
    """Print one line per subject."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subjects", nargs="*", default=list(WORLDS))
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    print(f"{'subject':<20}{'commits':>8}{'ms/commit':>12}")
    for name in args.subjects:
        commits, per_commit = _measure(name, args.repeat)
        print(f"{name:<20}{commits:>8}{per_commit:>12.3f}")


if __name__ == "__main__":
    main()
