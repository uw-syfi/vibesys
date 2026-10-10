"""Split the check's tests across CI runners, balanced by recorded seconds.

`CPU_CHECK_SHARD=I/N` keeps the tests assigned to shard `I` (1-based) of `N`.
Each test starts a candidate server on the tiny model in its own temporary
directory, so shards (and tests) share no state; they are split across runners
rather than run in parallel on one, which would make timing-sensitive servers
compete for the same cores.

Tests are placed longest-first onto the currently lightest shard, using the
seconds per test function in `shard_durations.json`. A function missing from
the record weighs the record's mean, so a new test lands somewhere sensible
until the record is refreshed. Every runner computes the same assignment from
the same inputs, so the shards partition the tests. A stale record only
unbalances the shards; it never drops or duplicates a test.

Refresh the record from a serial `pytest cpu_check --durations=0` run (a
parametrized test's entry is its seconds per case).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

SHARD_ENV = "CPU_CHECK_SHARD"
DURATIONS = Path(__file__).with_name("shard_durations.json")

_SHARD_SPEC = re.compile(r"(\d+)/(\d+)")


def parse_shard(spec: str) -> tuple[int, int]:
    """Parse `I/N` into `(I, N)` with `1 <= I <= N`, naming `CPU_CHECK_SHARD` on error."""
    match = _SHARD_SPEC.fullmatch(spec)
    if match is None or not 1 <= int(match[1]) <= int(match[2]):
        raise ValueError(f"{SHARD_ENV}={spec!r} must look like I/N with 1 <= I <= N")
    return int(match[1]), int(match[2])


def load_durations(path: Path = DURATIONS) -> dict[str, float]:
    """Read the recorded seconds per test function name."""
    return {name: float(seconds) for name, seconds in json.loads(path.read_text()).items()}


def assign(
    tests: Sequence[tuple[str, str]], durations: Mapping[str, float], shards: int
) -> dict[str, int]:
    """Map each `(node id, function name)` to a 0-based shard.

    The result depends only on the set of tests and the record, not on the
    order the tests are given in.
    """
    mean = sum(durations.values()) / len(durations) if durations else 1.0
    ordered = sorted(tests, key=lambda test: (-durations.get(test[1], mean), test[0]))
    loads = [0.0] * shards
    placed: dict[str, int] = {}
    for node_id, function in ordered:
        lightest = min(range(shards), key=lambda shard: (loads[shard], shard))
        loads[lightest] += durations.get(function, mean)
        placed[node_id] = lightest
    return placed
