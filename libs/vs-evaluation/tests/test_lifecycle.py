"""Public lifecycle contract tests for provider-neutral evaluation work."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api import (
    AvailabilityState,
    EvaluationCanceled,
    EvaluationCompleted,
    EvaluationCoordinator,
    EvaluationFailed,
    EvaluationLifecycleEvent,
    EvaluationLifecyclePhase,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvaluationStepResult,
    EvaluationTimedOut,
    ExecutorObservation,
    FilesystemEvaluationStore,
    ResourceRequirements,
    StageState,
    StoredEvaluation,
)
from vs_evaluation.api.testing import (
    FakeClock,
    FakeDeadlineFactory,
    FakeEvaluationExecutor,
    InMemoryEvaluationStore,
)

if TYPE_CHECKING:
    from pathlib import Path


def request(key: str = "stable-work-key") -> EvaluationRequest:
    """Build one ordered multi-stage request."""
    return EvaluationRequest(
        key=key,
        stages=(
            EvaluationStep(name="correctness", payload={"argv": ["check"]}),
            EvaluationStep(name="measurement", payload={"argv": ["measure"]}),
        ),
        stop_on_failure=True,
    )


def coordinator(
    executor: FakeEvaluationExecutor,
    store: InMemoryEvaluationStore | FilesystemEvaluationStore,
    *,
    events: list[EvaluationLifecycleEvent] | None = None,
) -> EvaluationCoordinator:
    """Create a coordinator with the executor's clock."""
    if events is None:
        return EvaluationCoordinator(executor, store, executor.clock, max_await_timeout_s=20)
    return EvaluationCoordinator(
        executor,
        store,
        executor.clock,
        max_await_timeout_s=20,
        events=events.append,
    )


@pytest.mark.asyncio
async def test_lifecycle_events_publish_revisioned_durable_changes_and_wait_timeout() -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock)
    observed: list[EvaluationLifecycleEvent] = []
    service = coordinator(executor, InMemoryEvaluationStore(), events=observed)

    handle = await service.submit(request("observed-key"))
    executor.set_state(handle.id, EvaluationState.RUNNING, current_stage="correctness")
    await handle.status()
    executor.set_state(
        handle.id,
        EvaluationState.RUNNING,
        current_stage="measurement",
        stage_results=(EvaluationStepResult(name="correctness", state=StageState.SUCCEEDED),),
    )
    await handle.status()
    await handle.status()
    timed_out = await handle.await_result(2)

    assert isinstance(timed_out, EvaluationTimedOut)
    assert [event.phase for event in observed] == [
        EvaluationLifecyclePhase.SUBMITTED,
        EvaluationLifecyclePhase.QUEUED,
        EvaluationLifecyclePhase.RUNNING,
        EvaluationLifecyclePhase.RUNNING,
        EvaluationLifecyclePhase.TIMED_OUT,
    ]
    assert [event.revision for event in observed[:-1]] == [0, 1, 2, 3]
    assert observed[2].current_stage == "correctness"
    assert observed[3].stage_results[0].name == "correctness"
    assert observed[-1].revision == 3
    assert observed[-1].state is EvaluationState.RUNNING


@pytest.mark.asyncio
async def test_submit_deduplicates_by_stable_key_and_reuses_terminal_result() -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock)
    store = InMemoryEvaluationStore()
    service = coordinator(executor, store)

    first = await service.submit(request())
    duplicate = await service.submit(request())
    executor.set_state(
        first.id,
        EvaluationState.SUCCEEDED,
        stage_results=(
            EvaluationStepResult(
                name="correctness",
                state=StageState.SUCCEEDED,
                result={"passed": True},
                duration_s=2,
            ),
            EvaluationStepResult(
                name="measurement", state=StageState.SUCCEEDED, result={"score": 3.5}, duration_s=5
            ),
        ),
    )
    result = await first.await_result(10)
    after_completion = await service.submit(request())

    assert duplicate.id == first.id == after_completion.id
    assert len(executor.submissions) == 1
    assert isinstance(result, EvaluationCompleted)
    assert [stage.name for stage in result.stages] == ["correctness", "measurement"]
    assert result.stages[0].result == {"passed": True}


@pytest.mark.asyncio
async def test_history_is_read_only_and_includes_zero_or_all_durable_submissions() -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock)
    service = coordinator(executor, InMemoryEvaluationStore())

    assert await service.history() == ()

    handle = await service.submit(request("history-key"))
    history = await service.history()

    assert tuple(record.handle_id for record in history) == (handle.id,)
    assert len(executor.submissions) == 1

    repeated = await service.history()
    assert repeated == history
    assert len(executor.submissions) == 1


@pytest.mark.asyncio
async def test_bounded_await_returns_timed_out_and_enforces_maximum() -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock)
    handle = await coordinator(executor, InMemoryEvaluationStore()).submit(request())

    result = await handle.await_result(3.5)

    assert isinstance(result, EvaluationTimedOut)
    assert result.status is not None
    assert result.status.value == "queued"
    assert clock.monotonic() == 3.5
    with pytest.raises(ValueError, match="maximum"):
        await handle.await_result(20.1)
    with pytest.raises(ValueError, match="positive"):
        await handle.await_result(0)


@pytest.mark.asyncio
async def test_await_wakes_on_completion_without_wall_clock_wait() -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock, advance_clock_on_timeout=False)
    handle = await coordinator(executor, InMemoryEvaluationStore()).submit(request())

    waiting = asyncio.create_task(handle.await_result(5))
    await executor.wait_started.wait()
    executor.set_state(
        handle.id,
        EvaluationState.SUCCEEDED,
        stage_results=(
            EvaluationStepResult(name="correctness", state=StageState.SUCCEEDED),
            EvaluationStepResult(name="measurement", state=StageState.SUCCEEDED),
        ),
    )
    result = await waiting

    assert isinstance(result, EvaluationCompleted)
    assert clock.monotonic() == 0


@pytest.mark.asyncio
async def test_cancel_and_failure_have_explicit_await_outcomes() -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock)
    service = coordinator(executor, InMemoryEvaluationStore())
    canceled = await service.submit(request("cancel-key"))
    await canceled.cancel()
    failed = await service.submit(request("failure-key"))
    executor.set_state(
        failed.id,
        EvaluationState.FAILED,
        failure="correctness stage failed",
        stage_results=(
            EvaluationStepResult(
                name="correctness",
                state=StageState.FAILED,
                failure="assertion failed",
                duration_s=1,
            ),
            EvaluationStepResult(name="measurement", state=StageState.SKIPPED),
        ),
    )

    canceled_result = await canceled.await_result(5)
    failed_result = await failed.await_result(5)

    assert isinstance(canceled_result, EvaluationCanceled)
    assert isinstance(failed_result, EvaluationFailed)
    assert failed_result.message == "correctness stage failed"


@pytest.mark.asyncio
async def test_availability_is_typed_and_freshness_uses_injected_clock() -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(
        clock,
        availability_state=AvailabilityState.DELAYED,
        capacity=8,
        in_flight=7,
        queue_depth=4,
        estimated_start_after_s=12,
        estimated_runtime_s=15,
        supported_evidence_kinds=("metrics",),
        supported_capabilities=("cpu",),
        fresh_for_s=5,
    )

    snapshot = await coordinator(executor, InMemoryEvaluationStore()).availability(
        ResourceRequirements(cpu_cores=2)
    )
    clock.advance(6)

    assert snapshot.state is AvailabilityState.DELAYED
    assert snapshot.in_flight == 7
    assert snapshot.queue_depth == 4
    assert not snapshot.is_fresh(clock.monotonic())


@pytest.mark.asyncio
async def test_reconcile_reuses_durable_handle_after_store_reopen(tmp_path: Path) -> None:
    clock = FakeClock()
    first_executor = FakeEvaluationExecutor(clock)
    first_store = FilesystemEvaluationStore(tmp_path)
    first_service = coordinator(first_executor, first_store)
    first_handle = await first_service.submit(request("resume-key"))

    resumed_executor = FakeEvaluationExecutor(clock)
    resumed_store = FilesystemEvaluationStore(tmp_path)
    resumed_service = coordinator(resumed_executor, resumed_store)
    recovered = await resumed_service.reconcile()

    assert len(recovered) == 1
    assert recovered[0].id == first_handle.id
    assert len(resumed_executor.submissions) == 1
    stored = await resumed_store.get_by_key("resume-key")
    assert stored is not None
    assert stored.handle_id == first_handle.id


@pytest.mark.asyncio
async def test_filesystem_store_claim_is_atomic_across_store_instances(tmp_path: Path) -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock)
    stores = [FilesystemEvaluationStore(tmp_path), FilesystemEvaluationStore(tmp_path)]
    services = [coordinator(executor, store) for store in stores]

    handles = await asyncio.gather(*(service.submit(request("shared-key")) for service in services))

    assert handles[0].id == handles[1].id
    assert len(executor.submissions) == 1


@pytest.mark.asyncio
async def test_ambiguous_submit_error_is_reconciled_without_duplicate_execution() -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock, fail_after_accept_once=True)
    service = coordinator(executor, InMemoryEvaluationStore())

    handle = await service.submit(request("ambiguous-key"))
    status = await handle.status()

    assert status.value == "queued"
    assert len(executor.submissions) == 1


@pytest.mark.asyncio
async def test_key_reuse_with_different_request_is_rejected() -> None:
    clock = FakeClock()
    service = coordinator(FakeEvaluationExecutor(clock), InMemoryEvaluationStore())
    await service.submit(request())
    changed = EvaluationRequest(
        key="stable-work-key",
        stages=(EvaluationStep(name="different", payload={}),),
    )

    with pytest.raises(ValueError, match="different request"):
        await service.submit(changed)


@pytest.mark.asyncio
async def test_status_rejects_executor_state_regression() -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock)
    handle = await coordinator(executor, InMemoryEvaluationStore()).submit(request())
    executor.set_state(handle.id, EvaluationState.RUNNING, current_stage="correctness")
    await handle.status()
    executor.set_state(handle.id, EvaluationState.STARTING, current_stage="correctness")

    with pytest.raises(RuntimeError, match="regressed"):
        await handle.status()


class BlockingInspection(FakeEvaluationExecutor):
    """Hold an executor inspection at a deterministic synchronization point."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.block = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        """Block only after the initial submission has been accepted."""
        if self.block:
            self.started.set()
            await self.release.wait()
        return await super().inspect(handle_id)


class BlockingStore(InMemoryEvaluationStore):
    """Hold an authoritative read before any status becomes available."""

    def __init__(self) -> None:
        super().__init__()
        self.block = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get(self, handle_id: str) -> StoredEvaluation | None:
        """Block only when the test arms the initial await read."""
        if self.block:
            self.started.set()
            await self.release.wait()
        return await super().get(handle_id)


class TransitionAfterInspection(FakeEvaluationExecutor):
    """Publish completion after returning an older inspected state."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.complete_after_inspect = False

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        """Make the inspect-to-wait race deterministic."""
        observed = await super().inspect(handle_id)
        if self.complete_after_inspect:
            self.complete_after_inspect = False
            self.set_state(
                handle_id,
                EvaluationState.SUCCEEDED,
                stage_results=(
                    EvaluationStepResult(name="correctness", state=StageState.SUCCEEDED),
                    EvaluationStepResult(name="measurement", state=StageState.SUCCEEDED),
                ),
            )
        return observed


@pytest.mark.asyncio
async def test_await_deadline_interrupts_blocked_inspection_and_keeps_durable_status() -> None:
    clock = FakeClock()
    executor = BlockingInspection(clock)
    deadlines = FakeDeadlineFactory()
    service = EvaluationCoordinator(
        executor,
        InMemoryEvaluationStore(),
        clock,
        deadline_factory=deadlines,
    )
    handle = await service.submit(request())
    executor.block = True

    waiting = asyncio.create_task(handle.await_result(5))
    await executor.started.wait()
    deadlines.scopes[0].expire()
    result = await waiting

    assert isinstance(result, EvaluationTimedOut)
    assert result.status is EvaluationState.QUEUED
    assert deadlines.scopes[0].requested_s == 5
    executor.block = False
    executor.set_state(
        handle.id,
        EvaluationState.SUCCEEDED,
        stage_results=(
            EvaluationStepResult(name="correctness", state=StageState.SUCCEEDED),
            EvaluationStepResult(name="measurement", state=StageState.SUCCEEDED),
        ),
    )
    assert isinstance(await handle.await_result(5), EvaluationCompleted)


@pytest.mark.asyncio
async def test_await_deadline_before_first_store_read_has_no_status() -> None:
    clock = FakeClock()
    store = BlockingStore()
    deadlines = FakeDeadlineFactory()
    service = EvaluationCoordinator(
        FakeEvaluationExecutor(clock), store, clock, deadline_factory=deadlines
    )
    handle = await service.submit(request())
    store.block = True

    waiting = asyncio.create_task(handle.await_result(5))
    await store.started.wait()
    deadlines.scopes[0].expire()
    result = await waiting

    assert isinstance(result, EvaluationTimedOut)
    assert result.status is None
    store.block = False
    assert await handle.status() is EvaluationState.QUEUED


@pytest.mark.asyncio
async def test_await_observes_change_published_between_inspect_and_wait() -> None:
    clock = FakeClock()
    executor = TransitionAfterInspection(clock)
    handle = await coordinator(executor, InMemoryEvaluationStore()).submit(request())
    executor.complete_after_inspect = True

    result = await handle.await_result(5)

    assert isinstance(result, EvaluationCompleted)
    assert executor.wait_calls


@pytest.mark.asyncio
async def test_provider_timeout_error_is_not_reported_as_await_deadline() -> None:
    class ProviderTimeout(FakeEvaluationExecutor):
        def __init__(self, clock: FakeClock) -> None:
            super().__init__(clock)
            self.fail_inspection = False

        async def inspect(self, handle_id: str) -> ExecutorObservation | None:
            if self.fail_inspection:
                raise TimeoutError
            return await super().inspect(handle_id)

    clock = FakeClock()
    executor = ProviderTimeout(clock)
    deadlines = FakeDeadlineFactory()
    service = EvaluationCoordinator(
        executor,
        InMemoryEvaluationStore(),
        clock,
        deadline_factory=deadlines,
    )
    handle = await service.submit(request())
    executor.fail_inspection = True

    with pytest.raises(TimeoutError):
        await handle.await_result(5)
