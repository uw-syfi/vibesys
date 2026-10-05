"""Peer turns in one scope that both wait: the later commit is rejected and its turn just ends.

An agent's wait is checked against committed state when the agent asks, and committed when
its turn ends. With turns running at once, a peer's commit can land in between: core then
refuses the second suspension (the scope already has an open continuation). That refusal
must end the turn plainly, visible to the strategy as an ordinary result, and keep the
peer's suspension and result. It must never halt the run: an agent tool call never does.

The first turn runs alone and submits a measurement through the real bridge. A wave of
peer turns then runs together, each in its own session. A stand-in for the bridge's
accepted wait (``AcceptedWaits``) makes the chosen peers yield a wait on that measurement.
Each peer turn is held until the test releases it, in a chosen order, and the next peer is
released only after the previous peer's observation was committed, so every completion
order is exercised on purpose: no sleeps, no wall clock.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.agent_tool_world import Program, ScriptedAgent, scenario
from tests.support.concurrent_turns_strategy import (
    FIRST,
    ConcurrentTurnsState,
    ConcurrentTurnsStrategy,
)
from tests.support.fake_run_clock import FakeRunClock
from tests.support.skeleton_world import LEASE, Process, drive

from vs_core.api import (
    Continuation,
    ContinuationId,
    ContinuationPhase,
    InvocationId,
    InvocationRef,
    Limits,
    RunStatus,
    TurnObserved,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_agent.api import AgentTurnRequest
    from vs_core.api import DispatchTurn
    from vs_runtime.api.core import AgentEvaluationBridge, CoreRuntime, RuntimeRecord, TurnYields

NAMES = ("peer-a", "peer-b", "peer-c")


@dataclass
class Release:
    """Holds each peer turn until its turn in ``order`` comes.

    A peer reaches the gate after its work and blocks. When all have arrived, the first in
    ``order`` is released; each next one is released when the previous one's turn
    observation has been consumed by a commit.
    """

    order: tuple[str, ...]
    _arrived: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _open: dict[str, threading.Event] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._open = {name: threading.Event() for name in self.order}

    def reach(self, name: str) -> None:
        """Called on the turn's thread: wait here until this turn is released."""
        with self._lock:
            self._arrived.add(name)
            if len(self._arrived) == len(self.order):
                self._open[self.order[0]].set()
        self._open[name].wait()

    def committed(
        self, previous: RuntimeRecord[object] | None, current: RuntimeRecord[object]
    ) -> None:
        """Called on the loop's thread after each commit: release the peer after a finished one."""
        if previous is None:
            return
        gone = _observed(previous) - _observed(current)
        for name in gone & set(self.order):
            at = self.order.index(name)
            if at + 1 < len(self.order):
                self._open[self.order[at + 1]].set()


def _observed(record: RuntimeRecord[object]) -> set[str]:
    """The invocations whose turn observation is committed but not yet applied."""
    return {
        event.invocation.invocation_id.root
        for event in record.pending_inputs
        if isinstance(event, TurnObserved)
    }


@dataclass
class PeerAgent(ScriptedAgent):
    """The first turn submits a measurement; each peer turn works, then waits to be released."""

    release: Release | None = None

    def __call__(self, request: AgentTurnRequest) -> None:
        """Run the first turn's program, hold a peer at the gate, let a resume pass."""
        assert self.release is not None
        name = request.invocation_id
        if name == FIRST:
            super().__call__(request)
        elif name in self.release.order:
            self.release.reach(name)


@dataclass
class AcceptedWaits:
    """Stands in for an agent's accepted wait: the chosen peers yield a wait on the measurement.

    The bridge's gate would pass each of these waits, because it reads committed state at
    the time the agent asks. Core's commit-time rule is what the test needs to exercise.
    """

    bridge: AgentEvaluationBridge
    waiters: frozenset[str]
    shell: Callable[[], CoreRuntime[ConcurrentTurnsState]]

    def yielded(self, request: DispatchTurn) -> Continuation | None:
        """A wait on the first turn's measurement for a chosen peer, else the bridge's answer."""
        turn = request.turn
        if turn.invocation_id.root not in self.waiters:
            return self.bridge.yielded(request)
        (job,) = self.shell().record.envelope.core.evaluation.jobs
        session, generation = turn.session.session_id, request.scope.generation
        name = turn.invocation_id.root
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
            jobs=(job.resource_id,),
            deadline_at=job.plan.deadline_at,
            phase=ContinuationPhase.WAITING,
        )


async def play(
    tmp_path: Path, order: tuple[str, ...], waiters: frozenset[str]
) -> tuple[Process, ConcurrentTurnsState]:
    """Run the wave to its end with the peers finishing in ``order``; it must not halt."""
    release = Release(order)
    programs: tuple[Program, ...] = ((("submit",),),)
    strategy = ConcurrentTurnsStrategy(wave=tuple(sorted(order)))
    limits = Limits(max_parallel=len(order), max_turns=40, max_measurement_submissions=64)
    holder: list[Process] = []

    def agent(root: Path) -> PeerAgent:
        return PeerAgent(root, programs=programs, release=release)

    def waits(bridge: AgentEvaluationBridge) -> TurnYields:
        return AcceptedWaits(bridge, waiters, lambda: holder[0].shell)

    async with scenario(tmp_path, strategy, limits, agent, yields_over=waits) as played:
        played.world.commits = release  # type: ignore[assignment]
        process = played.world.runtime()
        holder.append(process)
        clock = FakeRunClock(0.0)
        process.shell.start("skeleton", now_at=0.0, lease_duration=LEASE)
        played.bridge.attach(process.shell, clock)
        await played.bridge.serve()
        try:
            refusal = await drive(process, start=0.0, clock=clock)
        finally:
            await played.bridge.close()
        assert refusal is None
        strategy_state = process.shell.record.envelope.strategy
        assert isinstance(strategy_state, ConcurrentTurnsState)
        return process, strategy_state


def check(
    process: Process, seen: ConcurrentTurnsState, order: tuple[str, ...], waiters: frozenset[str]
) -> None:
    """The run ended terminally; the first waiter in finishing order suspended, the rest ended."""
    core = process.shell.record.envelope.core
    assert core.run.status == RunStatus.TERMINAL
    waiting = [name for name in order if name in waiters]
    kept = tuple(waiting[:1])
    assert seen.suspended == kept
    # Every turn finished and reported an ordinary result, the refused waits included.
    assert seen.failed == ()
    assert [name for name in seen.results if name in order] == list(order)
    assert sorted(seen.results) == sorted([FIRST, *order, *(f"{name}/resume" for name in kept)])
    # The kept wait was resumed and closed: nothing is left suspended.
    assert len(core.evaluation.continuations) == len(kept)
    assert len(seen.resumable) == len(kept)


def run_play(tmp_path: Path, order: tuple[str, ...], waiters: frozenset[str]) -> None:
    root = tmp_path / uuid.uuid4().hex[:6]
    root.mkdir()

    async def go() -> None:
        process, seen = await play(root, order, waiters)
        check(process, seen, order, waiters)

    asyncio.run(go())


@pytest.mark.parametrize("order", list(itertools.permutations(NAMES[:2])), ids="-".join)
def test_a_second_wait_in_one_scope_ends_its_turn_and_keeps_the_first(
    tmp_path: Path, order: tuple[str, ...]
) -> None:
    """Both peers wait; whichever commits second ends plainly and the first one's wait stands."""
    run_play(tmp_path, order, frozenset(order))


_WAVES = st.integers(2, 3).flatmap(
    lambda size: st.tuples(
        st.permutations(NAMES[:size]).map(tuple),
        st.sets(st.sampled_from(NAMES[:size])).map(frozenset),
    )
)


@settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(wave=_WAVES)
def test_no_completion_order_of_waiting_peers_halts_the_run(
    tmp_path: Path, wave: tuple[tuple[str, ...], frozenset[str]]
) -> None:
    """Any finishing order and any set of waiting peers: the run ends, one wait is kept."""
    order, waiters = wave
    run_play(tmp_path, order, waiters)
