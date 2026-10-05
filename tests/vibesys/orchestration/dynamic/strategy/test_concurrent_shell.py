"""The production shell runs several agent turns at once and commits one input at a time.

Two implementer turns are gated by a count of event-loop yields (no sleeps, no wall
clock), so a drawn delay decides the order they finish in, and equal delays are the
same-tick tie. Whatever the order or the cap, the run must end in the same place, every
commit must be one pure core step, and concurrency must stay within the cap.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.fake_run_clock import FakeRunClock
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import FACTS, LIMITS, config
from tests.vibesys.orchestration.dynamic.strategy._shell import (
    LEASE,
    ScriptedExecutors,
    _executors,
)

from vibesys.orchestration.dynamic.core_policy.api import reply_schemas, requirements_for
from vibesys.orchestration.dynamic.strategy.api import (
    DynamicStrategy,
    DynamicStrategyState,
    dynamic_operation_registry,
)
from vs_core.api import (
    DispatchTurn,
    RequestObserved,
    ResourceId,
    RunEnvelope,
    RunStatus,
    SubmitMeasurement,
    step,
)
from vs_core.testing.drive import Harness, Running, new_run
from vs_project.api import FakeStateStore
from vs_runtime.api.core import (
    CoreRunHost,
    CoreRuntime,
    CoreRuntimeBindings,
    RunLoopConfig,
    drive_core,
    start_core,
)
from vs_runtime.api.testing import FakePublicationDelivery

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel
    from tests.vibesys.orchestration.dynamic.strategy._shell import Script

    from vs_core.api import CoreEvent, CoreState, OperationRegistry, Request, SchemaRef, Transition
    from vs_runtime.api.core import ExecutionContext, ExecutionResult

CAPS = (1, 2, 3)
IMPLEMENTERS = 2


class _YieldingClock(FakeRunClock):
    """Logical time that passes without waiting for other tasks, so turns can overlap."""

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)
        self.sleeps.append(seconds)
        self.at += seconds


@dataclass
class _Ledger:
    """What the executors and the pure core saw, in the order it happened."""

    turns_in_flight: int = 0
    max_turns_in_flight: int = 0
    executed: list[str] = field(default_factory=list)
    stepped: list[tuple[CoreState, CoreEvent, CoreState]] = field(default_factory=list)
    delays: deque[int] = field(default_factory=deque)


class _RecordingTransitions:
    """The production pure step, with every call recorded (no behavior change)."""

    def __init__(self, ledger: _Ledger) -> None:
        self._ledger = ledger

    def step(self, state: CoreState, event: CoreEvent) -> Transition:
        transition = step(state, event)
        self._ledger.stepped.append((state, event, transition.state))
        return transition


class _DelayedExecutors(ScriptedExecutors):
    """Implementer turns finish after a drawn number of event-loop yields."""

    def __init__(
        self,
        script: Script,
        core: Callable[[], CoreState],
        registry: OperationRegistry,
        schemas: Mapping[SchemaRef, type[BaseModel]],
        ledger: _Ledger,
    ) -> None:
        super().__init__(script, core, registry, schemas)
        self._ledger = ledger

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        assert request.request_id is not None
        self._ledger.executed.append(request.request_id.root)
        if not _is_implementer_turn(request):
            return await super().execute(request, context)
        ledger = self._ledger
        ledger.turns_in_flight += 1
        ledger.max_turns_in_flight = max(ledger.max_turns_in_flight, ledger.turns_in_flight)
        try:
            for _ in range(ledger.delays.popleft() if ledger.delays else 0):
                await asyncio.sleep(0)
            return await super().execute(request, context)
        finally:
            ledger.turns_in_flight -= 1


def _is_implementer_turn(request: Request) -> bool:
    return isinstance(request, DispatchTurn) and request.turn.session.role_id.root.endswith(
        "implementer"
    )


@dataclass(frozen=True)
class _Finished:
    core: CoreState
    ledger: _Ledger
    start: CoreState


def _run(cap: int | None, delays: list[int]) -> _Finished:
    ledger = _Ledger(delays=deque(delays))
    executors = Executors(
        planner=deque([plan_reply(*(implement(f"h{n}") for n in range(IMPLEMENTERS)))]),
        implementer=deque(implemented() for _ in range(IMPLEMENTERS)),
        judge=deque(reviewed() for _ in range(IMPLEMENTERS)),
        # A job is named by its request, not by the order submissions happen to arrive in.
        submit=lambda request: Running(
            resource_id=ResourceId(root=f"job:{_request_root(request)}")
        ),
    )
    selected = config(max_in_flight=IMPLEMENTERS)
    harness = Harness(
        registry=dynamic_operation_registry(),
        facts=FACTS,
        limits=LIMITS,
        envelope_type=RunEnvelope[DynamicStrategyState],
        requirements=requirements_for(selected),
    )
    strategy = DynamicStrategy(config=selected)
    store = FakeStateStore()
    shell: CoreRuntime[DynamicStrategyState] = CoreRuntime(
        store,
        strategy,
        new_run(strategy, harness),
        bindings=CoreRuntimeBindings(
            registry=harness.registry,
            transitions=_RecordingTransitions(ledger),
            executors=_executors(
                _DelayedExecutors(
                    executors,
                    lambda: shell.record.envelope.core,
                    harness.registry,
                    reply_schemas(selected),
                    ledger,
                )
            ),
        ),
    )
    host = CoreRunHost(shell, FakePublicationDelivery(store), _YieldingClock(1.0))
    loop_config = RunLoopConfig(
        host_id="concurrent", lease_duration=LEASE, max_dispatches=400, max_concurrent=cap
    )
    start_core(host, loop_config)
    start = shell.record.envelope.core
    outcome = asyncio.run(drive_core(host, loop_config))
    assert outcome.status == RunStatus.TERMINAL
    return _Finished(core=shell.record.envelope.core, ledger=ledger, start=start)


def _request_root(request: SubmitMeasurement) -> str:
    assert request.request_id is not None
    return request.request_id.root


def _committed_chain(finished: _Finished) -> list[tuple[CoreState, CoreEvent, CoreState]]:
    """The pure steps the shell committed, from the state after start to the final state."""
    by_before = {id(entry[0]): entry for entry in finished.ledger.stepped}
    chain = []
    current = finished.start
    while id(current) in by_before:
        entry = by_before[id(current)]
        chain.append(entry)
        current = entry[2]
    assert current is finished.core, "every commit must be one pure step of one input"
    return chain


def _observations(finished: _Finished) -> list[tuple[object, ...]]:
    return sorted(
        (
            # Request ids embed the core revision they were issued at, which timing changes.
            str(event.observation.request_id.root).split(":")[0],
            event.observation.status.value,
            event.observation.terminal,
        )
        for _, event, _ in _committed_chain(finished)
        if isinstance(event, RequestObserved)
    )


def _winner(finished: _Finished) -> tuple[object, ...]:
    result = finished.core.run.result
    assert result is not None
    return (result.model_dump_json(),)


_BASELINE = {}


def _baseline() -> _Finished:
    if not _BASELINE:
        _BASELINE["run"] = _run(1, [])
    return _BASELINE["run"]


@settings(max_examples=25, deadline=None)
@given(
    cap=st.sampled_from(CAPS),
    delays=st.lists(st.integers(min_value=0, max_value=6), min_size=IMPLEMENTERS, max_size=4),
)
def test_any_completion_order_commits_the_same_run_within_the_cap(
    cap: int, delays: list[int]
) -> None:
    finished = _run(cap, delays)
    reference = _baseline()
    ledger = finished.ledger
    assert ledger.max_turns_in_flight <= cap
    assert len(ledger.executed) == len(set(ledger.executed)), "a request ran twice"
    assert _winner(finished) == _winner(reference)
    assert _observations(finished) == _observations(reference)
    for before, event, after in _committed_chain(finished):
        assert step(before, event).state == after


def test_turns_overlap_up_to_the_cap() -> None:
    long_turns = [40] * IMPLEMENTERS
    assert _run(1, long_turns).ledger.max_turns_in_flight == 1
    assert _run(2, long_turns).ledger.max_turns_in_flight == 2
    assert _run(3, long_turns).ledger.max_turns_in_flight == 2


def test_without_an_explicit_cap_the_run_limit_sets_it() -> None:
    """One source of truth: the run's max_parallel (LIMITS has 2) is the loop's cap."""
    assert _run(None, [40] * IMPLEMENTERS).ledger.max_turns_in_flight == LIMITS.max_parallel
