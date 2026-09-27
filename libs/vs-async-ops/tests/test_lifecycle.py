"""Public lifecycle contract for resource-neutral asynchronous operations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from vs_async_ops.api import (
    OperationCancellationTimeoutError,
    OperationCompleted,
    OperationCoordinator,
    OperationCoordinatorClosedError,
    OperationHandle,
    OperationObservationTimeoutError,
    OperationPolicy,
    OperationRequest,
    OperationState,
    OperationTimedOut,
)
from vs_async_ops.api.testing import (
    FakeOperationDeadlineFactory,
    FakeOperationRunner,
    ImmediateTimeoutWaiter,
    InMemoryOperationStore,
    ObservingWaiter,
)

if TYPE_CHECKING:
    from pydantic import JsonValue


class _BlockingStore:
    """Faithful store wrapper with deterministic read suspension."""

    def __init__(self, *, block_create: bool = False, block_records: bool = False) -> None:
        self.delegate = InMemoryOperationStore()
        self.block_create = block_create
        self.block_get = False
        self.block_records = block_records
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def create(self, record: OperationHandle) -> None:
        if self.block_create:
            self.entered.set()
            await self.release.wait()
        await self.delegate.create(record)

    async def get(self, operation_id: str) -> OperationHandle | None:
        if self.block_get:
            self.entered.set()
            await self.release.wait()
        return await self.delegate.get(operation_id)

    async def replace(self, record: OperationHandle, *, expected_revision: int) -> None:
        await self.delegate.replace(record, expected_revision=expected_revision)

    async def records(self) -> tuple[OperationHandle, ...]:
        if self.block_records:
            self.entered.set()
            await self.release.wait()
        return await self.delegate.records()


class _CancellationRunner:
    """Expose runner cancellation and task-unwind order."""

    def __init__(self, *, block_cancel: bool = False, fail_cancel: bool = False) -> None:
        self.started = asyncio.Event()
        self.cancel_entered = asyncio.Event()
        self.cancel_release = asyncio.Event()
        self.block_cancel = block_cancel
        self.fail_cancel = fail_cancel
        self.events: list[str] = []

    async def run(self, request: OperationRequest) -> JsonValue:
        del request
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.events.append("task-canceled")
            raise

    async def cancel(self, operation_id: str) -> None:
        del operation_id
        self.events.append("runner-cancel")
        self.cancel_entered.set()
        if self.fail_cancel:
            message = "provider cancellation failed"
            raise RuntimeError(message)
        if self.block_cancel:
            await self.cancel_release.wait()


class _FailingEvents:
    def __init__(self) -> None:
        self.errors: list[tuple[Exception, object]] = []

    def publish(self, event: object) -> None:
        del event
        message = "observer failed"
        raise RuntimeError(message)

    def report(self, error: Exception, event: object) -> None:
        self.errors.append((error, event))


class _CountingWaiter:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.count = 0
        self.ready = asyncio.Event()

    async def wait(self, event: asyncio.Event, timeout_s: float) -> bool:
        del timeout_s
        self.count += 1
        if self.count == self.expected:
            self.ready.set()
        await event.wait()
        return True


def _request(operation_id: str, key: str = "session-a") -> OperationRequest:
    return OperationRequest(
        operation_id=operation_id,
        concurrency_key=key,
        payload={"request": operation_id},
    )


@pytest.mark.asyncio
async def test_submit_is_nonblocking_and_timeout_is_observational() -> None:
    runner = FakeOperationRunner()
    coordinator = OperationCoordinator(
        runner,
        InMemoryOperationStore(),
        waiter=ImmediateTimeoutWaiter(),
    )

    submitted = await coordinator.submit(_request("one"))
    observation = await coordinator.await_result("one", 10)

    assert submitted.state is OperationState.QUEUED
    assert isinstance(observation, OperationTimedOut)
    assert (await coordinator.status("one")).state in {
        OperationState.QUEUED,
        OperationState.RUNNING,
    }
    runner.complete("one", {"answer": 1})
    await runner.wait_started("one")
    completed = await coordinator.await_result("one", 10)
    assert isinstance(completed, OperationCompleted)
    assert completed.record.result == {"answer": 1}


@pytest.mark.asyncio
async def test_running_transition_does_not_finish_a_bounded_await() -> None:
    runner = FakeOperationRunner()
    waiter = ObservingWaiter()
    coordinator = OperationCoordinator(
        runner,
        InMemoryOperationStore(),
        waiter=waiter,
    )
    await coordinator.submit(_request("one"))
    pending = asyncio.create_task(coordinator.await_result("one", 10))
    await waiter.entered.wait()
    await runner.wait_started("one")
    assert not pending.done()

    runner.complete("one", {"answer": 1})
    observed = await pending
    assert isinstance(observed, OperationCompleted)


@pytest.mark.asyncio
async def test_terminal_signal_wakes_all_waiters_and_late_waiter_reads_store() -> None:
    runner = FakeOperationRunner()
    waiter = _CountingWaiter(3)
    coordinator = OperationCoordinator(runner, InMemoryOperationStore(), waiter=waiter)
    await coordinator.submit(_request("many-waiters"))
    await runner.wait_started("many-waiters")
    waiters = [asyncio.create_task(coordinator.await_result("many-waiters", 10)) for _ in range(3)]
    await waiter.ready.wait()
    runner.complete("many-waiters", {"done": True})

    observed = await asyncio.gather(*waiters)
    late = await coordinator.await_result("many-waiters", 10)

    assert all(isinstance(item, OperationCompleted) for item in observed)
    assert isinstance(late, OperationCompleted)


@pytest.mark.asyncio
async def test_same_key_serializes_while_distinct_keys_run_concurrently() -> None:
    runner = FakeOperationRunner()
    coordinator = OperationCoordinator(runner, InMemoryOperationStore())
    await coordinator.submit(_request("a1", "a"))
    await coordinator.submit(_request("a2", "a"))
    await coordinator.submit(_request("b1", "b"))

    await runner.wait_started("a1")
    await runner.wait_started("b1")
    assert "a2" not in runner.started
    assert runner.max_active == 2

    runner.complete("a1", {"done": "a1"})
    await runner.wait_started("a2")
    runner.complete("a2", {"done": "a2"})
    runner.complete("b1", {"done": "b1"})
    await asyncio.gather(
        coordinator.await_result("a2", 10),
        coordinator.await_result("b1", 10),
    )


@pytest.mark.asyncio
async def test_cancel_is_terminal_and_restart_interrupts_orphans() -> None:
    runner = FakeOperationRunner()
    store = InMemoryOperationStore()
    coordinator = OperationCoordinator(runner, store)
    await coordinator.submit(_request("cancel"))
    await runner.wait_started("cancel")
    canceled = await coordinator.cancel("cancel")
    assert canceled.state is OperationState.CANCELED
    assert runner.canceled == ["cancel"]

    orphan = OperationHandle(request=_request("orphan"), state=OperationState.RUNNING)
    restarted = OperationCoordinator(FakeOperationRunner(), InMemoryOperationStore((orphan,)))
    await restarted.start()
    assert (await restarted.status("orphan")).state is OperationState.INTERRUPTED

    closing_runner = FakeOperationRunner()
    closing = OperationCoordinator(closing_runner, InMemoryOperationStore())
    await closing.submit(_request("close-running", "close"))
    await closing.submit(_request("close-queued", "close"))
    await closing_runner.wait_started("close-running")
    await closing.close()
    assert (await closing.status("close-running")).state is OperationState.INTERRUPTED
    assert (await closing.status("close-queued")).state is OperationState.INTERRUPTED
    assert closing_runner.canceled == ["close-running"]


@pytest.mark.asyncio
async def test_global_limit_and_queued_cancel_are_enforced() -> None:
    runner = FakeOperationRunner()
    coordinator = OperationCoordinator(
        runner,
        InMemoryOperationStore(),
        policy=OperationPolicy(max_in_flight=2),
    )
    await coordinator.submit(_request("one", "one"))
    await coordinator.submit(_request("two", "two"))
    await coordinator.submit(_request("three", "three"))
    await runner.wait_started("one")
    await runner.wait_started("two")
    assert "three" not in runner.started
    assert runner.max_active == 2

    runner.complete("one", {"done": 1})
    await runner.wait_started("three")
    runner.complete("two", {"done": 2})
    runner.complete("three", {"done": 3})
    await asyncio.gather(coordinator.await_result("two", 10), coordinator.await_result("three", 10))

    serial = OperationCoordinator(runner, InMemoryOperationStore())
    await serial.submit(_request("running", "serial"))
    await serial.submit(_request("queued", "serial"))
    await runner.wait_started("running")
    queued = await serial.cancel("queued")
    assert queued.state is OperationState.CANCELED
    assert "queued" not in runner.canceled
    await serial.cancel("running")
    assert "running" in runner.canceled


@pytest.mark.asyncio
async def test_canceling_a_key_waiter_allows_the_key_to_be_reused() -> None:
    runner = FakeOperationRunner()
    coordinator = OperationCoordinator(runner, InMemoryOperationStore())
    await coordinator.submit(_request("running", "shared"))
    await coordinator.submit(_request("waiting", "shared"))
    await runner.wait_started("running")

    canceled = await coordinator.cancel("waiting")
    runner.complete("running", {"done": "running"})
    await coordinator.await_result("running", 10)
    await coordinator.submit(_request("replacement", "shared"))
    await runner.wait_started("replacement")
    runner.complete("replacement", {"done": "replacement"})

    replacement = await coordinator.await_result("replacement", 10)
    assert canceled.state is OperationState.CANCELED
    assert isinstance(replacement, OperationCompleted)
    assert runner.started == ["running", "replacement"]


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_invalid_global_limit_is_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="max_in_flight"):
        OperationPolicy(max_in_flight=cast("int", value))


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
def test_invalid_cancellation_timeout_is_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="cancellation_timeout_s"):
        OperationPolicy(cancellation_timeout_s=value)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
async def test_invalid_await_timeout_is_rejected(value: float) -> None:
    coordinator = OperationCoordinator(FakeOperationRunner(), InMemoryOperationStore())
    await coordinator.submit(_request("one"))
    with pytest.raises(ValueError, match="timeout_s"):
        await coordinator.await_result("one", value)


@pytest.mark.asyncio
async def test_configured_await_maximum_is_enforced() -> None:
    coordinator = OperationCoordinator(
        FakeOperationRunner(),
        InMemoryOperationStore(),
        policy=OperationPolicy(max_await_timeout_s=5),
    )
    await coordinator.submit(_request("one"))
    with pytest.raises(ValueError, match="at most 5"):
        await coordinator.await_result("one", 6)


@pytest.mark.asyncio
async def test_json_null_is_a_present_successful_result() -> None:
    runner = FakeOperationRunner()
    coordinator = OperationCoordinator(runner, InMemoryOperationStore())
    await coordinator.submit(_request("null"))
    await runner.wait_started("null")
    runner.complete("null", None)

    completed = await coordinator.await_result("null", 10)

    assert isinstance(completed, OperationCompleted)
    assert completed.record.result_present is True
    assert completed.record.result is None


@pytest.mark.asyncio
async def test_event_sink_failure_is_reported_without_controlling_work() -> None:
    runner = FakeOperationRunner()
    observer = _FailingEvents()
    coordinator = OperationCoordinator(
        runner,
        InMemoryOperationStore(),
        events=observer.publish,
        event_errors=observer.report,
    )
    await coordinator.submit(_request("event-error"))
    await runner.wait_started("event-error")
    runner.complete("event-error", {"done": True})

    completed = await coordinator.await_result("event-error", 10)

    assert isinstance(completed, OperationCompleted)
    assert len(observer.errors) == 3


@pytest.mark.asyncio
async def test_close_cancels_runner_before_task_and_rejects_later_submit() -> None:
    runner = _CancellationRunner()
    coordinator = OperationCoordinator(runner, InMemoryOperationStore())
    await coordinator.submit(_request("closing"))
    await runner.started.wait()

    await coordinator.close()

    assert runner.events == ["runner-cancel", "task-canceled"]
    with pytest.raises(OperationCoordinatorClosedError):
        await coordinator.submit(_request("too-late"))


@pytest.mark.asyncio
async def test_concurrent_cancel_is_idempotent() -> None:
    runner = _CancellationRunner()
    coordinator = OperationCoordinator(runner, InMemoryOperationStore())
    await coordinator.submit(_request("cancel-race"))
    await runner.started.wait()

    results = await asyncio.gather(
        coordinator.cancel("cancel-race"), coordinator.cancel("cancel-race")
    )

    assert {item.state for item in results} == {OperationState.CANCELED}
    assert runner.events == ["runner-cancel", "task-canceled"]


@pytest.mark.asyncio
async def test_runner_cancel_failure_still_terminates_task_and_record() -> None:
    runner = _CancellationRunner(fail_cancel=True)
    coordinator = OperationCoordinator(runner, InMemoryOperationStore())
    await coordinator.submit(_request("cancel-error"))
    await runner.started.wait()

    with pytest.raises(RuntimeError, match="provider cancellation failed"):
        await coordinator.cancel("cancel-error")

    assert runner.events == ["runner-cancel", "task-canceled"]
    assert (await coordinator.status("cancel-error")).state is OperationState.CANCELED


@pytest.mark.asyncio
async def test_deadline_covers_store_observation_without_unbounded_fallback() -> None:
    store = _BlockingStore()
    runner = FakeOperationRunner()
    deadlines = FakeOperationDeadlineFactory()
    coordinator = OperationCoordinator(runner, store, deadline_factory=deadlines)
    await coordinator.submit(_request("slow-read"))
    await runner.wait_started("slow-read")
    store.block_get = True

    waiting = asyncio.create_task(coordinator.await_result("slow-read", 5))
    await store.entered.wait()
    deadlines.scopes[-1].expire()
    observed = await waiting

    assert isinstance(observed, OperationTimedOut)
    assert observed.record.request.operation_id == "slow-read"
    store.block_get = False
    await coordinator.cancel("slow-read")


@pytest.mark.asyncio
async def test_deadline_reports_when_no_record_was_observed() -> None:
    store = _BlockingStore(block_records=True)
    deadlines = FakeOperationDeadlineFactory()
    coordinator = OperationCoordinator(FakeOperationRunner(), store, deadline_factory=deadlines)

    waiting = asyncio.create_task(coordinator.await_result("unknown", 5))
    await store.entered.wait()
    deadlines.scopes[-1].expire()

    with pytest.raises(OperationObservationTimeoutError):
        await waiting


@pytest.mark.asyncio
async def test_cancellation_deadline_records_terminal_state() -> None:
    runner = _CancellationRunner(block_cancel=True)
    deadlines = FakeOperationDeadlineFactory()
    coordinator = OperationCoordinator(
        runner,
        InMemoryOperationStore(),
        deadline_factory=deadlines,
        policy=OperationPolicy(cancellation_timeout_s=3),
    )
    await coordinator.submit(_request("stuck-cancel"))
    await runner.started.wait()

    canceling = asyncio.create_task(coordinator.cancel("stuck-cancel"))
    await runner.cancel_entered.wait()
    deadlines.scopes[-1].expire()

    with pytest.raises(OperationCancellationTimeoutError):
        await canceling
    assert (await coordinator.status("stuck-cancel")).state is OperationState.CANCELED


@pytest.mark.asyncio
async def test_close_deadline_interrupts_when_runner_cancel_is_stuck() -> None:
    runner = _CancellationRunner(block_cancel=True)
    deadlines = FakeOperationDeadlineFactory()
    coordinator = OperationCoordinator(
        runner,
        InMemoryOperationStore(),
        deadline_factory=deadlines,
        policy=OperationPolicy(cancellation_timeout_s=3),
    )
    await coordinator.submit(_request("stuck-close"))
    await runner.started.wait()

    closing = asyncio.create_task(coordinator.close())
    await runner.cancel_entered.wait()
    deadlines.scopes[-1].expire()

    with pytest.raises(OperationCancellationTimeoutError):
        await closing
    assert (await coordinator.status("stuck-close")).state is OperationState.INTERRUPTED


@pytest.mark.asyncio
async def test_close_submit_race_finishes_accepted_work_and_closes_admission() -> None:
    store = _BlockingStore(block_create=True)
    coordinator = OperationCoordinator(FakeOperationRunner(), store)
    submitting = asyncio.create_task(coordinator.submit(_request("submit-race")))
    await store.entered.wait()
    closing = asyncio.create_task(coordinator.close())
    store.release.set()

    await submitting
    await closing

    assert (await coordinator.status("submit-race")).state is OperationState.INTERRUPTED
    with pytest.raises(OperationCoordinatorClosedError):
        await coordinator.submit(_request("after-race"))


@pytest.mark.asyncio
async def test_reusing_retired_key_never_allows_overlap() -> None:
    runner = FakeOperationRunner()
    coordinator = OperationCoordinator(runner, InMemoryOperationStore())

    for index in range(20):
        operation_id = f"reuse-{index}"
        await coordinator.submit(_request(operation_id, "reused-key"))
        await runner.wait_started(operation_id)
        runner.complete(operation_id, {"index": index})
        assert isinstance(await coordinator.await_result(operation_id, 10), OperationCompleted)

    assert runner.max_active == 1
