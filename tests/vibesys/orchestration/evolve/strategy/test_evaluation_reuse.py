"""An evolve judge that asks to measure the candidate its mutator measured gets that evaluation.

Run on the production bridge and core, with scripted agents. The mutator submits an evaluation
of its offspring and ends its turn with the job still running; the judge, a read-only turn on the
same offspring, asks for the same candidate. The run admits one evaluation per exact candidate and
scope, so the judge is handed the mutator's handle and may wait on it. Before the fix the judge was
refused for budget, and the port removed the judge's evaluation tool to avoid the failed verdicts.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.evolve.strategy._replies import mutated, reviewed
from tests.vibesys.orchestration.evolve.strategy._run import run_shell
from tests.vibesys.orchestration.evolve.strategy._waiting_turns import (
    WaitingTurns,
    attach_for,
    new_bridge,
    translator_for,
)

from vibesys.evolve_roles import JUDGE, MUTATOR
from vs_core.api import AttemptPhase, RunStatus, Settle, Withdraw
from vs_runtime.api.testing import HostOptions

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from vs_runtime.api.testing import Run


def _run(
    root: Path, *, polls: tuple[int, ...], waits: tuple[bool, ...], parallelism: int
) -> tuple[Run, WaitingTurns]:
    """A run whose mutators submit and leave the job running, and whose judges may wait."""
    bridge = new_bridge(root)
    script = WaitingTurns(
        mutator=deque(mutated() for _ in range(40)),
        judge=deque(reviewed() for _ in range(40)),
        roles=("judge",),
        submit_only=("mutator",),
        decide=lambda n: waits[n % len(waits)],
        polls=list(polls),
    )
    host = HostOptions(
        max_concurrent=2,
        translator=translator_for(script, bridge),
        attach=attach_for(script, bridge),
        controlled=True,
        lifecycle=frozenset({"suspend"}),
    )
    finished = run_shell(
        script,
        host=host,
        max_generations=1,
        children_per_generation=2,
        max_parallelism=parallelism,
    )
    return finished, script


def _check(finished: Run, script: WaitingTurns) -> None:
    assert finished.core.run.status == RunStatus.TERMINAL
    assert all(a.phase == AttemptPhase.TERMINAL for a in finished.core.attempts.attempts)
    assert all("handle_id" in reply for _, _, reply in script.asked), script.asked
    by_attempt: dict[str, list[tuple[str, dict]]] = {}
    for attempt, role, reply in script.asked:
        by_attempt.setdefault(attempt, []).append((role, reply))
    for asks in by_attempt.values():
        assert len({reply["handle_id"] for _, reply in asks}) == 1, "one candidate, one handle"
        assert asks[0][1]["repeat"] is False
        assert all(reply["repeat"] for _, reply in asks[1:])


def test_the_judge_has_the_evaluation_tool_the_design_table_gives_it() -> None:
    assert [tool.id for tool in JUDGE.extra_tools] == [tool.id for tool in MUTATOR.extra_tools]


def test_a_judge_asking_for_the_mutators_evaluation_gets_its_handle(tmp_path: Path) -> None:
    finished, script = _run(tmp_path, polls=(3,), waits=(True,), parallelism=1)

    _check(finished, script)
    assert any(role.endswith("judge") and reply["repeat"] for _, role, reply in script.asked)
    settles = [
        item.disposition
        for item in finished.decisions
        if isinstance(item, Withdraw) and isinstance(item.disposition, Settle)
    ]
    assert settles
    assert all(item.candidate is not None for item in settles), "every offspring was measured"


@settings(max_examples=6, deadline=None, derandomize=True, database=None)
@given(
    polls=st.lists(st.integers(0, 4), min_size=1, max_size=3).map(tuple),
    waits=st.lists(st.booleans(), min_size=1, max_size=3).map(tuple),
    parallelism=st.integers(1, 2),
)
def test_judges_and_mutators_share_one_evaluation_whatever_runs_when(
    tmp_path_factory: pytest.TempPathFactory,
    polls: tuple[int, ...],
    waits: tuple[bool, ...],
    parallelism: int,
) -> None:
    finished, script = _run(
        tmp_path_factory.mktemp("reuse"), polls=polls, waits=waits, parallelism=parallelism
    )

    _check(finished, script)
