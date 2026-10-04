"""Acceptance regressions that also run unchanged on PR head 09b5af13."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic._support import (
    Script,
    baseline_run,
    dynamic_options,
    portfolio,
)

from vibesys.hypothesis import CandidateDisposition
from vibesys.orchestration.dynamic import PLUGIN, DynamicState
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, ORCHESTRATOR
from vibesys.orchestration.dynamic.control import Withdrawal
from vibesys.orchestration.dynamic.models import PortfolioPlan, WorkstreamPhase

# test-isolation: DynamicRun is the current public Workers port; agent actions
# have no product entrypoint until the following orchestrator service chunk.
from vibesys.orchestration.dynamic.orchestration import _DynamicRun

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


def test_p1_independent_provider_cancellation_propagates(tmp_path: Path) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("a")],
            IMPLEMENTER.id: [asyncio.CancelledError()],
        }
    )
    run = baseline_run(tmp_path, script)

    async def scenario() -> None:
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())


def test_p1_withdrawal_is_durable_before_provider_cancellation(tmp_path: Path) -> None:
    async def scenario() -> None:
        entered, cancelled, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        script = Script({ORCHESTRATOR.id: [portfolio("a")]})

        async def respond(
            role: AgentRole,
            history: tuple[str, ...],
            message: str,
            response: type[BaseModel] | None,
        ) -> object:
            if role.id != IMPLEMENTER.id:
                return script.respond(role, history, message, response)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await finish.wait()
                raise

        run = baseline_run(tmp_path, script, responder=respond)
        options = dynamic_options(max_in_flight=1)
        dynamic = await _DynamicRun.open(run, options)
        loop = dynamic.search_loop()
        task = asyncio.create_task(loop.run(dynamic.recoverable()))
        try:
            await entered.wait()
            await loop.withdraw("a", Withdrawal.CANCEL)
            await cancelled.wait()
            durable = await run.state.load(DynamicState)
            assert durable is not None
            # Simulate a crashed host reading the last acknowledged envelope,
            # while the far-side provider has not finished cancellation.
            restored = _DynamicRun(run, options, durable, asyncio.Lock(), _can_profile=False)
            assert restored.recoverable() == (), "withdrawn work resumed as ordinary execution"
        finally:
            finish.set()
            await task
            await dynamic.input_gate.stop()

    asyncio.run(scenario())


def _evaluated_state() -> DynamicState:
    encoded = json.loads((Path(__file__).parent / "fixtures/state_v6/completed.json").read_text())
    for hypothesis in encoded["search"]["hypotheses"]:
        hypothesis["rounds"] = []
        hypothesis["declared_outcome"] = None
        hypothesis["resolution"] = None
        hypothesis["candidate_retained"] = None
    return DynamicState.model_validate_json(json.dumps(encoded))


def test_p1_cancelled_accepted_candidate_cannot_be_adopted(tmp_path: Path) -> None:
    async def scenario() -> None:
        run = baseline_run(tmp_path, Script({}))
        state = _evaluated_state()
        await run.state.commit(state)
        dynamic = await _DynamicRun.open(run, dynamic_options(max_in_flight=1))
        try:
            item = dynamic.state.workstreams[0]
            await dynamic.settle(item.plan, Withdrawal.CANCEL)
            assert dynamic.state.workstreams[0].phase is WorkstreamPhase.CANCELLED
            (record,) = dynamic.state.search.rounds
            assert record.candidate_disposition == CandidateDisposition.DISCARD.value
            assert record.candidate_retained is False
            assert dynamic.rounds.winner() is None
            assert dynamic.rounds.buildable() == ()
        finally:
            await dynamic.input_gate.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [RuntimeError(), RuntimeError("   "), asyncio.CancelledError()])
def test_workstream_failure_always_logs_a_nonempty_reason(
    tmp_path: Path, error: BaseException
) -> None:
    async def scenario() -> None:
        script = Script({ORCHESTRATOR.id: [portfolio("a")]})
        run = baseline_run(tmp_path, script)
        dynamic = await _DynamicRun.open(run, dynamic_options(max_in_flight=1))
        try:
            plan = PortfolioPlan.model_validate(portfolio("a")).workstreams[0]
            await dynamic.classify(plan, error)
            failures = [
                call.message for call in run.observations.calls if "failed:" in call.message
            ]
            assert failures
            assert all(message.split("failed:", 1)[1].strip() for message in failures)
        finally:
            await dynamic.input_gate.stop()

    asyncio.run(scenario())
