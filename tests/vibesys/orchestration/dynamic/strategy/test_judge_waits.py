"""Workstreams whose agents wait for their own evaluations always let the run end.

A fresh implementer or judge turn may submit an evaluation of its own through the
production bridge and end its turn waiting; core resumes it once the job ends. The tests
generate which turns wait, how long each job runs, how long each turn takes (in event-loop
yields), what the judge and the implementer conclude and what the benchmark measures, with
`max_rounds` and `max_in_flight` from small values, and require the run to reach terminal
with every attempt closed and nothing left waiting.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.checkpoint_faults import CHECKPOINTS
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy.world import ENDINGS, DynamicWorld, Ending

from vs_core.api import AttemptPhase, Settle, Withdraw
from vs_dst.api import SimulatedRun, simulate


@dataclass(frozen=True)
class Scenario:
    """One generated run: the budgets, which turns wait, and what everything answers."""

    rounds: int
    in_flight: int
    waits: tuple[bool, ...]
    """For the n-th fresh turn of a waiting role, whether it submits an evaluation and waits."""
    polls: tuple[int, ...]
    """How many polls each evaluation job reports running, in submission order."""
    delays: tuple[int, ...]
    """Event-loop yields each turn takes before it ends, in start order."""
    verdicts: tuple[bool, ...]
    outcomes: tuple[str, ...]
    throughput: tuple[float, ...]
    parents: bool = False
    roles: tuple[str, ...] = ("implementer", "judge")
    """The roles whose turns may wait."""
    submit_only: tuple[str, ...] = ()
    """The roles whose turns submit an evaluation and end plainly, leaving it running."""
    ends: tuple[Ending, ...] = ("wait",)
    """How each waiting turn ends: ``wait``, ``result`` or ``invalid`` (see `DynamicWorld`)."""
    unchanged_after_first_retain: bool = False
    """Whether a resumed turn leaves the worktree as it was (retains nothing of its own)."""
    checkpoints: tuple[str, ...] = ()
    """How the workspace executor answers each checkpoint request (see `FaultyCheckpoints`)."""


def waiting_world(scenario: Scenario) -> DynamicWorld:
    """A world whose turns wait as the scenario says."""
    entries = [
        [
            implement(
                f"h{round_}-{n}",
                **({"parent_hypothesis_id": f"h0-{n}"} if scenario.parents and round_ else {}),
            )
            for n in range(scenario.in_flight)
        ]
        for round_ in range(scenario.rounds + 6)
    ]
    return DynamicWorld(
        planner=[plan_reply(*items) for items in entries],
        implementer=[implemented(scenario.outcomes[n % len(scenario.outcomes)]) for n in range(60)],
        judge=[reviewed(passed=scenario.verdicts[n % len(scenario.verdicts)]) for n in range(60)],
        waits=scenario.waits,
        delays=scenario.delays,
        polls=scenario.polls,
        roles=scenario.roles,
        ends=scenario.ends,
        submit_only=scenario.submit_only,
        unchanged_after_first_retain=scenario.unchanged_after_first_retain,
        throughputs=scenario.throughput,
        checkpoints=scenario.checkpoints,
        max_rounds=scenario.rounds,
        max_in_flight=scenario.in_flight,
    )


def run_waiting(scenario: Scenario) -> SimulatedRun:
    """A whole run whose turns wait as the scenario says."""
    return simulate(waiting_world(scenario))


def assert_ended_closed(run: SimulatedRun) -> None:
    """The run is terminal and live, with every attempt closed."""
    assert run.finished
    assert not run.liveness_violations
    for attempt in run.core.attempts.attempts:
        assert attempt.phase == AttemptPhase.TERMINAL


SCENARIOS = st.builds(
    Scenario,
    rounds=st.integers(1, 2),
    in_flight=st.integers(1, 2),
    waits=st.lists(st.booleans(), min_size=1, max_size=6).map(tuple),
    polls=st.lists(st.integers(0, 4), min_size=1, max_size=4).map(tuple),
    delays=st.lists(st.integers(0, 6), min_size=1, max_size=4).map(tuple),
    verdicts=st.lists(st.booleans(), min_size=1, max_size=4).map(tuple),
    outcomes=st.lists(
        st.sampled_from(("supported", "disproven", "nominated")), min_size=1, max_size=4
    ).map(tuple),
    throughput=st.lists(st.floats(10.0, 200.0), min_size=1, max_size=4).map(tuple),
    parents=st.booleans(),
    roles=st.sampled_from((("implementer", "judge"), ("judge",), ("implementer",))),
)


@settings(max_examples=12, deadline=None, derandomize=True, database=None)
@given(scenario=SCENARIOS)
def test_a_run_ends_whatever_its_agents_wait_for(scenario: Scenario) -> None:
    assert_ended_closed(run_waiting(scenario))


JUDGE_WAITS = Scenario(
    rounds=1,
    in_flight=1,
    waits=(True,),
    polls=(2,),
    delays=(0,),
    verdicts=(True,),
    outcomes=("supported",),
    throughput=(100.0,),
    roles=("judge",),
)


def test_a_judge_that_waits_for_its_own_evaluation_is_resumed_and_the_run_adopts() -> None:
    """The v12 stall: the judge, a read-only turn, submitted an evaluation and ended waiting.

    Core published no suspension for a turn that cannot write, so nothing ever resumed it
    and the run stalled with every intent complete and the workstream still in review.
    """
    finished = run_waiting(JUDGE_WAITS)

    assert finished.finished
    assert not finished.liveness_violations
    assert finished.core.run.result is not None
    assert finished.core.run.result.outcome == "success"
    assert finished.core.evaluation.continuations != ()


def test_one_of_four_judges_waiting_does_not_hold_the_run_back() -> None:
    """Two workstreams a round for two rounds, and only the third workstream's judge waits."""
    finished = run_waiting(
        replace(
            JUDGE_WAITS,
            rounds=2,
            in_flight=2,
            waits=(False, False, False, False, False, True),
            delays=(0, 3),
            parents=True,
        ),
    )

    assert_ended_closed(finished)


def test_an_implementer_resumed_after_waiting_keeps_the_candidate_it_checkpointed() -> None:
    """The resumed turn only reads the verdict, so the candidate is its suspended turn's."""
    finished = run_waiting(
        replace(JUDGE_WAITS, roles=("implementer",), unchanged_after_first_retain=True)
    )

    assert finished.finished
    assert not finished.liveness_violations
    assert finished.core.run.result is not None
    assert finished.core.run.result.outcome == "success"
    assert finished.core.evaluation.continuations != ()
    (settle,) = [
        item.disposition
        for item in finished.decisions
        if isinstance(item, Withdraw) and isinstance(item.disposition, Settle)
    ]
    assert settle.candidate is not None


@settings(max_examples=10, deadline=None, derandomize=True, database=None)
@given(
    ends=st.lists(st.sampled_from(ENDINGS), min_size=1, max_size=4).map(tuple),
    roles=st.sampled_from((("implementer", "judge"), ("judge",), ("implementer",))),
    polls=st.integers(0, 8),
)
def test_a_suspended_turn_is_a_wait_whatever_reply_it_ends_with(
    ends: tuple[Ending, ...], roles: tuple[str, ...], polls: int
) -> None:
    """Core holds a turn that validated a wait as suspended, so no reply may be accepted.

    A turn that ends with its final result or an invalid reply instead of the waiting reply
    must still yield: accepting or correcting it would request a turn core refuses, which
    halts the run (the simulated run is then not finished). So the strategy's decisions must
    not depend on which reply the suspended turn ended with: they equal those of a run whose
    turns all end with the waiting reply.
    """

    def run(chosen: tuple[Ending, ...]) -> SimulatedRun:
        return run_waiting(replace(JUDGE_WAITS, roles=roles, polls=(polls,), ends=chosen))

    finished = run(ends)

    assert finished.halted is None
    assert_ended_closed(finished)
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
)
def test_a_run_ends_whatever_the_checkpoint_of_a_turn_does(
    checkpoints: tuple[str, ...], polls: int, waits: tuple[bool, ...]
) -> None:
    """A turn that asked to wait is suspended or failed with a reason, never left waiting.

    The implementer waits or not, and its write checkpoint may fail: the judge that depends
    on it is never left without a turn (#2167).
    """
    scenario = replace(JUDGE_WAITS, roles=("implementer", "judge"), polls=(polls,), waits=waits)
    assert_ended_closed(run_waiting(replace(scenario, checkpoints=checkpoints)))


def test_a_declined_checkpoint_fails_the_waiting_turn_instead_of_stalling_the_run() -> None:
    """Every checkpoint fails: the turn cannot be suspended, and the run still ends."""
    finished = run_waiting(replace(JUDGE_WAITS, checkpoints=("failed",)))

    assert finished.finished
    assert not finished.liveness_violations
    assert finished.core.evaluation.continuations == ()
