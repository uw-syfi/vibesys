"""A host crash between authorizing a request and running it.

The restart inspects the request. If it never began (the receipt store has no ``begun``
record) core issues the same request again, once per recovery epoch, without asking the
strategy to replan. If it began and has no result (the crash came right after the ``begun``
marker), core issues it again to resume, and the executor inspects the external effect before
repeating anything. Recovery is replay, so the run ends exactly as the straight run does.

CI crashes at one point per request kind (begun marker) and the sampled dispatch
authorizations; the slow sweep crashes at every one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.crash_harness import (
    converges_after_one,
    crash_plan,
    name,
    pre_effect_points,
    representatives,
    run,
)

from vs_core.api import RunStatus

if TYPE_CHECKING:
    from vs_faults.api import Crossing


def _representative_points() -> list[Crossing]:
    reps = representatives()
    chosen = {*reps.begun, *reps.commits}
    return [c for c in pre_effect_points() if c in chosen]


def _check(crossing: Crossing) -> None:
    converges_after_one(crossing)
    assert run(crash_plan(crossing)).summary.status == RunStatus.TERMINAL


@pytest.mark.parametrize("crossing", _representative_points(), ids=name)
def test_a_crash_before_an_effect_ends_the_run_as_the_straight_run_does(
    crossing: Crossing,
) -> None:
    _check(crossing)


@pytest.mark.slow
@pytest.mark.parametrize("crossing", pre_effect_points(), ids=name)
def test_a_crash_before_each_effect_ends_the_run_as_the_straight_run_does(
    crossing: Crossing,
) -> None:
    _check(crossing)
