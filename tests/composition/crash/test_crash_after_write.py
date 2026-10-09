"""A host crash after any durable write of the skeleton run converges to the crash-free result.

CI crashes at one representative of each recovery path (``representatives``); the slow sweep
crashes after every durable write.
"""

from __future__ import annotations

import pytest
from tests.support.crash_harness import (
    converges_after_one,
    crash_points,
    name,
    representatives,
)

from vs_faults.api import Boundary, Crossing


def _writes() -> list[Crossing]:
    return [c for c in crash_points() if c.boundary == Boundary.DURABLE_WRITE]


def _representative_writes() -> list[Crossing]:
    reps = representatives()
    chosen = {*reps.sealed, *reps.commits}
    return [c for c in _writes() if c in chosen]


@pytest.mark.parametrize("crossing", _representative_writes(), ids=name)
def test_a_crash_after_representative_writes_converges(crossing: Crossing) -> None:
    converges_after_one(crossing)


@pytest.mark.slow
@pytest.mark.parametrize("crossing", _writes(), ids=name)
def test_a_crash_after_each_boundary_converges(crossing: Crossing) -> None:
    converges_after_one(crossing)
