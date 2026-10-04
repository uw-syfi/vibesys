"""Withdrawals at explicit implementer, judge and trusted-evaluation barriers."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

import pytest
from tests.vibesys.orchestration.dynamic._support import (
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
)

from vibesys.hypothesis.history import HypothesisOutcome
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.control import Accepted, Withdrawal
from vibesys.orchestration.dynamic.models import WorkstreamPhase

# test-isolation: DynamicRun is the current Workers port; an orchestrator
# service product entrypoint is deferred to the following migration chunk.
from vibesys.orchestration.dynamic.orchestration import _DynamicRun

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


type Barrier = Literal["implementer", "judge", "evaluation"]


async def _withdraw_at(
    tmp_path: Path,
    withdrawal: Withdrawal,
    barrier: Barrier,
) -> tuple[_DynamicRun, list[str], Accepted | object]:
    entered = asyncio.Event()
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("a")],
            IMPLEMENTER.id: [implementation("a")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def respond(
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        target = IMPLEMENTER.id if barrier == "implementer" else JUDGE.id
        if barrier != "evaluation" and role.id == target:
            entered.set()
            await asyncio.Event().wait()
        return script.respond(role, history, message, response)

    run = baseline_run(tmp_path, script, responder=respond)
    evaluation_gate = run.evaluation.gate("benchmark", 1) if barrier == "evaluation" else None
    dynamic = await _DynamicRun.open(run, dynamic_options(max_in_flight=1))
    loop = dynamic.search_loop()
    task = asyncio.create_task(loop.run(dynamic.recoverable()))
    try:
        await (evaluation_gate.entered.wait() if evaluation_gate is not None else entered.wait())
        result = await loop.withdraw("a", withdrawal)
        await task
    finally:
        await dynamic.input_gate.stop()
    return dynamic, run.evaluation.released, result


@pytest.mark.parametrize("withdrawal", list(Withdrawal))
@pytest.mark.parametrize("barrier", ["implementer", "judge", "evaluation"])
def test_a_withdrawn_workstream_settles_once_in_its_phase(
    tmp_path: Path,
    withdrawal: Withdrawal,
    barrier: Barrier,
) -> None:
    dynamic, released, result = asyncio.run(_withdraw_at(tmp_path, withdrawal, barrier))
    assert isinstance(result, Accepted)
    assert released == ["a"]
    item = dynamic.state.workstreams[0]
    rounds = [
        record for record in dynamic.state.search.rounds if record.round_number == item.sequence
    ]
    if withdrawal is Withdrawal.CANCEL:
        assert item.phase is WorkstreamPhase.CANCELLED
        assert len(rounds) == 1
        assert rounds[0].hypothesis_outcome == HypothesisOutcome.INCONCLUSIVE.value
        assert rounds[0].candidate_retained is False
    else:
        assert item.phase is WorkstreamPhase.PARKED
        assert rounds == []
        assert item.budget.spent == (0 if item.implementation is None else 1)
