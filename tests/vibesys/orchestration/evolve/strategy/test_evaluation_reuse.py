"""An evolve judge that asks to measure the candidate its mutator measured gets that evaluation.

Run on the production bridge and core, with scripted agents. The mutator submits an evaluation
of its offspring and ends its turn with the job still running; the judge, a read-only turn on the
same offspring, asks for the same candidate. The run admits one evaluation per exact candidate and
scope, so the judge is handed the mutator's handle and may wait on it. Before the fix the judge was
refused for budget, and the port removed the judge's evaluation tool to avoid the failed verdicts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.evolve.strategy.world import EvolveWorld, run_to_end

from vibesys.evolve_roles import JUDGE, MUTATOR
from vs_core.api import AttemptPhase, Settle, Withdraw

if TYPE_CHECKING:
    from vs_dst.api import SimulatedRun


def _run(
    *, polls: tuple[int, ...], waits: tuple[bool, ...], parallelism: int
) -> tuple[SimulatedRun, EvolveWorld]:
    """A run whose mutators submit and leave the job running, and whose judges may wait."""
    world = EvolveWorld(
        wait_roles=("judge",),
        submit_only=("mutator",),
        waits=waits,
        polls=polls,
        max_generations=1,
        children_per_generation=2,
        max_parallelism=parallelism,
    )
    return run_to_end(world), world


def _check(run: SimulatedRun, world: EvolveWorld) -> None:
    assert all(a.phase == AttemptPhase.TERMINAL for a in run.core.attempts.attempts)
    assert all("handle_id" in reply for _, _, reply in world.asked), world.asked
    by_attempt: dict[str, list[tuple[str, dict]]] = {}
    for attempt, role, reply in world.asked:
        by_attempt.setdefault(attempt, []).append((role, reply))
    for asks in by_attempt.values():
        assert len({reply["handle_id"] for _, reply in asks}) == 1, "one candidate, one handle"
        assert asks[0][1]["repeat"] is False
        assert all(reply["repeat"] for _, reply in asks[1:])


def test_the_judge_has_the_evaluation_tool_the_design_table_gives_it() -> None:
    assert [tool.id for tool in JUDGE.extra_tools] == [tool.id for tool in MUTATOR.extra_tools]


def test_a_judge_asking_for_the_mutators_evaluation_gets_its_handle() -> None:
    run, world = _run(polls=(3,), waits=(True,), parallelism=1)

    _check(run, world)
    assert any(role.endswith("judge") and reply["repeat"] for _, role, reply in world.asked)
    settles = [
        item.disposition
        for item in run.decisions
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
    polls: tuple[int, ...], waits: tuple[bool, ...], parallelism: int
) -> None:
    run, world = _run(polls=polls, waits=waits, parallelism=parallelism)

    _check(run, world)
