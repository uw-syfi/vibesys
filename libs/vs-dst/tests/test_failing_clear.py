"""A ``clear`` that fails for good is retried at the loop's backoff, and no faster, whatever stops the run.

``docs/contributing/run-recovery.md`` (rules 3 and 5, and the loop's table of cancel reasons): a
``clear`` that cannot reach its backend proves nothing, so the loop asks again with backoff and
records no result over it. It is the one wait the loop does not bound by itself; the run ends only
when an ending abandons it, or the host's stop grace cancels the host (which the simulation's
patience stands for). What the loop does bound is the pace: a step whose ``clear`` fails for good
must never spend its tries in one instant, as a step whose runner raises at once never does.

The far side here is a Docker daemon that goes away after one attempt of a step raised: every
``clear`` after it raises ``ClearUnavailableError`` (`ClearFailingWorld`). The run's deadline is
either far, so nothing stops the run, or inside the wait, so a drain stop lands while the step
holds its tag.

What this does not hold yet: how such a run ends under a drain. Today the kernel's request recovery
(`_request_recovery.py`, which the prove-or-block deletion removes) ends the open request at its
limit through a ``BlockRequest`` and never records the step's result, so the end the doc states
(an ending abandons the ``clear`` of a step it cancels, and waits for one it releases) cannot be
read from the log until that layer is gone.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_core.api import RunStatus
from vs_core.api.testing import Unknown
from vs_dst.api import STEP_LOOP_TIMING, StepLoopHosting, simulate
from vs_sim.api.testing import examples

from ._tiny import TinyWorld

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.api.testing import Answer

FAR = 100_000.0
"""A run deadline nothing reaches: only a failing ``clear`` holds the run."""
DRAIN_AT = 60.0
"""A run deadline inside the wait: the drain stop lands while the step waits in ``clear``."""

SETTLING = ("ensure_workspace", "close_attempt_scope", "discard_workspace")
"""The steps of the search's settlement whose first attempt the far side leaves without a result."""


class DeadClearWorld(TinyWorld):
    """The first attempt of one kind of step gets no answer, and the far side is gone after it."""

    def __init__(self, kind: str, deadline_at: float, *, gives_up_after: int) -> None:
        super().__init__(2)
        setup = self.setup
        self.setup = dataclasses.replace(
            setup, harness=dataclasses.replace(setup.harness, deadline_at=deadline_at)
        )
        self.kind = kind
        self.silent = False
        self.clears = 0
        """How many times a ``clear`` was asked for."""
        self._gives_up_after = gives_up_after

    def __call__(self, request: Request, core: CoreState) -> Answer:
        if request.kind == self.kind and not self.silent:
            self.silent = True
            return Unknown()
        return super().__call__(request, core)

    def clear_fails(self, request: Request) -> bool:
        """Gone for good, except that a loop asking without end is answered after ``gives_up_after`` asks.

        A loop that retries in no time never advances the virtual clock, so it would never reach
        the host's patience: past the limit the far side answers, the run goes on, and the count
        the test checks shows what the loop did.
        """
        del request
        self.clears += 1
        return self.silent and self.clears <= self._gives_up_after


@settings(max_examples=examples(pr=6, nightly=60))
@given(
    kind=st.sampled_from(SETTLING),
    deadline_at=st.sampled_from((FAR, DRAIN_AT)),
    patience=st.floats(min_value=20.0, max_value=400.0, allow_nan=False),
)
def test_a_clear_that_fails_for_good_is_retried_no_faster_than_the_loops_backoff(
    kind: str, deadline_at: float, patience: float
) -> None:
    """In the time the host is waited on, ``clear`` is asked for at most once per backoff, and again and again."""
    floor = STEP_LOOP_TIMING.clear_backoff_s
    bound = patience / floor + 1
    world = DeadClearWorld(kind, deadline_at, gives_up_after=int(bound) + 1)
    run = simulate(world, StepLoopHosting(), patience=patience)

    assert isinstance(run.world, DeadClearWorld)
    assert run.world.clears <= bound, (kind, deadline_at, patience)
    assert run.world.clears >= 2, (kind, deadline_at, patience)


@pytest.mark.parametrize("kind", SETTLING)
def test_a_clear_that_fails_for_good_holds_a_run_nothing_stops(kind: str) -> None:
    """With no stop and no deadline in reach the run waits on the ``clear``, and the host is given up on."""
    run = simulate(
        DeadClearWorld(kind, FAR, gives_up_after=10**6), StepLoopHosting(), patience=300.0
    )

    assert not run.finished
    assert run.core.run.status is not RunStatus.TERMINAL
    assert run.core.run.result is None
