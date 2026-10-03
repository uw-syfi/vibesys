"""Parking and cancelling a running workstream at any point of its life."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic._support import (
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
)

from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.control import (
    Accepted,
    Withdrawal,
)
from vibesys.orchestration.dynamic.models import WorkstreamPhase

# test-isolation: the dynamic run is the loop's Workers port; no public entry
# point withdraws a workstream until the orchestrator agent (step 5) does.
from vibesys.orchestration.dynamic.orchestration import _DynamicRun
from vs_loop_state.api import HypothesisOutcome

if TYPE_CHECKING:
    from pathlib import Path

_MAX_YIELDS = 10_000


def _script() -> Script:
    return Script(
        {
            ORCHESTRATOR.id: [portfolio("a")],
            IMPLEMENTER.id: [implementation("a")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )


async def _withdraw_after(
    tmp_path: Path, withdrawal: Withdrawal, yields: int
) -> tuple[_DynamicRun, list[str], Accepted | object]:
    run = baseline_run(tmp_path, _script())
    dynamic = await _DynamicRun.open(run, dynamic_options(max_in_flight=1))
    loop = dynamic.search_loop()
    task = asyncio.create_task(loop.run(dynamic.recoverable()))
    try:
        for _ in range(_MAX_YIELDS):
            if "a" in loop.core.running:
                break
            await asyncio.sleep(0)
        for _ in range(yields):
            await asyncio.sleep(0)
        # The search may already have ended: nothing is left to withdraw.
        result = None if task.done() else await loop.withdraw("a", withdrawal)
        await task
    finally:
        await dynamic.input_gate.stop()
    return dynamic, run.evaluation.released, result


@pytest.mark.parametrize("withdrawal", list(Withdrawal))
@pytest.mark.parametrize("yields", range(20))
def test_a_withdrawn_workstream_settles_once_in_its_phase(
    tmp_path: Path, withdrawal: Withdrawal, yields: int
) -> None:
    """Park or cancel at any point: it releases its jobs and settles once.

    Only a cancel records a round.
    """
    dynamic, released, result = asyncio.run(_withdraw_after(tmp_path, withdrawal, yields))

    # Exactly one release per accepted withdrawal, none otherwise.
    assert released == (["a"] if isinstance(result, Accepted) else [])

    state = dynamic.state
    item = state.workstreams[0]
    rounds = [record for record in state.search.rounds if record.round_number == item.sequence]
    if not isinstance(result, Accepted):
        # The attempt finished first and keeps its own result.
        assert len(rounds) == 1
        return
    if rounds and item.phase not in {WorkstreamPhase.CANCELLED, WorkstreamPhase.PARKED}:
        # The attempt recorded its round before the withdrawal landed.
        assert len(rounds) == 1
        return
    if withdrawal is Withdrawal.CANCEL:
        assert item.phase is WorkstreamPhase.CANCELLED
        assert len(rounds) == 1
        assert rounds[0].hypothesis_outcome == HypothesisOutcome.INCONCLUSIVE.value
    else:
        assert item.phase is WorkstreamPhase.PARKED
        assert rounds == []
        # A parked turn is refunded: the orchestrator, not the attempt, ended it.
        assert item.budget.spent == (0 if item.implementation is None else 1)
