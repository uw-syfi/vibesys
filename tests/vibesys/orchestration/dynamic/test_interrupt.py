"""Interrupting a running implementer turn to deliver an orchestrator note."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic._support import (
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
)

from vibesys.orchestration.dynamic import steers
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.models import AgentLoopState

# test-isolation: the dynamic run is the loop's Workers port; no public entry
# point interrupts a turn until the orchestrator agent's steer tool (step 4) does.
from vibesys.orchestration.dynamic.orchestration import _DynamicRun
from vibesys.orchestration.dynamic.workstream import InterruptResult

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole

_NOTE = "A sibling measured 41.0 tok/s with this cache; batch decode first."
_ENDED_EARLY = "Your previous turn was ended early"


class _HeldTurns:
    """Hold every implementer turn open until released, as a long provider turn is."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.opened: asyncio.Queue[int] = asyncio.Queue()
        self.release = asyncio.Event()
        self._turns = 0

    def respond(
        self,
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        reply = self.script.respond(role, history, message, response)
        if role.id != IMPLEMENTER.id:
            return reply
        self._turns += 1
        self.opened.put_nowait(self._turns)

        async def held() -> object:
            await self.release.wait()
            return reply

        return held()


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
        results = [dynamic.workstreams.interrupt("a")]
        task = asyncio.create_task(dynamic.search_loop().run(dynamic.recoverable()))
        try:
            assert await held.opened.get() == 1
            steers.enqueue(dynamic.state, "a", _NOTE, at_s=958.0, interrupt=True)
            results.append(dynamic.workstreams.interrupt("a"))
            results.append(dynamic.workstreams.interrupt("a"))
            assert await held.opened.get() == 2
            results.append(dynamic.workstreams.interrupt("a"))
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
