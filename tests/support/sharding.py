"""Split the suite across CI runners by test file, balanced by recorded durations.

``--shard=I/N`` (or ``VIBESYS_TEST_SHARD=I/N``, which lets one CI check group
serve every shard) keeps only the test files assigned to shard ``I`` (1-based) of
``N``. Files are assigned longest-first to the currently lightest shard, using
the per-file seconds in ``shard_durations.json``. A file missing from that
record is weighted at the mean of the recorded files, so a new test file lands
somewhere sensible until the record is refreshed.

A file whose recorded seconds exceed ``HEAVY_SECONDS`` is too big to place as one
unit (one crash-sweep file alone can outlast a whole shard's budget). Its tests
are spread over the shards instead, each weighted at an even share of the file's
seconds, and are placed after every whole file so they fill the gaps.

Files that belong to other shards are skipped before import (``pytest_ignore_collect``),
because importing the whole suite in every shard cost minutes of each CI job.

Every worker and every shard computes the same assignment from the same inputs,
so the shards partition the suite: each test runs in exactly one shard.

Every CI shard records its measured seconds (``--record-shard-durations=PATH``,
or ``$VIBESYS_RECORD_SHARD_DURATIONS``) and uploads them as a
``shard-durations-I`` artifact; ``scripts/refresh_shard_durations.py`` merges a
run's artifacts into ``shard_durations.json``. Each shard also warns when it
overran ``SHARD_BUDGET_SECONDS`` or ran files the record underestimates. A
stale record only unbalances the shards; it never drops or duplicates a test.
"""

from __future__ import annotations

import fnmatch
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from _pytest.terminal import TerminalReporter
    from xdist.workermanage import WorkerController

DEFAULT_DURATIONS = Path(__file__).with_name("shard_durations.json")

#: Recorded seconds above which a file's tests are spread over the shards.
HEAVY_SECONDS = 90.0
#: xdist workers per CI shard (a standard runner has two cores).
WORKERS_PER_SHARD = 2
#: Wall seconds one shard's tests may take; above this the shards need rebalancing
#: or more of them. Leaves room under ten minutes for runner setup.
SHARD_BUDGET_SECONDS = 400.0
#: Granularity used to project the even spread of heavy files.
_PROJECTION_UNITS = 200


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


def is_heavy(name: str, durations: Mapping[str, float]) -> bool:
    """Whether the file's tests are spread over shards instead of placed as one unit."""
    return durations.get(name, 0.0) > HEAVY_SECONDS


def shard_loads(
    files: Iterable[str], durations: Mapping[str, float], count: int
) -> tuple[dict[str, int], list[float]]:
    """Place each whole file longest-first on the lightest shard.

    Heavy files are left out: ``assign_heavy_items`` places their tests. Return
    the assignment and the seconds on each shard so far.
    """
    unique = sorted({name for name in files if not is_heavy(name, durations)})
    known = [durations[name] for name in unique if name in durations]
    fallback = sum(known) / len(known) if known else 1.0
    weights = {name: durations.get(name, fallback) for name in unique}
    loads = [0.0] * count
    assignment: dict[str, int] = {}
    for name in sorted(unique, key=lambda name: (-weights[name], name)):
        shard = min(range(count), key=lambda candidate: (loads[candidate], candidate))
        loads[shard] += weights[name]
        assignment[name] = shard + 1
    return assignment, loads


def assign_shards(
    files: Iterable[str], durations: Mapping[str, float], count: int
) -> dict[str, int]:
    """Map each whole (not heavy) file to a 1-based shard, longest first onto the lightest."""
    return shard_loads(files, durations, count)[0]


def assign_heavy_items(
    items_per_file: Mapping[str, int],
    durations: Mapping[str, float],
    loads: Iterable[float],
) -> tuple[dict[tuple[str, int], int], list[float]]:
    """Map each (heavy file, test index within the file) to a 1-based shard.

    Every test weighs an even share of its file's seconds and goes, heaviest
    first, to the currently lightest shard starting from ``loads``. Return the
    assignment and the final loads.
    """
    final = list(loads)
    units = [
        (durations[name] / total, name, index)
        for name, total in items_per_file.items()
        if is_heavy(name, durations)
        for index in range(total)
    ]
    assignment: dict[tuple[str, int], int] = {}
    for weight, name, index in sorted(units, key=lambda unit: (-unit[0], unit[1], unit[2])):
        shard = min(range(len(final)), key=lambda candidate: (final[candidate], candidate))
        final[shard] += weight
        assignment[name, index] = shard + 1
    return assignment, final


def projected_shard_seconds(
    files: Iterable[str], durations: Mapping[str, float], count: int
) -> list[float]:
    """Projected wall seconds of each shard's tests, assuming ``WORKERS_PER_SHARD`` workers."""
    unique = sorted(set(files))
    _, loads = shard_loads(unique, durations, count)
    _, loads = assign_heavy_items(
        {name: _PROJECTION_UNITS for name in unique if is_heavy(name, durations)},
        durations,
        loads,
    )
    return [load / WORKERS_PER_SHARD for load in loads]


def discover_test_files(root: Path, testpaths: Iterable[str], patterns: Iterable[str]) -> list[str]:
    """List the test files pytest would collect under ``testpaths``, relative to ``root``.

    Lets a shard skip the files it does not own without importing them. It may
    list a few files pytest would not collect; every shard lists the same ones,
    so the assignment stays a partition.
    """
    skipped = ("__pycache__", ".*", "build", "dist", "node_modules", "venv", "*.egg")
    found: set[str] = set()
    for testpath in testpaths:
        for pattern in patterns:
            for path in (root / testpath).rglob(pattern):
                relative = path.relative_to(root)
                if any(
                    fnmatch.fnmatch(part, skip) for part in relative.parts[:-1] for skip in skipped
                ):
                    continue
                found.add(relative.as_posix())
    return sorted(found)


def merge_durations(records: Iterable[Mapping[str, float]]) -> dict[str, float]:
    """Sum per-file seconds across shards (a heavy file's tests run in several)."""
    total: dict[str, float] = defaultdict(float)
    for record in records:
        for name, seconds in record.items():
            total[name] += seconds
    return {name: round(seconds, 2) for name, seconds in sorted(total.items())}


def _file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def order_test_indices(nodeids: Iterable[str], durations: Mapping[str, float]) -> list[int]:
    """Schedule expensive files first without changing their internal test order.

    A file's recorded setup, call and teardown seconds are divided among its
    collected items. Unknown files use the mean known file duration. Return
    indices so duplicate nodeids remain separate collected items.
    """
    files = [_file_of(nodeid) for nodeid in nodeids]
    counts = Counter(files)
    known = [durations[name] for name in counts if name in durations]
    fallback = sum(known) / len(known) if known else 1.0
    weights = {name: durations.get(name, fallback) / count for name, count in counts.items()}
    return sorted(range(len(files)), key=lambda index: -weights[files[index]])


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the sharding options."""
    group = parser.getgroup("shard", "split the suite across CI runners")
    group.addoption(
        "--shard",
        default=os.environ.get("VIBESYS_TEST_SHARD"),
        metavar="I/N",
        help="run only shard I of N (default: $VIBESYS_TEST_SHARD)",
    )
    group.addoption(
        "--shard-durations",
        default=str(DEFAULT_DURATIONS),
        help="JSON map of test file to seconds used to balance shards",
    )
    group.addoption(
        "--record-shard-durations",
        default=os.environ.get("VIBESYS_RECORD_SHARD_DURATIONS"),
        metavar="PATH",
        help=(
            "write the measured seconds per test file to PATH after the run "
            "(default: $VIBESYS_RECORD_SHARD_DURATIONS)"
        ),
    )


def pytest_configure_node(node: WorkerController) -> None:
    """Forward the controller's scheduling choice before workers reset xdist options."""
    node.workerinput["duration_order"] = node.config.getoption("dist") == "load"


_PLAN = pytest.StashKey["_Plan"]()


class _Plan:
    """What every collection decision of one session needs, computed once.

    ``pytest_ignore_collect`` runs for every path, so walking the tree and parsing
    the record for each call cost minutes per shard.
    """

    def __init__(self, config: pytest.Config) -> None:
        self.durations: dict[str, float] = json.loads(
            Path(config.getoption("--shard-durations")).read_text()
        )
        self.files = discover_test_files(
            config.rootpath, config.getini("testpaths"), config.getini("python_files")
        )
        self.index, self.count = parse_shard(config.getoption("--shard"))
        self.whole = assign_shards(self.files, self.durations, self.count)


def _plan(config: pytest.Config) -> _Plan:
    if _PLAN not in config.stash:
        config.stash[_PLAN] = _Plan(config)
    return config.stash[_PLAN]


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Skip, before importing it, a whole file that another shard owns."""
    if config.getoption("--shard") is None or collection_path.suffix != ".py":
        return None
    try:
        name = collection_path.relative_to(config.rootpath).as_posix()
    except ValueError:
        return None
    plan = _plan(config)
    if name in plan.whole and plan.whole[name] != plan.index:
        return True
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Select the requested shard and prioritize expensive tests for load scheduling."""
    spec = config.getoption("--shard")
    order = getattr(config, "workerinput", {}).get("duration_order", False)
    if spec is None and not order:
        return
    durations = json.loads(Path(config.getoption("--shard-durations")).read_text())
    if spec is not None:
        plan = _plan(config)
        index, count = plan.index, plan.count
        files = [_file_of(item.nodeid) for item in items]
        assignment, loads = shard_loads({*plan.files, *files}, durations, count)
        seen: Counter[str] = Counter(files)
        heavy, _ = assign_heavy_items(seen, durations, loads)
        position: Counter[str] = Counter()
        kept: list[pytest.Item] = []
        dropped: list[pytest.Item] = []
        for item, name in zip(items, files, strict=True):
            owner = (
                heavy.get((name, position[name])) if is_heavy(name, durations) else assignment[name]
            )
            position[name] += 1
            (kept if owner == index else dropped).append(item)
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
    if order:
        indices = order_test_indices((item.nodeid for item in items), durations)
        items[:] = [items[index] for index in indices]


_measured: dict[str, float] = defaultdict(float)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Accumulate setup, call, and teardown seconds per file."""
    _measured[_file_of(report.nodeid)] += report.duration


def drift_report(
    measured: Mapping[str, float], durations: Mapping[str, float]
) -> tuple[float, list[str]]:
    """Return this shard's measured wall seconds and the files whose record is stale.

    A file is stale when it is missing from the record or ran more than twice
    its recorded seconds and at least 30 s longer. Stale records are how a shard
    grows past its budget unnoticed, so the report names them.
    """
    stale = [
        f"{name}: measured {seconds:.0f}s, recorded "
        + (f"{durations[name]:.0f}s" if name in durations else "nothing")
        for name, seconds in sorted(measured.items(), key=lambda pair: -pair[1])
        if seconds - durations.get(name, 0.0) >= 30 and seconds > 2 * durations.get(name, 0.0)
    ]
    return sum(measured.values()) / WORKERS_PER_SHARD, stale


def pytest_terminal_summary(terminalreporter: TerminalReporter, config: pytest.Config) -> None:
    """Warn when this shard overran its budget or ran files the record underestimates."""
    spec = config.getoption("--shard")
    if spec is None or hasattr(config, "workerinput"):
        return
    durations = json.loads(Path(config.getoption("--shard-durations")).read_text())
    seconds, stale = drift_report(_measured, durations)
    index, count = parse_shard(spec)
    terminalreporter.write_line(
        f"shard {index}/{count}: {seconds:.0f}s of tests across {WORKERS_PER_SHARD} workers "
        f"(budget {SHARD_BUDGET_SECONDS:.0f}s)"
    )
    if seconds > SHARD_BUDGET_SECONDS:
        terminalreporter.write_line(
            f"::warning::shard {index}/{count} took {seconds:.0f}s of tests, over the "
            f"{SHARD_BUDGET_SECONDS:.0f}s budget: add shards or refresh {DEFAULT_DURATIONS.name}"
        )
    for line in stale:
        terminalreporter.write_line(
            f"::warning::stale {DEFAULT_DURATIONS.name} entry, {line}: run "
            "scripts/refresh_shard_durations.py"
        )


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Write the measured durations from the controller process, not xdist workers."""
    path = session.config.getoption("--record-shard-durations")
    if path is None or hasattr(session.config, "workerinput"):
        return
    rounded = {name: round(seconds, 2) for name, seconds in sorted(_measured.items())}
    Path(path).write_text(json.dumps(rounded, indent=1) + "\n", encoding="utf-8")
