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

import pytest
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
    ClockAdvanced,
    DispatchTurn,
    IntentPhase,
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
    HEARTBEAT_TASK,
    WAIT_TASK,
    CoreRunHost,
    CoreRuntime,
    CoreRuntimeBindings,
    Publication,
    PublicationAcknowledgement,
    PublicationContext,
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
        task = asyncio.current_task()
        if task is not None and task.get_name() == HEARTBEAT_TASK:
            # Logical time is moved by the turns and the loop, never by the heartbeat.
            await asyncio.sleep(0)
            return
        if task is not None and task.get_name() == WAIT_TASK:
            # The loop waiting beside running turns: their delays are loop yields, so no
            # time passes until a turn finishes (the loop cancels this sleep then).
            await asyncio.get_running_loop().create_future()
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
    failing: frozenset[int] = frozenset()
    """Ordinals (start order) of the implementer turns that raise after their delay."""
    started: int = 0
    returned: list[str] = field(default_factory=list)
    """Implementer turns that finished and handed their observation back."""


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
        ordinal = ledger.started
        ledger.started += 1
        ledger.turns_in_flight += 1
        ledger.max_turns_in_flight = max(ledger.max_turns_in_flight, ledger.turns_in_flight)
        try:
            for _ in range(ledger.delays.popleft() if ledger.delays else 0):
                await asyncio.sleep(0)
            if ordinal in ledger.failing:
                message = f"implementer turn {ordinal} failed"
                raise _TurnFailedError(message)
            result = await super().execute(request, context)
            ledger.returned.append(request.request_id.root)
            return result
        finally:
            ledger.turns_in_flight -= 1


class _TurnFailedError(RuntimeError):
    """An executor error: the turn's process died."""


def _is_implementer_turn(request: Request) -> bool:
    return isinstance(request, DispatchTurn) and request.turn.session.role_id.root.endswith(
        "implementer"
    )


@dataclass(frozen=True)
class _Finished:
    core: CoreState
    ledger: _Ledger
    start: CoreState


@dataclass(frozen=True)
class _Built:
    shell: CoreRuntime[DynamicStrategyState]
    host: CoreRunHost
    config: RunLoopConfig
    ledger: _Ledger
    start: CoreState


def _build(
    cap: int | None,
    delays: list[int],
    *,
    failing: frozenset[int] = frozenset(),
    delivery: Callable[[FakeStateStore], FakePublicationDelivery] = FakePublicationDelivery,
) -> _Built:
    ledger = _Ledger(delays=deque(delays), failing=failing)
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
    host = CoreRunHost(shell, delivery(store), _YieldingClock(1.0))
    loop_config = RunLoopConfig(
        host_id="concurrent", lease_duration=LEASE, max_dispatches=400, max_concurrent=cap
    )
    start_core(host, loop_config)
    return _Built(shell, host, loop_config, ledger, shell.record.envelope.core)


def _run(cap: int | None, delays: list[int]) -> _Finished:
    built = _build(cap, delays)
    outcome = asyncio.run(drive_core(built.host, built.config))
    assert outcome.status == RunStatus.TERMINAL
    return _Finished(core=built.shell.record.envelope.core, ledger=built.ledger, start=built.start)


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


@settings(max_examples=12, deadline=None)
@given(
    delays=st.lists(
        st.integers(min_value=0, max_value=3), min_size=IMPLEMENTERS, max_size=IMPLEMENTERS
    ),
    failing=st.integers(min_value=0, max_value=IMPLEMENTERS - 1),
)
def test_a_failing_turn_keeps_what_the_turns_that_finished_with_it_returned(
    delays: list[int], failing: int
) -> None:
    """A failure halts the run, but an observation another flight already returned is
    committed first: that work ran once and is not redone.
    """
    built = _build(2, delays, failing=frozenset({failing}))
    with pytest.raises(_TurnFailedError):
        asyncio.run(drive_core(built.host, built.config))
    intents = {i.request_id.root: i for i in built.shell.record.envelope.core.intents.intents}
    for request_id in built.ledger.returned:
        assert intents[request_id].phase == IntentPhase.COMPLETED, request_id


class _AdmittingDelivery(FakePublicationDelivery):
    """Publishing awaits, and an agent tool call arrives meanwhile; the delivery may fail."""

    def __init__(self, store: FakeStateStore, *, fails: bool) -> None:
        super().__init__(store)
        self.shell: CoreRuntime[DynamicStrategyState] | None = None
        self.admitted: list[CoreEvent] = []
        self._fails = fails

    async def publish(
        self, publication: Publication, context: PublicationContext
    ) -> PublicationAcknowledgement:
        assert self.shell is not None
        if not self.admitted:
            now_at = context.now_at
            for _ in range(2):
                event = ClockAdvanced(now_at=now_at)
                self.shell.admit(event, now_at=now_at)
                self.admitted.append(event)
            if self._fails:
                message = "delivery is down"
                raise OSError(message)
        return await super().publish(publication, context)


@pytest.mark.parametrize("fails", [True, False])
def test_a_tool_call_admitted_during_a_publish_commits_and_never_halts(*, fails: bool) -> None:
    deliveries: list[_AdmittingDelivery] = []

    def make(store: FakeStateStore) -> FakePublicationDelivery:
        deliveries.append(_AdmittingDelivery(store, fails=fails))
        return deliveries[0]

    built = _build(2, [1, 1], delivery=make)
    deliveries[0].shell = built.shell
    try:
        outcome = asyncio.run(drive_core(built.host, built.config))
    except OSError:
        assert fails, "only a failing delivery may end the run"
    else:
        assert not fails
        assert outcome.status == RunStatus.TERMINAL
    (delivery,) = deliveries
    assert len(delivery.admitted) == 2
    for event in delivery.admitted:
        # Once to answer the tool call, once in the commit that followed it.
        assert sum(1 for _, stepped, _ in built.ledger.stepped if stepped is event) == 2
