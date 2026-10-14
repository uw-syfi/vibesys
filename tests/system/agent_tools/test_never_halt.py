"""An agent's evaluation tool calls can never halt the run: the end-to-end examples.

A scripted agent (the Fake driver) calls the real tool handlers over the bridge's socket, from
its turns. The generated property over any call sequence lives in `test_core_property.py`
(`test_no_tool_call_sequence_halts_the_run`, on the cheap layer); the two examples here keep the
real socket in the loop: a fresh turn after a resumed one may submit and wait again, and a wait
on a handle from an earlier turn does not halt.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from tests.support.agent_tool_world import Program, ScriptedAgent, resumes, scenario, suspensions
from tests.support.skeleton_strategy import SkeletonConfig, SkeletonStrategy
from tests.support.skeleton_world import LEASE, Process, drive

from vs_core.api import RunStatus
from vs_core.api.testing import sample_limits
from vs_sim.api.testing import VirtualClock, clock_from, run_virtual

if TYPE_CHECKING:
    from pathlib import Path


async def play(
    tmp_path: Path, programs: tuple[Program, ...], total: int
) -> tuple[Process, ScriptedAgent]:
    """Drive the run to its end; it must not halt, refuse or raise."""
    limits = sample_limits(max_turns=4 * total + 4)

    def agent(root: Path) -> ScriptedAgent:
        return ScriptedAgent(root, programs=programs)

    async with scenario(
        tmp_path, SkeletonStrategy(config=SkeletonConfig(turns=total)), limits, agent
    ) as played:
        process = played.world.runtime()
        clock = clock_from(0.0)
        process.shell.start("skeleton", now_at=0.0, lease_duration=LEASE)
        played.bridge.attach(process.shell, clock)
        await played.bridge.serve()
        try:
            refusal = await drive(process, start=0.0, clock=clock)
        finally:
            await played.bridge.close()
        assert refusal is None
        return process, played.agent


def check(process: Process, agent: ScriptedAgent) -> None:
    """The run ended terminally, and every accepted suspension was resumed once."""
    state = process.shell.record.envelope.core
    assert state.run.status == RunStatus.TERMINAL
    assert suspensions(state) == resumes(state)
    assert suspensions(state) <= agent.waits_accepted


def run_play(tmp_path: Path, programs: tuple[Program, ...], total: int) -> ScriptedAgent:
    root = tmp_path / uuid.uuid4().hex[:6]
    root.mkdir()

    async def go() -> ScriptedAgent:
        process, agent = await play(root, programs, total)
        check(process, agent)
        return agent

    return run_virtual(VirtualClock(), go())


SUBMIT_AND_WAIT: Program = (("submit",), ("wait", (0,)))


def test_a_fresh_turn_after_a_resumed_turn_may_submit_and_wait_again(tmp_path: Path) -> None:
    """The live-1 crash: turn, resume that ends plainly, then a fresh turn waits again."""
    agent = run_play(tmp_path, (SUBMIT_AND_WAIT, (), (("submit",), ("wait", (1,))), ()), total=2)
    assert agent.errors == []
    assert agent.waits_accepted == 2


def test_a_wait_on_a_handle_from_an_earlier_turn_does_not_halt(tmp_path: Path) -> None:
    """Turn 0 submits and ends; turn 1 waits on that handle; turn 2 does nothing."""
    run_play(tmp_path, ((("submit",),), (("wait", (0,)),), ()), total=3)
