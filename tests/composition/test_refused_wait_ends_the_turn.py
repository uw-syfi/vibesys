"""A wait that core refuses when the turn ends must end the turn, not halt the run.

An agent's wait is checked against committed state when the agent asks and committed when
its turn ends. If the commit refuses it (a peer's commit, a closed scope, an unowned job),
the turn must end plainly: the run goes on, and the strategy sees an ordinary result. (The
agent's own reply still says "waiting" and no suspension follows; a strategy that keys on
the reply would wait forever, REVIEW-P10 P2-2.) Here a stand-in for the bridge's accepted wait yields a wait on a
job the run does not own, which core refuses when it commits the suspension.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from tests.support.agent_tool_world import Program, ScriptedAgent, scenario
from tests.support.concurrent_turns_strategy import (
    FIRST,
    ConcurrentTurnsState,
    ConcurrentTurnsStrategy,
)
from tests.support.fake_run_clock import FakeRunClock
from tests.support.skeleton_world import LEASE, drive

from vs_core.api import (
    Continuation,
    ContinuationId,
    ContinuationPhase,
    InvocationId,
    InvocationRef,
    Limits,
    ResourceId,
    RunStatus,
)
from vs_runtime.api.core import ObservationRejectedError

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api import DispatchTurn
    from vs_runtime.api.core import AgentEvaluationBridge, TurnYields


@dataclass
class RefusedWaits:
    """Stands in for an accepted wait that core will refuse: the turn yields a wait on an unowned job."""

    bridge: AgentEvaluationBridge

    def yielded(self, request: DispatchTurn) -> Continuation | None:
        """A wait the commit cannot honor for the first turn; the bridge's answer otherwise."""
        turn = request.turn
        if turn.invocation_id.root != FIRST:
            return self.bridge.yielded(request)
        name = turn.invocation_id.root
        session, generation = turn.session.session_id, request.scope.generation
        return Continuation(
            continuation_id=ContinuationId(root=f"{name}/evaluation"),
            invocation=InvocationRef(
                session_id=session, invocation_id=turn.invocation_id, generation=generation
            ),
            next_invocation=InvocationRef(
                session_id=session,
                invocation_id=InvocationId(root=f"{name}/resume"),
                generation=generation,
            ),
            jobs=(ResourceId(root="job:not-owned"),),
            deadline_at=500.0,
            phase=ContinuationPhase.WAITING,
        )


async def play(tmp_path: Path) -> ConcurrentTurnsState:
    """Run a one-turn attempt whose wait is refused at commit; it must reach a terminal state."""
    programs: tuple[Program, ...] = ((("submit",),),)
    limits = Limits(max_turns=20, max_measurement_submissions=64)

    def agent(root: Path) -> ScriptedAgent:
        return ScriptedAgent(root, programs=programs)

    def refused(bridge: AgentEvaluationBridge) -> TurnYields:
        return RefusedWaits(bridge)

    async with scenario(
        tmp_path, ConcurrentTurnsStrategy(wave=()), limits, agent, yields_over=refused
    ) as played:
        process = played.world.runtime()
        clock = FakeRunClock(0.0)
        process.shell.start("skeleton", now_at=0.0, lease_duration=LEASE)
        played.bridge.attach(process.shell, clock)
        await played.bridge.serve()
        try:
            assert await drive(process, start=0.0, clock=clock) is None
        finally:
            await played.bridge.close()
        assert process.shell.record.envelope.core.run.status == RunStatus.TERMINAL
        seen = process.shell.record.envelope.strategy
        assert isinstance(seen, ConcurrentTurnsState)
        return seen


@pytest.mark.xfail(
    strict=True,
    raises=ObservationRejectedError,
    reason=(
        "CONCURRENCY-4 finding: core refuses a suspension when the checkpoint-retention "
        "observation (retain-write) commits it, not when TurnObserved does. That is an "
        "executor observation, so the shell halts (ObservationRejectedError) and "
        "_end_turn_without_wait, which only handles a rejected TurnObserved, never runs."
    ),
)
def test_a_wait_core_refuses_at_commit_ends_the_turn_and_the_strategy_is_told(
    tmp_path: Path,
) -> None:
    """The run ends terminally; the turn's result is ordinary and marked as a refused wait."""
    root = tmp_path / uuid.uuid4().hex[:6]
    root.mkdir()
    seen = asyncio.run(play(root))
    assert seen.results == (FIRST,)
    assert seen.failed == ()
    assert seen.suspended == ()
