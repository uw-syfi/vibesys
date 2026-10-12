"""A mutator or judge turn that waits for its own evaluation is resumed.

Either role submits an evaluation through the production bridge and ends its turn waiting;
core resumes it once the job ends. A resumed turn that only reads the verdict records the same
revision again, and that current invocation's checkpoint is authoritative.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.checkpoint_faults import CHECKPOINTS
from tests.vibesys.orchestration.evolve.strategy.world import (
    ENDINGS,
    Ending,
    EvolveWorld,
    run_to_end,
)

from vs_core.api import AttemptPhase, Settle, Withdraw

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_dst.api import SimulatedRun

ROLES = (("mutator", "judge"), ("judge",), ("mutator",))


def _world(
    *,
    waits: tuple[bool, ...],
    polls: tuple[int, ...],
    parallelism: int,
    roles: tuple[str, ...] = ("mutator", "judge"),
    options: Mapping[str, Any] | None = None,
) -> EvolveWorld:
    """A one-generation run whose ``roles`` may wait; ``options`` are further world options."""
    return EvolveWorld(
        waits=waits,
        polls=polls,
        wait_roles=roles,
        max_generations=1,
        children_per_generation=2,
        max_parallelism=parallelism,
        **(options or {}),
    )


def _every_attempt_closed(run: SimulatedRun) -> bool:
    return all(attempt.phase == AttemptPhase.TERMINAL for attempt in run.core.attempts.attempts)


def test_a_child_whose_turn_waited_adopts_the_candidate_it_checkpointed() -> None:
    run = run_to_end(_world(waits=(True,), polls=(2,), parallelism=1, roles=("mutator",)))

    assert run.result is not None
    assert run.result.outcome == "success"
    assert run.core.evaluation.continuations != ()
    settles = [
        item.disposition
        for item in run.decisions
        if isinstance(item, Withdraw) and isinstance(item.disposition, Settle)
    ]
    assert settles
    assert all(item.candidate is not None for item in settles)


@settings(max_examples=4, deadline=None, derandomize=True, database=None)
@given(
    waits=st.lists(st.booleans(), min_size=1, max_size=4).map(tuple),
    polls=st.lists(st.integers(0, 3), min_size=1, max_size=3).map(tuple),
    parallelism=st.integers(1, 2),
)
def test_a_run_ends_whatever_its_turns_wait_for(
    waits: tuple[bool, ...], polls: tuple[int, ...], parallelism: int
) -> None:
    run = run_to_end(_world(waits=waits, polls=polls, parallelism=parallelism))

    assert _every_attempt_closed(run)


@settings(max_examples=10, deadline=None, derandomize=True, database=None)
@given(
    ends=st.lists(st.sampled_from(ENDINGS), min_size=1, max_size=4).map(tuple),
    polls=st.integers(0, 8),
    roles=st.sampled_from(ROLES),
)
def test_a_suspended_turn_is_a_wait_whatever_reply_it_ends_with(
    ends: tuple[Ending, ...], roles: tuple[str, ...], polls: int
) -> None:
    """Core holds a turn that validated a wait as suspended, so no reply may be accepted.

    A turn that ends with its final result or an invalid reply instead of the waiting reply
    must still yield: accepting or correcting it would request a turn core refuses, which
    halts the run. So the strategy's decisions must not depend on which reply the suspended
    turn ended with: they equal those of a run whose turns all end with the waiting reply.
    """

    def run(chosen: tuple[Ending, ...]) -> SimulatedRun:
        return run_to_end(
            _world(
                waits=(True,),
                polls=(polls,),
                parallelism=2,
                roles=roles,
                options={"ends": chosen},
            )
        )

    finished = run(ends)

    assert _every_attempt_closed(finished)
    if "invalid" not in ends:
        # Whether core records a wait on an invalid reply is core's own rule, so only the other
        # replies are compared.
        assert finished.core.evaluation.continuations != ()
        assert [type(item).__name__ for item in finished.decisions] == [
            type(item).__name__ for item in run(("wait",)).decisions
        ]


@settings(max_examples=10, deadline=None, derandomize=True, database=None)
@given(
    checkpoints=CHECKPOINTS,
    polls=st.integers(0, 3),
    waits=st.lists(st.booleans(), min_size=1, max_size=3).map(tuple),
    roles=st.sampled_from((("mutator",), ("mutator", "judge"))),
)
def test_a_run_ends_whatever_the_checkpoint_of_a_turn_does(
    checkpoints: tuple[str, ...], polls: int, waits: tuple[bool, ...], roles: tuple[str, ...]
) -> None:
    """A turn that asked to wait is suspended or failed with a reason, never left waiting.

    Core publishes the suspension only once the turn's workspace is checkpointed. Whether
    the turn waits or not, and whether the executor retains the workspace or fails the
    checkpoint, the run must reach terminal with every attempt closed and no wait left
    without something to end it (the liveness check). A role that depends on the write turn
    (the judge) is never started on a workspace core did not retain (#2167).
    """
    run = run_to_end(
        _world(
            waits=waits,
            polls=(polls,),
            parallelism=1,
            roles=roles,
            options={"checkpoints": checkpoints},
        )
    )

    assert _every_attempt_closed(run)


def test_a_declined_checkpoint_fails_the_waiting_turn_instead_of_stalling_the_run() -> None:
    """Every checkpoint fails: the turn cannot be suspended, and the run still ends."""
    run = run_to_end(
        _world(
            waits=(True,),
            polls=(1,),
            parallelism=1,
            roles=("mutator",),
            options={"checkpoints": ("failed",)},
        )
    )

    assert run.core.evaluation.continuations == ()


@settings(max_examples=6, deadline=None, derandomize=True, database=None)
@given(
    enveloped=st.booleans(),
    waits=st.lists(st.booleans(), min_size=1, max_size=3).map(tuple),
    ends=st.sampled_from(("wait", "wait_enveloped")),
)
def test_a_role_may_answer_in_the_bare_or_the_enveloped_form(
    *, enveloped: bool, waits: tuple[bool, ...], ends: Ending
) -> None:
    """The shared prompt tells agents to wrap waits in ``reply``; results may be wrapped too.

    Both forms of a wait or a final result are the same reply: the run's decisions equal those
    of a run whose agents answer in the bare form, and no offspring is discarded for a schema error.
    """
    run = run_to_end(
        _world(
            waits=waits,
            polls=(1,),
            parallelism=2,
            options={"ends": (ends,), "enveloped": enveloped},
        )
    )
    baseline = run_to_end(_world(waits=waits, polls=(1,), parallelism=2))

    assert [type(item).__name__ for item in run.decisions] == [
        type(item).__name__ for item in baseline.decisions
    ]
