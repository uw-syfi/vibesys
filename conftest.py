"""Suite-wide pytest configuration shared by every ``testpaths`` root.

Lives at the repository root rather than under ``tests/`` because the marker it
defines has to mean the same thing for ``tests/``, ``libs/*/tests``, and
``sdk/*/tests``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import settings
from tests.support.scratch_tree import remove_scratch_tree

from vs_sim.api.testing import Depth

# `--shard=I/N` splits the suite across CI runners (see tests/support/sharding.py).
# The hang-dump plugin is first so its deadline timer is armed before other
# plugins do work. The timer no-ops unless the workflow passed
# VIBESYS_PYTEST_DUMP_AT. The same plugin names each quiet-mode failure when
# the report arrives, so a cancelled session still shows the node id.
pytest_plugins = [
    "scripts.pytest_hang_dump",
    "vs_sim_pytest",
    "tests.support.isolated_environment",
    "tests.support.cwd_hygiene",
    "tests.support.tree_hygiene",
    "tests.support.path_hygiene",
    "tests.support.sharding",
    "tests.support.worker_exit",
    "tests.support.bounded_comparison",
]

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# NumPy's BLAS starts one busy-waiting thread per core when it loads, and the product loads
# NumPy at import (through the evolve search's dependencies). On a many-core host that
# doubles the CPU a worker spends on its imports (12 CPU-s against 5 for the same 6 s of
# wall time) and takes cores from the other workers; no test depends on threaded linear
# algebra. Set before collection imports the product, inherited by every child process.
for _variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_variable, "1")


@pytest.fixture
def sandbox_tmp_path() -> Iterator[Path]:
    """Own a short workspace root outside globally granted device directories.

    Sandbox tests remove /tmp from scratch grants to probe their workspace
    boundary. A --basetemp under /dev/shm instead falls beneath /dev, whose
    device access grant cannot be narrowed by a child-path restriction.
    """
    root = Path(tempfile.mkdtemp(prefix="vs-sbx-", dir="/tmp")).resolve()
    try:
        yield root
    finally:
        remove_scratch_tree(root)


# Hypothesis's per-example deadline is a wall-clock dependence, so it is off in
# every profile. The session's depth (`--depth`, owned by the vs-sim plugin)
# picks the profile: `pr` loads `ci`, derandomized so a run's examples are a
# pure function of the code (a newly found counterexample cannot fail an
# unrelated PR); `nightly` loads `nightly`, randomized so each night draws
# examples the derandomized tier never reaches, with `print_blob` so a failure
# prints its `@reproduce_failure` line, which replays under any profile (pin it
# with `@example(...)`). `HYPOTHESIS_PROFILE=dev|explore` overrides by hand:
# `dev` is randomized at the PR count, `explore` is the larger run for finding
# new bugs.
# `dev` and `ci` run 25 examples, not Hypothesis's 100. Branch coverage of the
# libraries is unchanged at 25 (measured for `vs-core`), while the property
# suites that do not set their own `max_examples` run about 3x faster; a test
# that needs a deeper search states `examples(pr=..., nightly=...)`.
# Every profile states `derandomize` explicitly: an unset option resolves from
# whichever profile is loaded, so `explore` would otherwise inherit `ci`'s.
settings.register_profile("dev", deadline=None, derandomize=False, max_examples=25)
settings.register_profile("ci", deadline=None, derandomize=True, print_blob=True, max_examples=10)
settings.register_profile(
    "explore", deadline=None, derandomize=False, max_examples=500, print_blob=True
)
settings.register_profile(
    "nightly", deadline=None, derandomize=False, max_examples=100, print_blob=True
)
_PROFILE_OF_DEPTH = {Depth.PR: "ci", Depth.NIGHTLY: "nightly"}


def pytest_configure(config: pytest.Config) -> None:
    """Load the Hypothesis profile that follows the session's depth."""
    chosen = (
        os.environ.get("HYPOTHESIS_PROFILE") or _PROFILE_OF_DEPTH[Depth(config.getoption("depth"))]
    )
    settings.load_profile(chosen)


#: xdist scheduling group for tests that cannot run beside one another.
_SERIAL_GROUP = "serial"

# Markers whose tests drive real processes, images or an exhaustive sweep and
# carry their own, longer deadlines (the largest is a 1800 s image build). They
# get a higher hard backstop than the deterministic default in pyproject.toml.
# A test that sets `@pytest.mark.timeout` itself keeps its own value.
# Real-tier tests, and deterministic tests at nightly depth, keep the previous
# 300 s bound. Nightly example counts are several times the shard depth, and
# that job's cancel is 60 minutes, so the shorter shard bound does not apply.
_LONG_RUNNING_MARKERS = ("e2e", "real_contract", "slow")
_LONG_RUNNING_TIMEOUT_S = 3600
_PRESERVED_TIMEOUT_S = 300

#: Fixtures that compile a real binary once per ``scope="session"`` and are
#: consumed by more than one test. "Session" only means "this xdist worker's
#: session": with ``-n auto``, a fixture's consumers can land on different
#: worker processes, and each of those workers builds it again from scratch,
#: since a session fixture is never actually shared across workers. Pinning
#: every consumer of the same fixture to one ``xdist_group`` (as
#: ``loadgroup`` already does for the ``serial`` marker below) makes the
#: build happen once per run, the way "session-scoped" reads.
_BUILD_ONCE_GROUPS = {
    "queue_native_runner": "queue-native-build",
    "compiled_queue_candidate": "queue-native-build",
    "priority_queue_native_runner": "prioqueue-native-build",
    "compiled_priority_queue_candidate": "prioqueue-native-build",
}


#: Fixtures that compile native code (cargo, make, go) or build the evaluator's native runner.
#: A test that requests one is a build-and-run test of an example evaluator: it gets the
#: ``native_build`` marker and runs in CI's ``native-evaluator-examples`` job
#: (``examples_native_evaluator_tests``), not in the always-on Python shards, which deselect
#: it. The tests of the same files that need no build (manifest and shape checks) stay.
_NATIVE_BUILD_FIXTURES = frozenset(
    {
        "queue_native_runner",
        "compiled_queue_candidate",
        "priority_queue_native_runner",
        "compiled_priority_queue_candidate",
        "built_priority_queue_baselines",
        "stack_native_runner",
        "compiled_stack_candidate",
        "built_stack_boost_lockfree",
        "ordered_map_native_runner",
        "compiled_ordered_map_candidate",
        "built_ordered_map_tbb",
        "unordered_map_native_runner",
        "compiled_unordered_map_candidate",
        "built_unordered_map_tbb",
    }
)


def _in_real_tier(item: pytest.Item) -> bool:
    """True when ``item`` lives in a ``sim_real_tiers`` directory.

    The directory list is the pytest ini the vs-sim plugin owns. The match is
    the same prefix rule as that plugin: a test is real when its path is the
    directory or a file inside it.
    """
    tiers = item.config.getini("sim_real_tiers")
    try:
        relative = item.path.relative_to(item.config.rootpath).as_posix()
    except ValueError:
        return False
    return any(relative == tier or relative.startswith(f"{tier}/") for tier in tiers)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: Iterable[pytest.Item]) -> None:
    """Pin every ``serial`` test, and every consumer of a shared build fixture, to one xdist worker.

    CI runs the suite with ``-n auto --dist loadgroup``. A test marked
    ``serial`` claims a process-wide or host-wide resource (a fixed port, a
    fixed path, the process environment), so two of them running at once is a
    flake. ``loadgroup`` sends every item in one ``xdist_group`` to the same
    worker, which serializes them with respect to each other while the rest of
    the suite keeps fanning out. Without xdist the marker is inert, and the
    suite is serial anyway.

    A test that requests one of ``_BUILD_ONCE_GROUPS`` gets the matching
    group automatically, purely from the fixture name it already has to
    declare to use the built artifact — there is nothing separate for a new
    test to remember.

    ``tryfirst=True`` is load-bearing, not stylistic: xdist's own worker-side
    ``pytest_collection_modifyitems`` (``xdist/remote.py``) reads each item's
    ``xdist_group`` markers and bakes the group into the item's nodeid so the
    controller can schedule by it. Markers added by a plain (untried) hookimpl
    in this file run in registration order relative to that xdist hookimpl,
    which is not guaranteed to be before it; without ``tryfirst`` the group
    marker can be added one hook call too late for xdist to see it, and
    ``--dist loadgroup`` silently stops grouping anything this hook tags,
    with no error. Verified with a two-item probe under ``-n 4 --dist
    loadgroup``: without ``tryfirst`` both items landed on different workers
    despite carrying the same group; with it, both landed on the same worker.
    """
    for item in items:
        if item.get_closest_marker("timeout") is None:
            if any(item.get_closest_marker(name) is not None for name in _LONG_RUNNING_MARKERS):
                item.add_marker(pytest.mark.timeout(_LONG_RUNNING_TIMEOUT_S))
            elif _in_real_tier(item) or item.config.getoption("depth") == Depth.NIGHTLY.value:
                item.add_marker(pytest.mark.timeout(_PRESERVED_TIMEOUT_S))
        if item.get_closest_marker(_SERIAL_GROUP) is not None:
            item.add_marker(pytest.mark.xdist_group(_SERIAL_GROUP))
            continue
        fixturenames = getattr(item, "fixturenames", ())
        if _NATIVE_BUILD_FIXTURES.intersection(fixturenames):
            item.add_marker(pytest.mark.native_build)
        for fixture_name, group in _BUILD_ONCE_GROUPS.items():
            if fixture_name in fixturenames:
                item.add_marker(pytest.mark.xdist_group(group))
                break
