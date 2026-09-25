"""Suite-wide pytest configuration shared by every ``testpaths`` root.

Lives at the repository root rather than under ``tests/`` because the marker it
defines has to mean the same thing for ``tests/``, ``libs/*/tests``, and
``sdk/*/tests``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from hypothesis import settings

if TYPE_CHECKING:
    from collections.abc import Iterable

# Hypothesis's per-example deadline is a wall-clock dependence, so it is off in
# every profile. `ci` is derandomized so a run's examples are a pure function of
# the code: a newly found counterexample cannot fail an unrelated PR. `explore`
# is the randomized, larger run for finding new bugs; select it by hand with
# `HYPOTHESIS_PROFILE=explore`.
# Every profile states `derandomize` explicitly: an unset option resolves from
# whichever profile is loaded, so `explore` would otherwise inherit `ci`'s.
settings.register_profile("dev", deadline=None, derandomize=False)
settings.register_profile("ci", deadline=None, derandomize=True, print_blob=True)
settings.register_profile(
    "explore", deadline=None, derandomize=False, max_examples=500, print_blob=True
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev"))

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
