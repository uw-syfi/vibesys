"""Interrupting a running implementer turn to deliver an orchestrator note."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._support import (
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
)
from tests.vibesys.orchestration.dynamic._turn_support import HeldTurns as _HeldTurns

from vibesys.orchestration.dynamic import steers
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.control import Withdrawal
from vibesys.orchestration.dynamic.lifecycle import IntentStage
from vibesys.orchestration.dynamic.models import AgentLoopState, DynamicState, WorkstreamPhase

# test-isolation: the dynamic run is the loop's Workers port; no public entry
# point interrupts a turn until the orchestrator agent's steer tool (step 4) does.
from vibesys.orchestration.dynamic.orchestration import _DynamicRun
from vibesys.orchestration.dynamic.workstream import InterruptResult
from vs_runtime.api import RuntimeContractError

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_runtime.api import AgentRole
    from vs_runtime.api.testing import FakeRun

_NOTE = "A sibling measured 41.0 tok/s with this cache; batch decode first."
_ENDED_EARLY = "Your previous turn was ended early"


def test_an_interrupt_ends_the_turn_keeps_its_work_and_delivers_the_note(tmp_path: Path) -> None:
    """The interrupted turn is refunded once; the next turn resumes with the note.

    A second interrupt while the first lands finds no live turn, and once the
    refund bound is spent the note would wait for the next turn instead.
    """
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("a")],
            IMPLEMENTER.id: [implementation("a"), implementation("a")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def run() -> tuple[_DynamicRun, list[InterruptResult]]:
        held = _HeldTurns(script)
        fake = baseline_run(tmp_path, script, responder=held.respond)
        dynamic = await _DynamicRun.open(fake, dynamic_options(max_in_flight=1))
        dynamic.state.agent = AgentLoopState()
        results = [await dynamic.workstreams.interrupt("a")]
        task = asyncio.create_task(dynamic.search_loop().run(dynamic.recoverable()))
        try:
            assert await held.opened.get() == 1
            steers.enqueue(dynamic.state, "a", _NOTE, at_s=958.0, interrupt=True)
            results.append(await dynamic.workstreams.interrupt("a"))
            results.append(await dynamic.workstreams.interrupt("a"))
            assert await held.opened.get() == 2
            durable = await fake.state.load(DynamicState)
            assert durable is not None
            retained = durable.workstreams[0].candidate_revision
            assert retained is not None
            assert retained in script.calls[-1][2]
            assert durable.workstreams[0].budget.refunded == 1
            results.append(await dynamic.workstreams.interrupt("a"))
            held.release.set()
            await task
        finally:
            await dynamic.input_gate.stop()
        return dynamic, results

    dynamic, results = asyncio.run(run())

    assert results == [
        InterruptResult.NO_LIVE_TURN,
        InterruptResult.INTERRUPTED,
        InterruptResult.NO_LIVE_TURN,
        InterruptResult.REFUNDS_SPENT,
    ]
    item = dynamic.state.workstreams[0]
    assert (item.budget.spent, item.budget.refunded) == (1, 1)
    first, second = [message for role, _, message in script.calls if role == IMPLEMENTER.id]
    assert _NOTE not in first
    assert _NOTE in second
    assert _ENDED_EARLY in second
    assert dynamic.state.agent is not None
    [note] = dynamic.state.agent.steers["a"]
    assert note.delivered_to is not None
    assert note.dropped is None
    assert len(dynamic.state.search.rounds) == 1


def test_provider_cancellation_is_not_an_orchestrator_interrupt(tmp_path: Path) -> None:
    """An independently cancelled provider turn propagates without replacement."""
    script = Script({ORCHESTRATOR.id: [portfolio("a")]})

    def respond(
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        if role.id == IMPLEMENTER.id:
            raise asyncio.CancelledError
        return script.respond(role, history, message, response)

    async def run() -> None:
        fake = baseline_run(tmp_path, script, responder=respond)
        dynamic = await _DynamicRun.open(fake, dynamic_options(max_in_flight=1))
        try:
            with pytest.raises(asyncio.CancelledError):
                await dynamic.search_loop().run(dynamic.recoverable())
        finally:
            await dynamic.input_gate.stop()
        assert dynamic.state.search.rounds == []
        assert dynamic.state.workstreams[0].budget.refunded == 0

    asyncio.run(run())


@settings(max_examples=40, deadline=None)
@example(actions=["steer", "interrupt", "park"])
@example(actions=["steer", "interrupt", "cancel"])
@example(actions=["steer", "interrupt", "crash"])
@given(
    actions=st.lists(
        st.sampled_from(("steer", "interrupt", "park", "cancel", "crash")), min_size=1, max_size=10
    )
)
def test_generated_live_turn_traces_keep_notes_and_intents(actions: list[str]) -> None:
    """Real Fake sessions cross explicit dispatch barriers and durable restarts."""
    with TemporaryDirectory(prefix="turn-trace-") as directory:
        script = Script(
            {
                ORCHESTRATOR.id: [portfolio("a")],
                IMPLEMENTER.id: [implementation("a") for _ in range(len(actions) + 1)],
                JUDGE.id: [{"passed": True, "analysis": "Correct."}],
            }
        )

        async def run() -> None:
            held = _HeldTurns(script)
            fake = baseline_run(Path(directory), script, responder=held.respond)
            dynamic = await _DynamicRun.open(
                fake, dynamic_options(max_in_flight=1, max_retries_per_round=10)
            )
            dynamic.state.agent = AgentLoopState()
            loop = dynamic.search_loop()
            task = asyncio.create_task(loop.run(dynamic.recoverable()))
            await held.opened.get()
            try:
                for number, action in enumerate(actions):
                    if action == "steer":
                        steers.enqueue(
                            dynamic.state,
                            "a",
                            f"Note {number}",
                            at_s=float(number),
                            interrupt=False,
                        )
                        await fake.state.commit(dynamic.state)
                    elif action == "interrupt":
                        assert (
                            await dynamic.workstreams.interrupt("a") is InterruptResult.INTERRUPTED
                        )
                        await held.opened.get()
                    elif action in {"park", "cancel"}:
                        await loop.withdraw("a", Withdrawal(action))
                        await task
                        break
                    else:
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                        await _assert_unknown_recovery_fenced(fake, script)
                        break
                else:
                    held.release.set()
                    await task
            finally:
                await dynamic.input_gate.stop()
            state = await fake.state.load(DynamicState)
            assert state is not None
            assert state.agent is not None
            assert state.workstreams[0].budget.refunded <= 10
            assert len(state.search.rounds) <= 1
            for note in state.agent.steers.get("a", []):
                assert note.delivered_to is None or note.dropped is None
                if note.delivered_to is not None:
                    assert note.delivered_to == note.reserved_to
                    rendered = [
                        message
                        for role, _, message in script.calls
                        if role in {IMPLEMENTER.id, JUDGE.id} and note.text in message
                    ]
                    assert len(rendered) == 1
                if note.delivered_to is None and note.dropped is None:
                    assert state.workstreams[0].phase in {
                        WorkstreamPhase.PARKED,
                        WorkstreamPhase.IMPLEMENTING,
                    }
            active = [
                intent
                for intent in state.lifecycle.intents.values()
                if intent.stage is not IntentStage.COMPLETED
            ]
            assert all(intent.stage is IntentStage.BLOCKED for intent in active)

        asyncio.run(run())


async def _assert_unknown_recovery_fenced(fake: FakeRun, script: Script) -> None:
    calls_before = len(script.calls)
    recovered = await _DynamicRun.open(
        fake, dynamic_options(max_in_flight=1, max_retries_per_round=10)
    )
    try:
        with pytest.raises(RuntimeContractError, match="requires reconciliation"):
            await recovered.search_loop().run(recovered.recoverable())
    finally:
        await recovered.input_gate.stop()
    assert len(script.calls) == calls_before
