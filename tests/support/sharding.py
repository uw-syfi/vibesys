"""Split the suite across CI runners by test file, balanced by recorded durations.

``--shard=I/N`` keeps only the test files assigned to shard ``I`` (1-based) of
``N``. Files are assigned longest-first to the currently lightest shard, using
the per-file seconds in ``shard_durations.json``. A file missing from that
record is weighted at the mean of the recorded files, so a new test file lands
somewhere sensible until the record is refreshed.

Every worker and every shard computes the same assignment from the same inputs,
so the shards partition the suite: each test runs in exactly one shard.

Refresh the record from a full run with ``--record-shard-durations=PATH``
(then copy the file over ``shard_durations.json``). A stale record only
unbalances the shards; it never drops or duplicates a test.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    import pytest

DEFAULT_DURATIONS = Path(__file__).with_name("shard_durations.json")


def parse_shard(spec: str) -> tuple[int, int]:
    """Parse ``I/N`` into a 1-based shard index and the shard count."""
    index_text, separator, count_text = spec.partition("/")
    if not separator or not index_text.isdecimal() or not count_text.isdecimal():
        message = f"--shard must look like I/N with 1 <= I <= N, got {spec!r}"
        raise ValueError(message)
    index, count = int(index_text), int(count_text)
    if not 1 <= index <= count:
        message = f"--shard must look like I/N with 1 <= I <= N, got {spec!r}"
        raise ValueError(message)
    return index, count


def assign_shards(
    files: Iterable[str], durations: Mapping[str, float], count: int
) -> dict[str, int]:
    """Map each distinct file to a 1-based shard, longest first onto the lightest shard."""
    unique = sorted(set(files))
    known = [durations[name] for name in unique if name in durations]
    fallback = sum(known) / len(known) if known else 1.0
    weights = {name: durations.get(name, fallback) for name in unique}
    loads = [0.0] * count
    assignment: dict[str, int] = {}
    for name in sorted(unique, key=lambda name: (-weights[name], name)):
        shard = min(range(count), key=lambda candidate: (loads[candidate], candidate))
        loads[shard] += weights[name]
        assignment[name] = shard + 1
    return assignment


def _file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the sharding options."""
    group = parser.getgroup("shard", "split the suite across CI runners")
    group.addoption("--shard", default=None, metavar="I/N", help="run only shard I of N")
    group.addoption(
        "--shard-durations",
        default=str(DEFAULT_DURATIONS),
        help="JSON map of test file to seconds used to balance shards",
    )
    group.addoption(
        "--record-shard-durations",
        default=None,
        metavar="PATH",
        help="write the measured seconds per test file to PATH after the run",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect every test outside the requested shard."""
    spec = config.getoption("--shard")
    if spec is None:
        return
    index, count = parse_shard(spec)
    durations = json.loads(Path(config.getoption("--shard-durations")).read_text())
    assignment = assign_shards((_file_of(item.nodeid) for item in items), durations, count)
    kept = [item for item in items if assignment[_file_of(item.nodeid)] == index]
    dropped = [item for item in items if assignment[_file_of(item.nodeid)] != index]
    config.hook.pytest_deselected(items=dropped)
    items[:] = kept


_measured: dict[str, float] = defaultdict(float)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Accumulate setup, call, and teardown seconds per file."""
    _measured[_file_of(report.nodeid)] += report.duration


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Write the measured durations from the controller process, not xdist workers."""
    path = session.config.getoption("--record-shard-durations")
    if path is None or hasattr(session.config, "workerinput"):
        return
    rounded = {name: round(seconds, 2) for name, seconds in sorted(_measured.items())}
    Path(path).write_text(json.dumps(rounded, indent=1) + "\n", encoding="utf-8")
