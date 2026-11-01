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

How such a run ends is the doc's rule per cancel reason (the loop's table): a step that reaches its
limit (``DEADLINE``) keeps asking ``clear``, so the loop records no result over it and never records
the run's end while it holds the tag; only what abandons the ``clear`` (a pause, an ending of a step
it cancels) or the host's stop grace (``StepLoopHosting(stop_grace_s=...)``, which a stop the control
channel took arms and core's own deadline stop does not) lets go of it. The tests here hold the
loop's half of that: nothing is recorded over the ``clear``, and a run it holds ends by the grace and
by nothing else. What the fold shows of such a run is not held yet: the kernel's request recovery
(`_request_recovery.py`, which the prove-or-block deletion removes) still ends the open request at its
limit through a ``BlockRequest`` and takes the run to its terminal state with no result recorded for
the step, so the log shows an end and a step result it never holds until that layer is gone.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from protobuf import Oneof

from vs_core.api import RunStatus
from vs_core.api.recovery import RunRest, at_rest
from vs_core.api.testing import Unknown
from vs_dst.api import STEP_LOOP_TIMING, StepLoopHosting, StopKind, simulate
from vs_sim.api.testing import examples

from ._tiny import TinyWorld

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.api.testing import Answer
    from vs_proto.api import Record

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


def _held_tag(core: CoreState) -> str:
    """The one step whose attempt has not ended: the step the failing ``clear`` holds."""
    (held,) = (row.tag.root for row in core.steps.attempts if not row.ended)
    return held


def _fields(records: tuple[Record, ...], field: str) -> tuple[Any, ...]:
    """The bodies of the records of one kind, in log order."""
    return tuple(
        r.body.value for r in records if isinstance(r.body, Oneof) and r.body.field == field
    )


def _records_no_result_and_no_end_over(records: tuple[Record, ...], core: CoreState) -> None:
    """The loop's half of the rule: the held step has no ``StepFinished`` and the run has no ``RunEnded``."""
    held = _held_tag(core)
    finished = [message.tag for message in _fields(records, "step_finished")]
    assert held not in finished, "a result is recorded over a clear that proves nothing"
    assert not _fields(records, "run_ended"), "the run's end is recorded while a tag is held"
    assert at_rest(core) is not RunRest.ENDED


@settings(max_examples=examples(pr=6, nightly=60))
@given(
    kind=st.sampled_from(SETTLING),
    deadline_at=st.sampled_from((FAR, DRAIN_AT)),
    patience=st.floats(min_value=20.0, max_value=400.0, allow_nan=False),
)
@example(kind="ensure_workspace", deadline_at=DRAIN_AT, patience=400.0)
@example(kind="close_attempt_scope", deadline_at=DRAIN_AT, patience=400.0)
@example(kind="discard_workspace", deadline_at=DRAIN_AT, patience=400.0)
def test_a_run_a_failing_clear_holds_has_no_result_recorded_over_it_and_no_end(
    kind: str, deadline_at: float, patience: float
) -> None:
    """Whether or not the run's deadline drain passes the step's limit, the held step ends no way the log shows."""
    run = simulate(
        DeadClearWorld(kind, deadline_at, gives_up_after=10**6),
        StepLoopHosting(),
        patience=patience,
    )

    _records_no_result_and_no_end_over(run.log_records, run.core)


@settings(max_examples=examples(pr=8, nightly=120), deadline=None)
@given(
    kind=st.sampled_from(SETTLING),
    stop=st.sampled_from(tuple(StopKind)),
    grace=st.floats(min_value=5.0, max_value=200.0, allow_nan=False),
    at=st.integers(min_value=0, max_value=1000),
)
@example(kind="close_attempt_scope", stop=StopKind.OPERATOR_CANCEL, grace=60.0, at=0)
def test_a_run_a_failing_clear_holds_is_let_go_by_the_hosts_stop_grace_and_by_nothing_else(
    kind: str, stop: StopKind, grace: float, at: int
) -> None:
    """A stop the control channel took ends the host ``grace`` after it, with no end in the log.

    Core's own deadline stop is not the channel's: the host gives no grace to it, so a run it
    drains is waited on until the operator gives up on the host.
    """
    patience = 5 * grace
    run = simulate(
        DeadClearWorld(kind, FAR, gives_up_after=10**6),
        StepLoopHosting(stop_grace_s=grace),
        patience=patience,
    )
    points = run.stop_points()
    stopped = run.stop(points[at % len(points)], stop)

    assert stopped.core is not None
    assert stopped.delivered_at is not None
    assert not stopped.ended, (kind, stop, stopped.why_not_ended)
    _records_no_result_and_no_end_over(stopped.log_records, stopped.core)
    if stop is StopKind.DEADLINE:
        assert stopped.took is not None
        assert stopped.took > grace, "core's deadline stop was given the control channel's grace"
    else:
        assert stopped.took == pytest.approx(grace), (kind, stop)
