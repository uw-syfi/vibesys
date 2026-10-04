"""Recovery after external release succeeds but settlement storage fails."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel
from tests.vibesys.orchestration.dynamic._support import (
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
)
from tests.vibesys.orchestration.dynamic.test_lifecycle_regressions import _evaluated_state

from vibesys.orchestration.dynamic import DynamicState
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.control import Withdrawal
from vibesys.orchestration.dynamic.lifecycle import (
    IntentKind,
    IntentStage,
    LifecycleIntent,
    LifecycleState,
)
from vibesys.orchestration.dynamic.models import DurableStateCommitError, WorkstreamPhase

# test-isolation: exercises the dynamic run's Workers port before service migration.
from vibesys.orchestration.dynamic.orchestration import _DynamicRun
from vibesys.orchestration.dynamic.transitions import AlreadySettledError
from vs_runtime.api import Run, RuntimeContractError
from vs_runtime.api.testing import FakeState

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api import AgentRole


class _SettlementWriteError(RuntimeError):
    """A scheduled failure of the envelope commit following external release."""


class _UnreadableState(FakeState):
    """Fail the explicit reload barrier without changing commit semantics."""

    fail_reload: bool = False

    async def load[ModelT: BaseModel](self, model: type[ModelT]) -> ModelT | None:
        if self.fail_reload:
            self.fail_reload = False
            message = "durable reload unavailable"
            raise OSError(message)
        return await super().load(model)


def test_write_and_reload_failure_remains_fatal_before_release(tmp_path: Path) -> None:
    async def scenario() -> None:
        base = baseline_run(tmp_path, Script({}))
        state = _UnreadableState(DynamicState, base.workspaces.root)
        run = Run(
            run_id=base.run_id,
            facts=base.facts,
            agents=base.agents,
            workspaces=base.workspaces,
            evaluation=base.evaluation,
            state=state,
            control=base.control,
            commands=base.commands,
            skills=base.skills,
            observations=base.observations,
        )
        await state.commit(_evaluated_state())
        dynamic = await _DynamicRun.open(run, dynamic_options(max_in_flight=1))
        state.script_commit_at("dynamic: prepare kept/1/cancel", OSError("write unavailable"))
        state.fail_reload = True
        with pytest.raises(DurableStateCommitError, match="prepare kept/1/cancel"):
            await dynamic.settle(dynamic.state.workstreams[0].plan, Withdrawal.CANCEL)
        assert base.evaluation.released == []
        durable = await state.load(DynamicState)
        assert durable is not None
        assert durable.lifecycle.intents == {}
        await dynamic.input_gate.stop()

    asyncio.run(scenario())


def test_cancelled_candidate_cannot_be_reused_as_a_continuation_parent(tmp_path: Path) -> None:
    async def scenario() -> None:
        script = Script(
            {
                ORCHESTRATOR.id: [
                    portfolio("kept", continue_hypothesis=True),
                    portfolio("new"),
                ],
                IMPLEMENTER.id: [implementation("new")],
                JUDGE.id: [{"passed": True, "analysis": "New candidate is correct."}],
            }
        )
        run = baseline_run(tmp_path, script)
        await run.state.commit(_evaluated_state())
        dynamic = await _DynamicRun.open(run, dynamic_options(max_in_flight=1, max_rounds=2))
        await dynamic.settle(dynamic.state.workstreams[0].plan, Withdrawal.CANCEL)
        await dynamic.search_loop().run(dynamic.recoverable())
        cancelled = dynamic.state.workstreams[0]
        assert cancelled.phase is WorkstreamPhase.CANCELLED
        assert cancelled.sequence == 1
        assert dynamic.state.workstreams[1].parent_revision != cancelled.candidate_revision
        planners = [message for role, _, message in script.calls if role == ORCHESTRATOR.id]
        assert len(planners) == 2
        assert "cannot be continued" in planners[1]
        assert [role for role, _, _ in script.calls].count(IMPLEMENTER.id) == 1
        await dynamic.input_gate.stop()

    asyncio.run(scenario())


def test_restart_replays_released_but_unsettled_withdrawal(tmp_path: Path) -> None:
    async def scenario() -> None:
        run = baseline_run(tmp_path, Script({}))
        await run.state.commit(_evaluated_state())
        options = dynamic_options(max_in_flight=1)
        dynamic = await _DynamicRun.open(run, options)
        item = dynamic.state.workstreams[0]
        run.state.script_commit_at(
            "dynamic: record hypothesis kept", _SettlementWriteError("settlement commit")
        )
        with pytest.raises(DurableStateCommitError):
            await dynamic.settle(item.plan, Withdrawal.CANCEL)
        durable = await run.state.load(DynamicState)
        assert durable is not None
        assert durable.workstreams[0].phase is WorkstreamPhase.EVALUATED
        assert run.evaluation.released == [item.hypothesis_id]
        recovered = await _DynamicRun.open(run, options)
        assert recovered.recoverable() == ()
        assert recovered.state.workstreams[0].phase is WorkstreamPhase.CANCELLED
        assert len(recovered.state.search.rounds) == 1
        assert run.evaluation.released == [item.hypothesis_id, item.hypothesis_id]
        await dynamic.input_gate.stop()
        await recovered.input_gate.stop()

    asyncio.run(scenario())


def test_version_7_cancellation_without_a_round_recovers_settlement(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = _evaluated_state()
        state.workstreams[0].phase = WorkstreamPhase.CANCELLED
        encoded = state.model_dump(mode="json")
        encoded["schema_version"] = 7
        encoded.pop("lifecycle")
        for item in encoded["workstreams"]:
            item.pop("invocation_sequence")
        migrated = DynamicState.model_validate_json(json.dumps(encoded), strict=True)
        run = baseline_run(tmp_path, Script({}))
        await run.state.commit(migrated)
        dynamic = await _DynamicRun.open(run, dynamic_options(max_in_flight=1))
        assert dynamic.recoverable() == ()
        assert len(dynamic.state.search.rounds) == 1
        assert dynamic.state.search.rounds[0].candidate_retained is False
        assert run.evaluation.released == ["kept"]
        await dynamic.input_gate.stop()

    asyncio.run(scenario())


def test_legacy_released_scope_is_cleaned_but_unknown_disposition_is_blocked(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        state = _evaluated_state()
        state.workstreams[0].phase = WorkstreamPhase.IMPLEMENTING
        encoded = state.model_dump(mode="json")
        encoded["schema_version"] = 7
        encoded.pop("lifecycle")
        for item in encoded["workstreams"]:
            item.pop("invocation_sequence")
        legacy = DynamicState.model_validate_json(json.dumps(encoded), strict=True)
        run = baseline_run(tmp_path, Script({}))
        await run.state.commit(legacy)
        await run.evaluation.release_jobs("kept")
        with pytest.raises(RuntimeContractError, match="kept/1/park"):
            await _DynamicRun.open(run, dynamic_options(max_in_flight=1))
        durable = await run.state.load(DynamicState)
        assert durable is not None
        assert durable.workstreams[0].phase is WorkstreamPhase.PARKED
        assert durable.workstreams[0].candidate_revision == state.workstreams[0].candidate_revision
        assert durable.search.rounds == []
        assert durable.lifecycle.intents["kept/1/park"].stage is IntentStage.BLOCKED
        assert run.evaluation.released == ["kept", "kept"]

    asyncio.run(scenario())


@pytest.mark.parametrize("first", ["round", "withdrawal"])
def test_committed_event_order_decides_settlement_race(tmp_path: Path, first: str) -> None:
    async def scenario() -> None:
        run = baseline_run(tmp_path, Script({}))
        await run.state.commit(_evaluated_state())
        dynamic = await _DynamicRun.open(run, dynamic_options(max_in_flight=1))
        item = dynamic.state.workstreams[0]
        if first == "round":
            await dynamic.rounds.record(0)
            assert not dynamic.can_withdraw(item.hypothesis_id)
            with pytest.raises(AlreadySettledError):
                await dynamic.withdraw(item.plan, Withdrawal.CANCEL)
            assert run.evaluation.released == []
            assert dynamic.state.workstreams[0].phase is WorkstreamPhase.EVALUATED
            assert len(dynamic.state.search.rounds) == 1
            assert dynamic.state.lifecycle.intents == {}
        else:
            await dynamic.withdraw(item.plan, Withdrawal.CANCEL)
            # Ordinary completion observes withdrawal authority and cannot
            # publish an accepted round while cleanup is unfinished.
            await dynamic.rounds.record(0)
            assert dynamic.state.search.rounds == []
            await dynamic.settle(item.plan, Withdrawal.CANCEL)
            assert dynamic.state.workstreams[0].phase is WorkstreamPhase.CANCELLED
            assert len(dynamic.state.search.rounds) == 1
            assert dynamic.state.search.rounds[0].candidate_retained is False
        await dynamic.input_gate.stop()

    asyncio.run(scenario())


def test_external_withdrawal_write_failure_aborts_and_drains_all_live_workers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        running: set[str] = set()
        cancelled: set[str] = set()
        script = Script({ORCHESTRATOR.id: [portfolio("a", "b")]})
        turns: list[str] = []

        async def respond(
            role: AgentRole,
            history: tuple[str, ...],
            message: str,
            response: type[BaseModel] | None,
        ) -> object:
            turns.append(role.id)
            if role.id != IMPLEMENTER.id:
                return script.respond(role, history, message, response)
            identifier = "a" if "`a`" in message else "b"
            running.add(identifier)
            if running == {"a", "b"}:
                entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.add(identifier)
                raise

        run = baseline_run(tmp_path, script, responder=respond)
        dynamic = await _DynamicRun.open(run, dynamic_options(max_in_flight=2))
        loop = dynamic.search_loop()
        task = asyncio.create_task(loop.run(dynamic.recoverable()))
        await entered.wait()
        run.state.script_commit_at(
            "dynamic: prepare a/1/cancel", _SettlementWriteError("withdraw intent")
        )
        with pytest.raises(DurableStateCommitError):
            await loop.withdraw("a", Withdrawal.CANCEL)
        with pytest.raises(DurableStateCommitError):
            await task
        assert cancelled == {"a", "b"}
        assert turns.count(ORCHESTRATOR.id) == 1
        assert turns.count(IMPLEMENTER.id) == 2
        assert JUDGE.id not in turns
        durable = await run.state.load(DynamicState)
        assert durable is not None
        assert durable.lifecycle.intents == {}
        assert dynamic.state == durable
        await dynamic.input_gate.stop()

    asyncio.run(scenario())


def test_stale_withdrawal_recovery_cannot_release_a_newer_generation(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = _evaluated_state()
        state.workstreams[0].sequence = 2
        old = LifecycleIntent(
            operation_id="kept/1/cancel",
            scope_id="kept",
            generation=1,
            kind=IntentKind.CANCEL,
        )
        state.lifecycle = LifecycleState(intents={old.operation_id: old})
        run = baseline_run(tmp_path, Script({}))
        await run.state.commit(state)
        with pytest.raises(RuntimeContractError, match="kept/1/cancel"):
            await _DynamicRun.open(run, dynamic_options(max_in_flight=1))
        durable = await run.state.load(DynamicState)
        assert durable is not None
        assert durable.workstreams[0].sequence == 2
        assert durable.workstreams[0].phase is WorkstreamPhase.EVALUATED
        assert durable.lifecycle.intents[old.operation_id].stage is IntentStage.BLOCKED
        assert run.evaluation.released == []
        assert durable.search.rounds == []

    asyncio.run(scenario())
