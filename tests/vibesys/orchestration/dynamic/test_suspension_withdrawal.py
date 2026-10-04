"""Withdraw suspended attempts through public workstream and envelope APIs."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic._support import dynamic_options
from tests.vibesys.orchestration.dynamic.test_plugin_suspension import _open

from vibesys.orchestration.dynamic.input_gate import InputGate
from vibesys.orchestration.dynamic.lifecycle import DispatchIntent, IntentKind
from vibesys.orchestration.dynamic.models import DynamicState, WorkstreamPhase
from vibesys.orchestration.dynamic.rounds import Rounds
from vibesys.orchestration.dynamic.transitions import WithdrawRequested
from vibesys.orchestration.dynamic.workstream import Workstreams
from vibesys.run.dynamic_suspension import EvaluationSuspension

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("withdrawal", [IntentKind.PARK, IntentKind.CANCEL])
def test_withdrawal_while_suspended_preserves_the_charged_attempt(
    tmp_path: Path,
    withdrawal: IntentKind,
) -> None:
    async def scenario() -> None:
        opened = await _open(tmp_path)
        search = opened.start()
        parked = await opened.waiting(search)
        search.cancel()
        with pytest.raises(asyncio.CancelledError):
            await search
        state = await opened.run.state.load(DynamicState)
        assert state is not None
        lock = asyncio.Lock()

        async def commit(label: str) -> None:
            await opened.run.state.commit(state, label=label)

        options = dynamic_options(max_in_flight=1)
        gate = InputGate(opened.runtime, options, state, lock=lock, commit=commit)
        rounds = Rounds(options, state, gate, lock, commit, clock=lambda: 0.0)
        workers = Workstreams(opened.runtime, options, state, rounds, lock, commit)
        shell = EvaluationSuspension(opened.runtime, state, lock, commit)
        opened.evaluations.executor.wait_started.clear()
        task = asyncio.create_task(workers.execute(state.workstreams[0].plan))
        barrier = asyncio.create_task(opened.evaluations.executor.wait_started.wait())
        done, _ = await asyncio.wait({task, barrier}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        await shell.apply(WithdrawRequested(scope_id="held", kind=withdrawal))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        operation_id = f"held/1/{withdrawal.value}"
        await shell.apply(DispatchIntent(operation_id=operation_id))
        await opened.runtime.evaluation.release_jobs("held")
        await opened.evaluations.coordinator.cancel(opened.handle)
        await workers.settle_withdrawn(
            "held", terminal=withdrawal is IntentKind.CANCEL, operation_id=operation_id
        )
        assert state.workstreams[0].budget == parked.workstreams[0].budget
        assert len(opened.calls) == 1
        assert state.workstreams[0].phase is (
            WorkstreamPhase.CANCELLED if withdrawal is IntentKind.CANCEL else WorkstreamPhase.PARKED
        )
        assert len(state.search.rounds) == (1 if withdrawal is IntentKind.CANCEL else 0)
        assert not any(
            intent.kind is IntentKind.RESUME for intent in state.lifecycle.intents.values()
        )
        if withdrawal is IntentKind.PARK:
            report = await opened.evaluations.coordinator.recorded_snapshot(opened.handle)
            opened.evaluation.submitted_reports[opened.handle] = report.model_dump_json()
            continuation_id = next(iter(state.lifecycle.continuations))
            await workers.reopen_evaluation_wait(continuation_id, (opened.handle,))
            assert state.workstreams[0].sequence == parked.workstreams[0].sequence
            assert state.workstreams[0].budget == parked.workstreams[0].budget
            await workers.execute(state.workstreams[0].plan)
            assert len(opened.calls) == 2
            assert len(state.search.rounds) == 1
        await gate.stop()
        opened.client.close()

    asyncio.run(scenario())
