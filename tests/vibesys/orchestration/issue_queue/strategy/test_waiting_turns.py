"""Turns that wait for their own evaluation always let the run end.

A fresh implementer or judge turn may submit an evaluation through the production bridge and end
its turn waiting; core resumes it once the job ends. The tests generate which turns wait, how
long each turn takes (in event-loop yields) and what the judge concludes, and require the run to
reach the result a run without waits reaches, with every attempt closed.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.checkpoint_faults import CHECKPOINTS
from tests.vibesys.orchestration.issue_queue.strategy.world import (
    IssueQueueWorld,
    evaluated,
    implemented,
    judged,
)

from vs_core.api import AttemptPhase
from vs_dst.api import SimulatedRun, simulate


@dataclass(frozen=True)
class Scenario:
    """One generated run: which turns wait, how long each takes, and what the judge says."""

    waits: tuple[bool, ...]
    delays: tuple[int, ...]
    verdicts: tuple[bool, ...]
    roles: tuple[str, ...] = ("implementer", "judge")
    ends: tuple[str, ...] = ("wait",)
    polls: int = 0
    checkpoints: tuple[str, ...] = ()
    """How the workspace executor answers each checkpoint request (see `FaultyCheckpoints`)."""


def run_waiting(scenario: Scenario) -> SimulatedRun:
    """A whole run whose turns wait as the scenario says; it ends and is live."""
    run = simulate(
        IssueQueueWorld(
            implementer=[implemented(f"turn {n}") for n in range(30)],
            judge=[judged(passed=scenario.verdicts[n % len(scenario.verdicts)]) for n in range(30)],
            perf=[evaluated() for _ in range(6)],
            waits=scenario.waits,
            delays=scenario.delays,
            roles=scenario.roles,
            ends=scenario.ends,
            polls=scenario.polls,
            checkpoints=scenario.checkpoints,
            max_attempts_per_issue=2,
        )
    )
    assert run.finished
    assert run.halted is None
    assert not run.liveness_violations
    return run


def _every_attempt_closed(run: SimulatedRun) -> bool:
    return all(attempt.phase == AttemptPhase.TERMINAL for attempt in run.core.attempts.attempts)


SCENARIOS = st.builds(
    Scenario,
    waits=st.lists(st.booleans(), min_size=1, max_size=6).map(tuple),
    delays=st.lists(st.integers(0, 6), min_size=1, max_size=4).map(tuple),
    verdicts=st.lists(st.booleans(), min_size=1, max_size=4).map(tuple),
    roles=st.sampled_from((("implementer", "judge"), ("judge",), ("implementer",))),
)


@settings(max_examples=10, deadline=None, derandomize=True, database=None)
@given(scenario=SCENARIOS)
def test_a_run_ends_whatever_its_agents_wait_for(scenario: Scenario) -> None:
    run = run_waiting(scenario)

    assert _every_attempt_closed(run)


def test_a_judge_that_waits_for_its_own_evaluation_is_resumed_and_the_run_adopts() -> None:
    run = run_waiting(Scenario(waits=(True,), delays=(0,), verdicts=(True,), roles=("judge",)))

    assert run.result is not None
    assert run.result.outcome == "success"
    assert run.core.evaluation.continuations != ()


def test_an_implementer_that_waits_for_its_own_evaluation_is_resumed_and_its_issue_closes() -> None:
    run = run_waiting(
        Scenario(waits=(True,), delays=(0,), verdicts=(True,), roles=("implementer",))
    )

    assert run.result is not None
    assert run.result.outcome == "success"
    assert run.core.evaluation.continuations != ()


@settings(max_examples=10, deadline=None, derandomize=True, database=None)
@given(
    ends=st.lists(st.sampled_from(("wait", "result", "invalid")), min_size=1, max_size=4).map(
        tuple
    ),
    roles=st.sampled_from((("implementer", "judge"), ("judge",), ("implementer",))),
    verdicts=st.lists(st.booleans(), min_size=1, max_size=4).map(tuple),
    polls=st.integers(0, 8),
)
def test_a_suspended_turn_is_a_wait_whatever_reply_it_ends_with(
    ends: tuple[str, ...], roles: tuple[str, ...], verdicts: tuple[bool, ...], polls: int
) -> None:
    """Core holds a turn that validated a wait as suspended, so no reply may be accepted.

    A turn that ends with its final result or an invalid reply instead of the waiting reply
    must still yield: accepting or correcting it would request a turn core refuses, which
    halts the run (`run_waiting` requires it not to). So the strategy's decisions must not
    depend on which reply the suspended turn ended with: they equal those of a run whose turns
    all end with the waiting reply.
    """

    def run(chosen: tuple[str, ...]) -> SimulatedRun:
        return run_waiting(
            Scenario(
                waits=(True,), delays=(0,), verdicts=verdicts, roles=roles, ends=chosen, polls=polls
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


@settings(max_examples=15, deadline=None, derandomize=True, database=None)
@given(
    checkpoints=CHECKPOINTS,
    roles=st.sampled_from((("implementer", "judge"), ("implementer",))),
    waits=st.lists(st.booleans(), min_size=1, max_size=3).map(tuple),
)
def test_a_run_ends_whatever_the_checkpoint_of_a_turn_does(
    checkpoints: tuple[str, ...], roles: tuple[str, ...], waits: tuple[bool, ...]
) -> None:
    """A write turn whose checkpoint fails ends its job with a reason, and no judge reads it.

    Whether the turn waits first or not, and whichever checkpoint answers fail, the run ends:
    a turn that asked to wait is suspended or failed with a reason, and a turn whose
    workspace was not retained is never reviewed (#2167).
    """
    run = run_waiting(
        Scenario(waits=waits, delays=(0,), verdicts=(True,), roles=roles, checkpoints=checkpoints)
    )

    assert _every_attempt_closed(run)


@pytest.mark.parametrize("waits", [True, False])
def test_a_failed_write_checkpoint_fails_the_issue_instead_of_stalling_the_judge(
    *, waits: bool
) -> None:
    """The implementer's write checkpoint fails (after a wait's checkpoint succeeded, if any)."""
    run = run_waiting(
        Scenario(
            waits=(waits,),
            delays=(0,),
            verdicts=(True,),
            checkpoints=("succeeded", "failed") if waits else ("failed",),
        )
    )

    assert run.result is not None
    assert run.result.outcome != "success"


def test_a_declined_checkpoint_fails_the_waiting_turn_instead_of_stalling_the_run() -> None:
    """Every checkpoint fails: the turn cannot be suspended, and the run still ends."""
    run = run_waiting(
        Scenario(waits=(True,), delays=(0,), verdicts=(True,), checkpoints=("failed",))
    )

    assert run.core.evaluation.continuations == ()
