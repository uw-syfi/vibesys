"""Workstreams whose agents wait for their own evaluations always let the run end.

A fresh implementer or judge turn may submit an evaluation of its own through the
production bridge and end its turn waiting; core resumes it once the job ends. The tests
generate which turns wait, how long each job runs, how long each turn takes (in event-loop
yields), what the judge and the implementer conclude and what the benchmark measures, with
`max_rounds` and `max_in_flight` from small values, and require the run to reach terminal
with every attempt closed and nothing left waiting.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import run_shell
from tests.vibesys.orchestration.dynamic.strategy._waiting_turns import (
    WaitingTurns,
    attach_for,
    new_bridge,
    translator_for,
)

from vs_core.api import AttemptPhase, RunStatus, Settle, Withdraw
from vs_runtime.api.testing import HostOptions

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from vs_runtime.api.testing import Run


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
    ends: tuple[str, ...] = ("wait",)
    """How each waiting turn ends: ``wait``, ``result`` or ``invalid`` (see `WaitingTurns.ends`)."""
    unchanged_after_first_retain: bool = False
    """Whether a resumed turn leaves the worktree as it was (retains nothing of its own)."""


def run_waiting(root: Path, scenario: Scenario) -> Run:
    """A whole run whose turns wait as the scenario says."""
    return run_scripted(root, scenario)[0]


def run_scripted(root: Path, scenario: Scenario) -> tuple[Run, WaitingTurns]:
    """A whole run whose turns wait as the scenario says, and the script that played its agents."""
    bridge = new_bridge(root)
    entries = {
        round_: [
            implement(
                f"h{round_}-{n}",
                **({"parent_hypothesis_id": f"h0-{n}"} if scenario.parents and round_ else {}),
            )
            for n in range(scenario.in_flight)
        ]
        for round_ in range(scenario.rounds + 6)
    }
    script = WaitingTurns(
        planner=deque(plan_reply(*items) for items in entries.values()),
        implementer=deque(
            implemented(scenario.outcomes[n % len(scenario.outcomes)]) for n in range(60)
        ),
        judge=deque(
            reviewed(passed=scenario.verdicts[n % len(scenario.verdicts)]) for n in range(60)
        ),
        decide=lambda n: scenario.waits[n % len(scenario.waits)],
        delay=lambda n: scenario.delays[n % len(scenario.delays)],
        polls=list(scenario.polls),
        roles=scenario.roles,
        ends=lambda n: scenario.ends[n % len(scenario.ends)],
        submit_only=scenario.submit_only,
        unchanged_after_first_retain=scenario.unchanged_after_first_retain,
        benchmark=lambda commit: scenario.throughput[
            int(commit[:4], 16) % len(scenario.throughput)
        ],
    )
    host = HostOptions(
        max_concurrent=scenario.in_flight + 2,
        translator=translator_for(script, bridge),
        attach=attach_for(script, bridge),
        controlled=True,
        lifecycle=frozenset({"suspend"}),
    )
    finished = run_shell(
        script, host=host, max_rounds=scenario.rounds, max_in_flight=scenario.in_flight
    )
    return finished, script


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
def test_a_run_ends_whatever_its_agents_wait_for(
    tmp_path_factory: pytest.TempPathFactory, scenario: Scenario
) -> None:
    finished = run_waiting(tmp_path_factory.mktemp("waits"), scenario)

    assert finished.core.run.status == RunStatus.TERMINAL
    for attempt in finished.core.attempts.attempts:
        assert attempt.phase == AttemptPhase.TERMINAL


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


def test_a_judge_that_waits_for_its_own_evaluation_is_resumed_and_the_run_adopts(
    tmp_path: Path,
) -> None:
    """The v12 stall: the judge, a read-only turn, submitted an evaluation and ended waiting.

    Core published no suspension for a turn that cannot write, so nothing ever resumed it
    and the run stalled with every intent complete and the workstream still in review.
    """
    finished = run_waiting(tmp_path, JUDGE_WAITS)

    assert finished.core.run.status == RunStatus.TERMINAL
    assert finished.core.run.result is not None
    assert finished.core.run.result.outcome == "success"
    assert finished.core.evaluation.continuations != ()


def test_one_of_four_judges_waiting_does_not_hold_the_run_back(tmp_path: Path) -> None:
    """Two workstreams a round for two rounds, and only the third workstream's judge waits."""
    finished = run_waiting(
        tmp_path,
        replace(
            JUDGE_WAITS,
            rounds=2,
            in_flight=2,
            waits=(False, False, False, False, False, True),
            delays=(0, 3),
            parents=True,
        ),
    )

    assert finished.core.run.status == RunStatus.TERMINAL
    assert all(a.phase == AttemptPhase.TERMINAL for a in finished.core.attempts.attempts)


def test_an_implementer_resumed_after_waiting_keeps_the_candidate_it_checkpointed(
    tmp_path: Path,
) -> None:
    """The resumed turn only reads the verdict, so the candidate is its suspended turn's."""
    finished = run_waiting(
        tmp_path,
        replace(JUDGE_WAITS, roles=("implementer",), unchanged_after_first_retain=True),
    )

    assert finished.core.run.status == RunStatus.TERMINAL
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
    ends=st.lists(st.sampled_from(("wait", "result", "invalid")), min_size=1, max_size=4).map(
        tuple
    ),
    roles=st.sampled_from((("implementer", "judge"), ("judge",), ("implementer",))),
    polls=st.integers(0, 8),
)
def test_a_suspended_turn_is_a_wait_whatever_reply_it_ends_with(
    tmp_path_factory: pytest.TempPathFactory,
    ends: tuple[str, ...],
    roles: tuple[str, ...],
    polls: int,
) -> None:
    """Core holds a turn that validated a wait as suspended, so no reply may be accepted.

    A turn that ends with its final result or an invalid reply instead of the waiting reply
    must still yield: accepting or correcting it would request a turn core refuses, which
    halts the run (`run_shell` raises the halt). So the strategy's decisions must not depend on
    which reply the suspended turn ended with: they equal those of a run whose turns all end
    with the waiting reply.
    """

    def run(chosen: tuple[str, ...]) -> Run:
        return run_waiting(
            tmp_path_factory.mktemp("ends"),
            Scenario(
                rounds=1,
                in_flight=1,
                waits=(True,),
                polls=(polls,),
                delays=(0,),
                verdicts=(True,),
                outcomes=("supported",),
                throughput=(100.0,),
                roles=roles,
                ends=chosen,
            ),
        )

    finished = run(ends)

    assert finished.core.run.status == RunStatus.TERMINAL
    for attempt in finished.core.attempts.attempts:
        assert attempt.phase == AttemptPhase.TERMINAL
    if "invalid" not in ends:
        # Whether core records a wait on an invalid reply is core's own rule, so only the other
        # replies are compared.
        assert finished.core.evaluation.continuations != ()
        assert [type(item).__name__ for item in finished.decisions] == [
            type(item).__name__ for item in run(("wait",)).decisions
        ]
