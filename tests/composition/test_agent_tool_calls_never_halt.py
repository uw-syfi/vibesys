"""An agent's evaluation tool calls can never halt the run.

Tool calls are untrusted input. A call core cannot accept must come back to the agent as a
tool error and the run must go on. A scripted agent (the Fake driver) calls the real tool
handlers over the bridge's socket, from its turns, with generated programs: submit, wait on
own, repeated or stale handles, across fresh turns and resumed turns. The strategy
dispatches a generated number of fresh implementer turns and resumes every suspension.

Properties: the run reaches a terminal state without a refusal or an unhandled error; every
tool call either succeeds or fails with a typed tool error; every suspension core accepted
is resumed exactly once.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.agent_tool_world import Program, ScriptedAgent, scenario
from tests.support.host_clock import clock_from
from tests.support.skeleton_world import LEASE, Process, drive
from tests.support.waiting_loop_strategy import LoopState, LoopStrategy

from vs_core.api import Limits, RunStatus
from vs_sim.api.testing import VirtualClock, run_virtual

if TYPE_CHECKING:
    from pathlib import Path


async def play(
    tmp_path: Path, programs: tuple[Program, ...], total: int
) -> tuple[Process, ScriptedAgent]:
    """Drive the run to its end; it must not halt, refuse or raise."""
    limits = Limits(max_turns=4 * total + 4, max_measurement_submissions=64)

    def agent(root: Path) -> ScriptedAgent:
        return ScriptedAgent(root, programs=programs)

    async with scenario(tmp_path, LoopStrategy(total=total), limits, agent) as played:
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
    strategy = process.shell.record.envelope.strategy
    assert isinstance(strategy, LoopState)
    assert state.run.status == RunStatus.TERMINAL
    assert strategy.suspensions == strategy.resumes
    assert strategy.suspensions <= agent.waits_accepted


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


_STEPS = st.one_of(
    st.just(("submit",)),
    st.tuples(st.just("wait"), st.lists(st.integers(0, 4), min_size=1, max_size=3).map(tuple)),
)
_PROGRAMS = st.lists(st.lists(_STEPS, max_size=4).map(tuple), min_size=1, max_size=4).map(tuple)


@settings(
    max_examples=2,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(programs=_PROGRAMS, total=st.integers(1, 3))
def test_no_tool_call_sequence_halts_the_run(
    tmp_path: Path, programs: tuple[Program, ...], total: int
) -> None:
    agent = run_play(tmp_path, programs, total)
    assert all(isinstance(text, str) and text for text in agent.errors)


def test_a_wait_on_a_handle_from_an_earlier_turn_does_not_halt(tmp_path: Path) -> None:
    """Turn 0 submits and ends; turn 1 waits on that handle; turn 2 does nothing."""
    run_play(tmp_path, ((("submit",),), (("wait", (0,)),), ()), total=3)
