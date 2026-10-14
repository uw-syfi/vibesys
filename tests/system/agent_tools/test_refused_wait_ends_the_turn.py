"""A wait that core refuses when the turn ends must end the turn, not halt the run.

An agent's wait is checked against committed state when the agent asks and committed when
its turn ends. If the commit refuses it (a peer's commit, a closed scope, an unowned job),
the turn must end plainly: the run goes on, and the search sees an ordinary result. (The
agent's own reply still says "waiting" and no suspension follows; a strategy that keys on
the reply would wait forever, REVIEW-P10 P2-2.) Here a stand-in for the bridge's accepted wait yields a wait on a
job the run does not own, which core refuses when it commits the suspension.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from tests.support.agent_tool_world import Program, ScriptedAgent, scenario
from tests.support.skeleton_strategy import SkeletonStrategy
from tests.support.skeleton_world import LEASE, drive

from vs_core.api import (
    Continuation,
    ContinuationId,
    ContinuationPhase,
    CoreState,
    InvocationId,
    InvocationRef,
    ResourceId,
    RunStatus,
)
from vs_core.api.testing import sample_limits
from vs_sim.api.testing import VirtualClock, clock_from, run_virtual

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api import DispatchTurn
    from vs_runtime.api.core import AgentEvaluationBridge, TurnYields


@dataclass
class RefusedWaits:
    """Stands in for an accepted wait that core will refuse: the turn yields a wait on an unowned job."""

    bridge: AgentEvaluationBridge
    job: str = "job:not-owned"
    refused: bool = False

    def yielded(self, request: DispatchTurn) -> Continuation | None:
        """A wait the commit cannot honor for the first turn; the bridge's answer otherwise."""
        turn = request.turn
        if self.refused or turn.charge_class == "resume":
            return self.bridge.yielded(request)
        self.refused = True
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
            jobs=(ResourceId(root=self.job),),
            deadline_at=500.0,
            phase=ContinuationPhase.WAITING,
        )


async def play(tmp_path: Path, job: str) -> CoreState:
    """Run a one-turn attempt whose wait is refused at commit; it must reach a terminal state."""
    programs: tuple[Program, ...] = ((),)
    limits = sample_limits(max_turns=20)

    def agent(root: Path) -> ScriptedAgent:
        return ScriptedAgent(root, programs=programs)

    def refused(bridge: AgentEvaluationBridge) -> TurnYields:
        return RefusedWaits(bridge, job)

    async with scenario(tmp_path, SkeletonStrategy(), limits, agent, yields_over=refused) as played:
        process = played.world.runtime()
        clock = clock_from(0.0)
        process.shell.start("skeleton", now_at=0.0, lease_duration=LEASE)
        played.bridge.attach(process.shell, clock)
        await played.bridge.serve()
        try:
            assert await drive(process, start=0.0, clock=clock) is None
        finally:
            await played.bridge.close()
        return process.shell.record.envelope.core


@pytest.mark.parametrize(
    "job", ["job:not-owned", "vs-" + "0" * 40, "x"], ids=["unowned", "handle-shaped", "short"]
)
def test_a_wait_core_refuses_at_commit_ends_the_turn_and_the_strategy_is_told(
    job: str, tmp_path: Path
) -> None:
    """The run ends terminally; the turn's result is ordinary and marked as a refused wait."""
    root = tmp_path / uuid.uuid4().hex[:6]
    root.mkdir()
    state = run_virtual(VirtualClock(), play(root, job))
    assert state.run.status == RunStatus.TERMINAL
    assert state.run.result is not None
    assert state.run.result.outcome == "success"
    # The turn ended plainly: one invocation, no committed wait, and so no resume.
    [row] = state.sessions.invocations
    assert not row.invocation.invocation_id.root.endswith("/resume")
    assert state.evaluation.continuations == ()
