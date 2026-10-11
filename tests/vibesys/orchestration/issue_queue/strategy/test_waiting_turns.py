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
