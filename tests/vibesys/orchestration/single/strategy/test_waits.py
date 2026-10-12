"""A combined turn that waits for its own evaluation is resumed, and its checkpoint is the candidate.

The implementer submits an evaluation through the production bridge and ends its turn waiting;
core resumes it once the job ends. The resumed turn only reads the verdict, so it retains
nothing of its own: the round's candidate is what the turn checkpointed before it waited.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.checkpoint_faults import CHECKPOINTS
from tests.vibesys.orchestration.single.strategy.world import ENDINGS, Ending, SingleWorld

from vs_core.api import AttemptPhase, Settle, Withdraw
from vs_dst.api import SimulatedRun, simulate

SEARCHES = settings(max_examples=10, deadline=None, derandomize=True, database=None)


def _every_attempt_closed(run: SimulatedRun) -> bool:
    return all(attempt.phase == AttemptPhase.TERMINAL for attempt in run.core.attempts.attempts)


def test_a_round_whose_turn_waited_adopts_the_candidate_it_checkpointed() -> None:
    world = SingleWorld(waits=(True,), polls=(2,), max_rounds=1)
    run = simulate(world)

    assert run.finished
    assert not run.liveness_violations
    assert run.result is not None
    assert run.result.outcome == "success"
    assert run.core.evaluation.continuations != ()
    (settle,) = [
        item.disposition
        for item in run.decisions
        if isinstance(item, Withdraw) and isinstance(item.disposition, Settle)
    ]
    assert settle.candidate is not None


@SEARCHES
@given(
    waits=st.lists(st.booleans(), min_size=1, max_size=4),
    polls=st.lists(st.integers(0, 3), min_size=1, max_size=3),
    rounds=st.integers(1, 2),
)
def test_a_run_ends_whatever_its_turns_wait_for(
    waits: list[bool], polls: list[int], rounds: int
) -> None:
    run = simulate(SingleWorld(waits=waits, polls=polls, max_rounds=rounds))

    assert run.finished
    assert not run.liveness_violations
    assert _every_attempt_closed(run)


@SEARCHES
@given(
    ends=st.lists(st.sampled_from(ENDINGS), min_size=1, max_size=4),
    rounds=st.integers(1, 2),
    polls=st.integers(0, 8),
)
def test_a_suspended_turn_is_a_wait_whatever_reply_it_ends_with(
    ends: list[Ending], rounds: int, polls: int
) -> None:
    """Core holds a turn that validated a wait as suspended, so no reply may be accepted.

    A turn that ends with its final result or an invalid reply instead of the waiting reply
    must still yield: accepting or correcting it would request a turn core refuses, which
    halts the run. So the strategy's decisions must not depend on which reply the suspended
    turn ended with: they equal those of a run whose turns all end with the waiting reply.
    """
    run = simulate(SingleWorld(waits=(True,), polls=(polls,), ends=ends, max_rounds=rounds))

    assert run.finished
    assert run.halted is None
    assert not run.liveness_violations
    assert _every_attempt_closed(run)
    if "invalid" not in ends:
        # Whether core records a wait on an invalid reply is core's own rule, so only the other
        # replies are compared.
        assert run.core.evaluation.continuations != ()
        waiting = simulate(SingleWorld(waits=(True,), polls=(polls,), max_rounds=rounds))
        assert [type(item).__name__ for item in run.decisions] == [
            type(item).__name__ for item in waiting.decisions
        ]


@SEARCHES
@given(
    checkpoints=CHECKPOINTS,
    polls=st.integers(0, 3),
    waits=st.lists(st.booleans(), min_size=1, max_size=3),
)
def test_a_run_ends_whatever_the_checkpoint_of_a_turn_does(
    checkpoints: tuple[str, ...], polls: int, waits: list[bool]
) -> None:
    """A turn that asked to wait is suspended or failed with a reason, never left waiting.

    Core publishes the suspension only once the turn's workspace is checkpointed. Whether
    the turn waits or not, and whether the executor retains the workspace or fails the
    checkpoint, the run must reach terminal with every attempt closed and no wait left
    without something to end it. A role that depends on the write turn (the judge) is never
    started on a workspace core did not retain (#2167).
    """
    run = simulate(SingleWorld(waits=waits, polls=(polls,), checkpoints=checkpoints, max_rounds=1))

    assert run.finished
    assert not run.liveness_violations
    assert _every_attempt_closed(run)


def test_a_declined_checkpoint_fails_the_waiting_turn_instead_of_stalling_the_run() -> None:
    """Every checkpoint fails: the turn cannot be suspended, and the run still ends."""
    run = simulate(SingleWorld(waits=(True,), polls=(1,), checkpoints=("failed",), max_rounds=1))

    assert run.finished
    assert not run.liveness_violations
    assert run.core.evaluation.continuations == ()
