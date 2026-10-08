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
from hypothesis.database import DirectoryBasedExampleDatabase
from hypothesis.internal.compat import int_from_bytes
from hypothesis.internal.reflection import function_digest

# `--shard=I/N` splits the suite across CI runners (see tests/support/sharding.py).
pytest_plugins = ["tests.support.isolated_environment", "tests.support.sharding"]

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


@pytest.fixture
def sandbox_tmp_path() -> Iterator[Path]:
    """Own a short workspace root outside globally granted device directories.

    Sandbox tests remove /tmp from scratch grants to probe their workspace
    boundary. A --basetemp under /dev/shm instead falls beneath /dev, whose
    device access grant cannot be narrowed by a child-path restriction.
    """
    with tempfile.TemporaryDirectory(prefix="vs-sbx-", dir="/tmp") as directory:
        yield Path(directory).resolve()


# Hypothesis's per-example deadline is a wall-clock dependence, so it is off in
# every profile. `ci` is deterministic so a run's examples are a pure function of
# the code and the saved failures: a newly found counterexample cannot fail an
# unrelated PR by chance. `explore` is the randomized, larger run for finding new
# bugs; select it by hand with `HYPOTHESIS_PROFILE=explore`. `nightly` is the
# scheduled run: randomized at the default example count, so each night draws new
# examples. Its failures are saved to the example database and their
# `@reproduce_failure` blobs are printed.
# Every profile states `derandomize` explicitly: an unset option resolves from
# whichever profile is loaded, so `explore` would otherwise inherit `ci`'s.
#
# Saving and replaying failures needs a database AND a per-test database key.
# Two obvious ways to make `ci` deterministic each remove one of them:
# `derandomize=True` implies `database=None`, and a forced global seed
# (`--hypothesis-seed`) sets the key to None, so nothing is saved or replayed.
# `hypothesis.seed` per test also sets `database=None`. So `ci` seeds each
# test as `derandomize=True` would, through its seed slot (see
# `_make_hypothesis_deterministic_under_ci`), which keeps both. CI restores
# `.hypothesis/` from the nightly run's cache, read-only on pull requests, so a
# failure found at nightly strength replays on a PR as its first example. The
# one departure from a pure PR tier is deliberate: a bug the nightly found keeps
# failing the PRs that reach it until it is fixed, since it is a bug on `main`.
# `VIBESYS_HYPOTHESIS_DB` relocates the database (tests/quality use it).
_EXAMPLE_DATABASE = DirectoryBasedExampleDatabase(
    os.environ.get("VIBESYS_HYPOTHESIS_DB")
    or Path(__file__).parent / ".hypothesis" / "examples"
)
settings.register_profile("dev", deadline=None, derandomize=False)
settings.register_profile(
    "ci", deadline=None, derandomize=False, print_blob=True, database=_EXAMPLE_DATABASE
)
settings.register_profile(
    "explore",
    deadline=None,
    derandomize=False,
    max_examples=500,
    print_blob=True,
    database=_EXAMPLE_DATABASE,
)
settings.register_profile(
    "nightly", deadline=None, derandomize=False, print_blob=True, database=_EXAMPLE_DATABASE
)
_PROFILE = os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev")
settings.load_profile(_PROFILE)

#: Hypothesis's per-test seed slot, the one `hypothesis.seed` sets. That decorator
#: also sets `database=None`, so the `ci` profile sets the slot directly.
_SEED_SLOT = "_hypothesis_internal_use_seed"


@pytest.fixture(autouse=True)
def _make_hypothesis_deterministic_under_ci(request: pytest.FixtureRequest) -> None:
    """Under the `ci` profile, draw the examples `derandomize=True` would, keeping the database.

    `derandomize=True` seeds a test's generator from the digest of its function.
    Setting the same seed slot gives a `@given` test those exact examples and keeps
    its database key. A stateful machine builds its test at run time, so its seed
    cannot be set ahead; it stays derandomized (and database-less) through its own
    `settings`. Both use Hypothesis internals (`function_digest`, `int_from_bytes`,
    the seed slot); tests/quality/test_hypothesis_profiles.py fails if an upgrade
    breaks determinism or the database round trip.
    """
    if _PROFILE != "ci":
        return
    test = request.function
    inner = getattr(getattr(test, "hypothesis", None), "inner_test", None)
    if inner is not None:
        setattr(test, _SEED_SLOT, int_from_bytes(function_digest(inner)))
    machine_settings = getattr(request.instance, "settings", None)
    if isinstance(machine_settings, settings):
        request.instance.settings = settings(machine_settings, derandomize=True, database=None)


#: xdist scheduling group for tests that cannot run beside one another.
_SERIAL_GROUP = "serial"

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
        if item.get_closest_marker(_SERIAL_GROUP) is not None:
            item.add_marker(pytest.mark.xdist_group(_SERIAL_GROUP))
            continue
        fixturenames = getattr(item, "fixturenames", ())
        for fixture_name, group in _BUILD_ONCE_GROUPS.items():
            if fixture_name in fixturenames:
                item.add_marker(pytest.mark.xdist_group(group))
                break
