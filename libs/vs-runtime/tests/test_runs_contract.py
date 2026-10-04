"""One launch-lifetime contract for production and Fake implementations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from pydantic import BaseModel, ConfigDict

from vs_runtime.api.testing import FakeRuns
from vs_runtime.api.wiring import InProcessRuns, TaskRunHandle

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_runtime.api import RunExecution

type Launch = tuple[InProcessRuns[Request, int, int, RunExecution[int]], FakeExecution]


class Request(BaseModel):
    """Validated launch input for the generic contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    resume: bool = False


@dataclass
class FakeExecution:
    """In-memory execution with explicit barriers and faithful cleanup."""

    sink: Callable[[int], None]
    entered: asyncio.Event
    release: asyncio.Event
    failure: BaseException | None = None
    close_failure: BaseException | None = None
    start_failure: BaseException | None = None
    starts: int = 0
    stops: int = 0
    closes: int = 0

    def start(self) -> None:
        self.starts += 1
        if self.start_failure is not None:
            raise self.start_failure

    async def await_result(self) -> int:
        self.sink(1)
        self.entered.set()
        await self.release.wait()
        if self.failure is not None:
            raise self.failure
        self.sink(2)
        return 42

    def stop(self) -> None:
        self.stops += 1
        self.release.set()

    def close(self) -> None:
        self.closes += 1
        if self.close_failure is not None:
            raise self.close_failure


@dataclass
class CleanupExecution(FakeExecution):
    """Hold asynchronous resource release at an explicit cancellation barrier."""

    cleanup_entered: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_release: asyncio.Event = field(default_factory=asyncio.Event)
    cleanups: int = 0
    completed_cleanups: int = 0

    async def await_result(self) -> int:
        try:
            return await super().await_result()
        finally:
            self.cleanups += 1
            self.cleanup_entered.set()
            await self.cleanup_release.wait()
            self.completed_cleanups += 1


type CleanupControl = Literal["cancel", "stop", "other-task-cancel"]


async def assert_cancellation_finishes_cleanup(
    implementation: type[InProcessRuns[Request, int, int, RunExecution[int]]],
    controls: list[CleanupControl],
    *,
    stop_first: bool,
) -> None:
    execution = CleanupExecution(lambda _event: None, asyncio.Event(), asyncio.Event())
    runs = implementation(
        lambda _request, _sink: execution,
        identity=lambda request: request.run_id,
        is_resume=lambda request: request.resume,
    )
    handle = runs.start(Request(run_id="cleanup"))
    await execution.entered.wait()
    if stop_first:
        handle.stop()
    handle.cancel()
    await execution.cleanup_entered.wait()

    async def cancel_from_other_task() -> None:
        handle.cancel()

    for control in controls:
        match control:
            case "cancel":
                handle.cancel()
            case "stop":
                handle.stop()
            case "other-task-cancel":
                await asyncio.create_task(cancel_from_other_task())
    execution.cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await handle.result()
    assert execution.cleanups == 1
    assert execution.completed_cleanups == 1
    assert execution.closes == 1
    assert runs.list_active() == ()
    assert execution.stops == int(stop_first)
    handle.cancel()
    handle.stop()
    with pytest.raises(asyncio.CancelledError):
        await handle.result()
    assert execution.completed_cleanups == 1
    assert execution.closes == 1
    assert execution.stops == int(stop_first)


@pytest.mark.asyncio
@pytest.mark.parametrize("implementation", [InProcessRuns, FakeRuns], ids=["in-process", "fake"])
@pytest.mark.parametrize("stop_first", [False, True], ids=["cancel", "stop-escalation"])
async def test_repeated_cancellation_preserves_async_cleanup(
    implementation: type[InProcessRuns[Request, int, int, RunExecution[int]]],
    *,
    stop_first: bool,
) -> None:
    await assert_cancellation_finishes_cleanup(
        implementation, ["cancel", "stop", "other-task-cancel"], stop_first=stop_first
    )


@pytest.mark.parametrize("implementation", [InProcessRuns, FakeRuns], ids=["in-process", "fake"])
@given(
    controls=st.lists(
        st.sampled_from(["cancel", "stop", "other-task-cancel"]), min_size=1, max_size=12
    ),
    stop_first=st.booleans(),
)
@example(controls=["cancel", "stop", "other-task-cancel"], stop_first=True)
def test_cleanup_survives_generated_cancellation_controls(
    implementation: type[InProcessRuns[Request, int, int, RunExecution[int]]],
    controls: list[CleanupControl],
    *,
    stop_first: bool,
) -> None:
    asyncio.run(
        assert_cancellation_finishes_cleanup(implementation, controls, stop_first=stop_first)
    )


@pytest.fixture(params=[InProcessRuns, FakeRuns], ids=["in-process", "fake"])
def launch(request: pytest.FixtureRequest) -> Launch:
    """Use identical inputs and execution capabilities for every implementation."""
    execution = FakeExecution(lambda _event: None, asyncio.Event(), asyncio.Event())

    def factory(_: Request, sink: Callable[[int], None]) -> RunExecution[int]:
        execution.sink = sink
        return execution

    runs = request.param(
        factory,
        identity=lambda value: value.run_id,
        is_resume=lambda value: value.resume,
    )
    return runs, execution


@pytest.mark.asyncio
async def test_start_executes_without_result_or_subscriber(launch: Launch) -> None:
    runs, execution = launch
    handle = runs.start(Request(run_id="one"))
    await execution.entered.wait()
    assert execution.starts == 1
    handle.start()
    assert execution.starts == 1
    assert runs.attach("one") is handle
    assert runs.list_active() == (handle,)
    handle.stop()
    assert await handle.result() == 42
    assert execution.stops == 1
    assert execution.closes == 1
    assert runs.list_active() == ()
    assert runs.attach("one") is handle
    assert [event async for event in handle.events()] == [1, 2]
    assert [event async for event in handle.events()] == [1, 2]


@pytest.mark.asyncio
async def test_result_waiter_cancellation_does_not_stop_execution(launch: Launch) -> None:
    runs, execution = launch
    handle = runs.start(Request(run_id="one"))
    await execution.entered.wait()
    waiter_entered = asyncio.Event()

    async def wait() -> int:
        waiter_entered.set()
        return await handle.result()

    waiter = asyncio.create_task(wait())
    await waiter_entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert runs.list_active() == (handle,)
    handle.stop()
    assert await handle.result() == 42
    assert execution.closes == 1


@pytest.mark.asyncio
async def test_subscriber_cancellation_does_not_stop_execution(launch: Launch) -> None:
    runs, _execution = launch
    handle = runs.start(Request(run_id="one"))
    stream = handle.events()
    assert await anext(stream) == 1
    await stream.aclose()
    assert runs.list_active() == (handle,)
    handle.stop()
    assert await handle.result() == 42
    assert [event async for event in handle.events()] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("entered", [False, True], ids=["before-entry", "running"])
async def test_forced_cancellation_cleans_up_even_before_coroutine_entry(
    launch: Launch, *, entered: bool
) -> None:
    runs, execution = launch
    handle = runs.start(Request(run_id="one"))
    if entered:
        await execution.entered.wait()
    handle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle.result()
    assert execution.closes == 1
    assert runs.list_active() == ()
    assert [event async for event in handle.events()] == ([1] if entered else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", [False, True], ids=["execution", "cleanup"])
async def test_failure_settles_events_and_preserves_error(launch: Launch, *, cleanup: bool) -> None:
    runs, execution = launch
    error = ValueError("owned failure")
    if cleanup:
        execution.close_failure = error
    else:
        execution.failure = error
    handle = runs.start(Request(run_id="one"))
    execution.release.set()
    with pytest.raises(ValueError, match="owned failure"):
        await handle.result()
    assert execution.closes == 1
    assert runs.list_active() == ()
    assert [event async for event in handle.events()] == ([1, 2] if cleanup else [1])


@pytest.mark.asyncio
async def test_registry_validation_and_resume(launch: Launch) -> None:
    runs, execution = launch
    with pytest.raises(KeyError):
        runs.attach("missing")
    with pytest.raises(ValueError, match="fresh request"):
        runs.start(Request(run_id="one", resume=True))
    with pytest.raises(ValueError, match=r"request\.resume"):
        runs.resume(Request(run_id="one"))
    with pytest.raises(ValueError, match="empty"):
        runs.start(Request(run_id=""))
    handle = runs.start(Request(run_id="one"))
    with pytest.raises(ValueError, match="already active"):
        runs.start(Request(run_id="one"))
    with pytest.raises(ValueError, match="already active"):
        runs.resume(Request(run_id="one", resume=True))
    handle.stop()
    await handle.result()
    with pytest.raises(ValueError, match="already exists"):
        runs.start(Request(run_id="one"))
    resumed = runs.resume(Request(run_id="one", resume=True))
    assert resumed is not handle
    assert runs.attach("one") is resumed
    assert await resumed.result() == 42
    assert execution.closes == 2


@pytest.mark.asyncio
async def test_simultaneous_subscribers_each_receive_complete_history(launch: Launch) -> None:
    runs, execution = launch
    handle = runs.start(Request(run_id="one"))
    first = handle.events()
    second = handle.events()
    assert await anext(first) == 1
    assert await anext(second) == 1
    first_pending = asyncio.ensure_future(anext(first))
    second_pending = asyncio.ensure_future(anext(second))
    execution.release.set()
    assert await first_pending == 2
    assert await second_pending == 2
    assert await handle.result() == 42
    with pytest.raises(StopAsyncIteration):
        await anext(first)
    with pytest.raises(StopAsyncIteration):
        await anext(second)


@pytest.mark.asyncio
async def test_cancellation_cleanup_failure_remains_observable(launch: Launch) -> None:
    runs, execution = launch
    execution.close_failure = ValueError("cleanup failure")
    handle = runs.start(Request(run_id="one"))
    handle.cancel()
    with pytest.raises(ValueError, match="cleanup failure"):
        await handle.result()
    assert execution.closes == 1
    assert [event async for event in handle.events()] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("implementation", [InProcessRuns, FakeRuns], ids=["in-process", "fake"])
async def test_prepared_request_is_used_for_identity_and_execution(
    implementation: type[InProcessRuns[Request, int, int, RunExecution[int]]],
) -> None:
    execution = FakeExecution(lambda _event: None, asyncio.Event(), asyncio.Event())
    received: list[Request] = []

    def factory(request: Request, sink: Callable[[int], None]) -> RunExecution[int]:
        received.append(request)
        execution.sink = sink
        return execution

    def prepare(request: Request) -> Request:
        return request.model_copy(update={"run_id": f"canonical-{request.run_id}"})

    runs = implementation(
        factory,
        identity=lambda request: request.run_id,
        is_resume=lambda request: request.resume,
        prepare=prepare,
    )
    handle = runs.start(Request(run_id="display"))
    assert handle.run_id == "canonical-display"
    assert received == [Request(run_id="canonical-display")]
    assert runs.attach("canonical-display") is handle
    handle.stop()
    assert await handle.result() == 42


@pytest.mark.parametrize("implementation", [InProcessRuns, FakeRuns], ids=["in-process", "fake"])
def test_event_loop_shutdown_drains_async_execution_cleanup(
    implementation: type[InProcessRuns[Request, int, int, RunExecution[int]]],
) -> None:
    observations: list[str] = []

    class ShutdownExecution:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        def start(self) -> None:
            pass

        async def await_result(self) -> int:
            try:
                self.entered.set()
                await self.release.wait()
            finally:
                cleanup = asyncio.Event()
                asyncio.get_running_loop().call_soon(cleanup.set)
                await cleanup.wait()
                observations.append("execution-released")
            return 42

        def stop(self) -> None:
            self.release.set()

        def close(self) -> None:
            observations.append("session-closed")

    async def exit_with_active_run() -> None:
        execution = ShutdownExecution()
        runs = implementation(
            lambda _request, _sink: execution,
            identity=lambda request: request.run_id,
            is_resume=lambda request: request.resume,
        )
        runs.start(Request(run_id="one"))
        await execution.entered.wait()

    asyncio.run(exit_with_active_run())
    assert observations == ["execution-released", "session-closed"]


@pytest.mark.asyncio
async def test_cancelled_pending_event_consumer_does_not_stop_execution(launch: Launch) -> None:
    runs, execution = launch
    handle = runs.start(Request(run_id="one"))
    stream = handle.events()
    assert await anext(stream) == 1
    consumer_entered = asyncio.Event()

    async def next_event() -> int:
        consumer_entered.set()
        return await anext(stream)

    consumer = asyncio.create_task(next_event())
    await consumer_entered.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert runs.list_active() == (handle,)
    handle.stop()
    assert await handle.result() == 42
    assert execution.closes == 1
    assert [event async for event in handle.events()] == [1, 2]


@pytest.mark.asyncio
async def test_subscription_setup_failure_closes_without_registering_run(launch: Launch) -> None:
    runs, execution = launch
    execution.start_failure = ValueError("subscription failed")
    with pytest.raises(ValueError, match="subscription failed"):
        runs.start(Request(run_id="one"))
    assert execution.closes == 1
    assert runs.list_active() == ()
    with pytest.raises(KeyError):
        runs.attach("one")


@pytest.mark.parametrize("implementation", [InProcessRuns, FakeRuns], ids=["in-process", "fake"])
def test_shutdown_immediately_after_start_closes_session(
    implementation: type[InProcessRuns[Request, int, int, RunExecution[int]]],
) -> None:
    execution: FakeExecution | None = None

    async def exit_without_awaiting_run() -> None:
        nonlocal execution
        opened = FakeExecution(lambda _event: None, asyncio.Event(), asyncio.Event())
        execution = opened
        runs = implementation(
            lambda _request, _sink: opened,
            identity=lambda request: request.run_id,
            is_resume=lambda request: request.resume,
        )
        runs.start(Request(run_id="one"))

    asyncio.run(exit_without_awaiting_run())
    assert execution is not None
    assert execution.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("implementation", [InProcessRuns, FakeRuns], ids=["in-process", "fake"])
async def test_resumed_run_is_listed_in_current_launch_order(
    implementation: type[InProcessRuns[Request, int, int, RunExecution[int]]],
) -> None:
    def factory(_request: Request, sink: Callable[[int], None]) -> FakeExecution:
        return FakeExecution(sink, asyncio.Event(), asyncio.Event())

    runs = implementation(
        factory,
        identity=lambda request: request.run_id,
        is_resume=lambda request: request.resume,
    )
    first = runs.start(Request(run_id="first"))
    first.stop()
    await first.result()
    second = runs.start(Request(run_id="second"))
    resumed = runs.resume(Request(run_id="first", resume=True))
    try:
        assert runs.list_active() == (second, resumed)
    finally:
        second.stop()
        resumed.stop()
        await second.result()
        await resumed.result()


type ContractRuns = InProcessRuns[Request, int, int, RunExecution[int]]
type ContractHandle = TaskRunHandle[int, int, RunExecution[int]]
type TerminalOutcome = Literal["success", "cancelled", "failed"]


@dataclass
class RunContractSide:
    """One implementation and its independently owned execution resources."""

    runs: ContractRuns
    executions: dict[str, FakeExecution]
    handles: dict[str, ContractHandle] = field(default_factory=dict)
    retired: list[tuple[ContractHandle, TerminalOutcome, list[int]]] = field(default_factory=list)


def run_contract_side(implementation: type[ContractRuns]) -> RunContractSide:
    executions: dict[str, FakeExecution] = {}

    def factory(request: Request, sink: Callable[[int], None]) -> RunExecution[int]:
        execution = FakeExecution(sink, asyncio.Event(), asyncio.Event())
        executions[request.run_id] = execution
        return execution

    return RunContractSide(
        implementation(
            factory,
            identity=lambda request: request.run_id,
            is_resume=lambda request: request.resume,
        ),
        executions,
    )


class RunsContractMachine(RuleBasedStateMachine):
    """Apply generated registry and handle controls to both implementations.

    Awaited execution-entry and terminal-result barriers define every observation;
    no assertion depends on which event-loop task happens to run first.
    """

    def __init__(self) -> None:
        super().__init__()
        self.runner = asyncio.Runner()
        self.sides = [run_contract_side(InProcessRuns), run_contract_side(FakeRuns)]
        self.order: list[str] = []
        self.terminals: dict[str, TerminalOutcome] = {}
        self.history: dict[str, list[int]] = {}

    @rule(
        run_id=st.sampled_from(["a", "b", "c"]),
        resume=st.booleans(),
        cancel_before_entry=st.booleans(),
    )
    def launch(self, run_id: str, *, resume: bool, cancel_before_entry: bool) -> None:
        active = run_id in self.order and run_id not in self.terminals
        duplicate = run_id in self.order and not resume
        request = Request(run_id=run_id, resume=resume)

        async def apply() -> None:
            for side in self.sides:
                operation = side.runs.resume if resume else side.runs.start
                if active or duplicate:
                    with pytest.raises(ValueError, match="already active" if active else "exists"):
                        operation(request)
                    continue
                handle = operation(request)
                if run_id in side.handles:
                    side.retired.append(
                        (side.handles[run_id], self.terminals[run_id], self.history[run_id].copy())
                    )
                side.handles[run_id] = handle
                handle.start()
                if cancel_before_entry:
                    handle.cancel()
                    handle.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await handle.result()
                else:
                    await side.executions[run_id].entered.wait()

        self.runner.run(apply())
        if active or duplicate:
            return
        if run_id in self.order:
            self.order.remove(run_id)
        self.order.append(run_id)
        self.terminals.pop(run_id, None)
        self.history[run_id] = [] if cancel_before_entry else [1]
        if cancel_before_entry:
            self.terminals[run_id] = "cancelled"

    @rule(
        run_id=st.sampled_from(["a", "b", "c"]),
        outcome=st.sampled_from(["success", "cancelled", "failed"]),
        repetitions=st.integers(min_value=1, max_value=4),
    )
    def settle(self, run_id: str, outcome: TerminalOutcome, repetitions: int) -> None:
        if run_id not in self.order or run_id in self.terminals:
            return

        async def apply() -> None:
            for side in self.sides:
                handle = side.handles[run_id]
                execution = side.executions[run_id]
                match outcome:
                    case "success":
                        handle.stop()
                    case "cancelled":
                        for _ in range(repetitions):
                            handle.cancel()
                            handle.stop()
                    case "failed":
                        execution.failure = ValueError("generated execution failure")
                        execution.release.set()
                await self.assert_result(handle, outcome)
                assert execution.starts == 1
                assert execution.closes == 1
                assert execution.stops == int(outcome == "success")

        self.runner.run(apply())
        self.terminals[run_id] = outcome
        if outcome == "success":
            self.history[run_id].append(2)

    @rule(run_id=st.sampled_from(["a", "b", "c", "missing"]))
    def attach(self, run_id: str) -> None:
        for side in self.sides:
            if run_id not in self.order:
                with pytest.raises(KeyError):
                    side.runs.attach(run_id)
            else:
                assert side.runs.attach(run_id) is side.handles[run_id]

    @rule(run_id=st.sampled_from(["a", "b", "c"]), repetitions=st.integers(1, 4))
    def replay_terminal(self, run_id: str, repetitions: int) -> None:
        if run_id not in self.terminals:
            return

        async def apply() -> None:
            for side in self.sides:
                handle = side.handles[run_id]
                for _ in range(repetitions):
                    handle.cancel()
                    handle.stop()
                    handle.start()
                    await self.assert_result(handle, self.terminals[run_id])
                    assert [event async for event in handle.events()] == self.history[run_id]
                assert side.executions[run_id].closes == 1

        self.runner.run(apply())

    @rule()
    def replay_replaced_handles(self) -> None:
        async def apply() -> None:
            for side in self.sides:
                for handle, outcome, history in side.retired:
                    assert not handle.active
                    await self.assert_result(handle, outcome)
                    assert [event async for event in handle.events()] == history

        self.runner.run(apply())

    @invariant()
    def active_registry_has_current_launch_order(self) -> None:
        expected = [run_id for run_id in self.order if run_id not in self.terminals]
        for side in self.sides:
            active = side.runs.list_active()
            assert [handle.run_id for handle in active] == expected
            assert all(handle.active for handle in active)
            assert all(side.runs.attach(handle.run_id) is handle for handle in active)
            for run_id in self.terminals:
                assert not side.handles[run_id].active

    @staticmethod
    async def assert_result(handle: ContractHandle, outcome: TerminalOutcome) -> None:
        match outcome:
            case "success":
                assert await handle.result() == 42
            case "cancelled":
                with pytest.raises(asyncio.CancelledError):
                    await handle.result()
            case "failed":
                with pytest.raises(ValueError, match="generated execution failure"):
                    await handle.result()

    def teardown(self) -> None:
        async def drain() -> None:
            for side in self.sides:
                for handle in side.runs.list_active():
                    handle.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await handle.result()

        try:
            self.runner.run(drain())
        finally:
            self.runner.close()


TestRunsContract = RunsContractMachine.TestCase
TestRunsContract.settings = settings(max_examples=35, stateful_step_count=30)
