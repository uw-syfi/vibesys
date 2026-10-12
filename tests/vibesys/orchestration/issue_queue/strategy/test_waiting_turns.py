"""Turns that wait for their own evaluation always let the run end.

A fresh implementer or judge turn may submit an evaluation through the production bridge and end
its turn waiting; core resumes it once the job ends. The tests generate which turns wait, how
long each turn takes (in event-loop yields) and what the judge concludes, and require the run to
reach the result a run without waits reaches, with every attempt closed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.issue_queue.strategy._replies import evaluated, implemented, judged
from tests.vibesys.orchestration.issue_queue.strategy._run import run_shell
from tests.vibesys.orchestration.issue_queue.strategy._waiting import (
    WaitingTurns,
    attach_for,
    new_bridge,
    translator_for,
)

from vs_core.api import AttemptPhase, RunStatus
from vs_runtime.api.testing import HostOptions

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from vs_runtime.api.testing import Run


@dataclass(frozen=True)
class Scenario:
    """One generated run: which turns wait, how long each takes, and what the judge says."""

    waits: tuple[bool, ...]
    delays: tuple[int, ...]
    verdicts: tuple[bool, ...]
    roles: tuple[str, ...] = ("implementer", "judge")
    ends: tuple[str, ...] = ("wait",)
    polls: int = 0


def run_waiting(root: Path, scenario: Scenario) -> Run:
    """A whole run whose turns wait as the scenario says."""
    bridge = new_bridge(root)
    script = WaitingTurns(
        implementer=deque(implemented(f"turn {n}") for n in range(30)),
        judge=deque(
            judged(passed=scenario.verdicts[n % len(scenario.verdicts)]) for n in range(30)
        ),
        perf=deque(evaluated() for _ in range(6)),
        decide=lambda n: scenario.waits[n % len(scenario.waits)],
        delay=lambda n: scenario.delays[n % len(scenario.delays)],
        roles=scenario.roles,
        ends=lambda n: scenario.ends[n % len(scenario.ends)],
        polls=scenario.polls,
    )
    host = HostOptions(
        translator=translator_for(script, bridge),
        attach=attach_for(script, bridge),
        controlled=True,
        lifecycle=frozenset({"suspend"}),
    )
    return run_shell(script, host=host, max_attempts_per_issue=2)


SCENARIOS = st.builds(
    Scenario,
    waits=st.lists(st.booleans(), min_size=1, max_size=6).map(tuple),
    delays=st.lists(st.integers(0, 6), min_size=1, max_size=4).map(tuple),
    verdicts=st.lists(st.booleans(), min_size=1, max_size=4).map(tuple),
    roles=st.sampled_from((("implementer", "judge"), ("judge",), ("implementer",))),
)


@settings(max_examples=10, deadline=None, derandomize=True, database=None)
@given(scenario=SCENARIOS)
def test_a_run_ends_whatever_its_agents_wait_for(
    tmp_path_factory: pytest.TempPathFactory, scenario: Scenario
) -> None:
    finished = run_waiting(tmp_path_factory.mktemp("w"), scenario)

    assert finished.core.run.status == RunStatus.TERMINAL
    for attempt in finished.core.attempts.attempts:
        assert attempt.phase == AttemptPhase.TERMINAL


def test_a_judge_that_waits_for_its_own_evaluation_is_resumed_and_the_run_adopts(
    tmp_path: Path,
) -> None:
    finished = run_waiting(
        tmp_path, Scenario(waits=(True,), delays=(0,), verdicts=(True,), roles=("judge",))
    )

    assert finished.core.run.status == RunStatus.TERMINAL
    assert finished.core.run.result is not None
    assert finished.core.run.result.outcome == "success"
    assert finished.core.evaluation.continuations != ()


def test_an_implementer_that_waits_for_its_own_evaluation_is_resumed_and_its_issue_closes(
    tmp_path: Path,
) -> None:
    finished = run_waiting(
        tmp_path, Scenario(waits=(True,), delays=(0,), verdicts=(True,), roles=("implementer",))
    )

    assert finished.core.run.result is not None
    assert finished.core.run.result.outcome == "success"
    assert finished.core.evaluation.continuations != ()


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
    tmp_path_factory: pytest.TempPathFactory,
    ends: tuple[str, ...],
    roles: tuple[str, ...],
    verdicts: tuple[bool, ...],
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
                waits=(True,), delays=(0,), verdicts=verdicts, roles=roles, ends=chosen, polls=polls
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
