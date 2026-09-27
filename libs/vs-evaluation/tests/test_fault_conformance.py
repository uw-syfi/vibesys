"""Fault conformance tests for the public evaluation lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api import (
    EvaluationCompleted,
    EvaluationCoordinator,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvaluationStepResult,
    EvaluationTimedOut,
    ExecutorObservation,
    FilesystemEvaluationStore,
    ResourceRequirements,
    StageState,
    stable_handle_id,
)
from vs_evaluation.api.testing import (
    FakeClock,
    FakeEvaluationBackend,
    FakeEvaluationExecutor,
    InMemoryEvaluationStore,
)

if TYPE_CHECKING:
    from pathlib import Path


def _request(key: str) -> EvaluationRequest:
    return EvaluationRequest(
        key=key,
        stages=(
            EvaluationStep(name="correctness", payload={}),
            EvaluationStep(name="measurement", payload={}),
        ),
        stop_on_failure=True,
    )


def _success() -> ExecutorObservation:
    return ExecutorObservation(
        state=EvaluationState.SUCCEEDED,
        stage_results=(
            EvaluationStepResult(name="correctness", state=StageState.SUCCEEDED),
            EvaluationStepResult(name="measurement", state=StageState.SUCCEEDED),
        ),
    )


def _coordinator(
    executor: FakeEvaluationExecutor,
    store: InMemoryEvaluationStore | FilesystemEvaluationStore,
) -> EvaluationCoordinator:
    return EvaluationCoordinator(executor, store, executor.clock, max_await_timeout_s=20)


@pytest.mark.asyncio
async def test_remote_accept_then_local_error_reconciles_without_duplicate_submission() -> None:
    clock = FakeClock()
    backend = FakeEvaluationBackend()
    executor = FakeEvaluationExecutor(clock, backend=backend, fail_after_accept_once=True)
    service = _coordinator(executor, InMemoryEvaluationStore())

    handle = await service.submit(_request("ambiguous-accept"))
    assert (await handle.snapshot()).submission_pending is False

    duplicate = await service.submit(_request("ambiguous-accept"))

    assert duplicate.id == handle.id
    assert len(backend.submissions) == 1


@pytest.mark.asyncio
async def test_repeated_provider_observation_timeouts_do_not_poison_later_completion() -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock)
    handle = await _coordinator(executor, InMemoryEvaluationStore()).submit(
        _request("transient-observation-timeout")
    )
    executor.set_observation(handle.id, _success())
    executor.timeout_next_inspections(2)

    for _ in range(2):
        with pytest.raises(TimeoutError):
            await handle.status()

    assert await handle.status() is EvaluationState.SUCCEEDED
    assert isinstance(await handle.await_result(1), EvaluationCompleted)


@pytest.mark.asyncio
async def test_repeated_nonterminal_change_waits_can_finish_within_one_await_budget() -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock)
    handle = await _coordinator(executor, InMemoryEvaluationStore()).submit(
        _request("repeated-nonterminal-waits")
    )
    executor.script_wait_timeout(1, count=2)
    executor.script_wait_transition(_success(), elapsed_s=1)

    result = await handle.await_result(10)

    assert isinstance(result, EvaluationCompleted)
    assert clock.monotonic() == 3
    assert len(executor.wait_calls) == 4


@pytest.mark.asyncio
async def test_caller_await_timeout_preserves_work_for_later_completion() -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock)
    handle = await _coordinator(executor, InMemoryEvaluationStore()).submit(
        _request("caller-timeout")
    )

    first = await handle.await_result(2)
    executor.set_observation(handle.id, _success())
    second = await handle.await_result(2)

    assert isinstance(first, EvaluationTimedOut)
    assert first.status is EvaluationState.QUEUED
    assert isinstance(second, EvaluationCompleted)
    assert len(executor.submissions) == 1


@pytest.mark.asyncio
async def test_duplicate_observation_is_idempotent_and_stale_observation_is_rejected() -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock)
    handle = await _coordinator(executor, InMemoryEvaluationStore()).submit(
        _request("stale-observation")
    )
    running = ExecutorObservation(
        state=EvaluationState.RUNNING,
        current_stage="correctness",
    )
    executor.set_observation(handle.id, running)
    first = await handle.snapshot()
    executor.script_observations(
        running,
        ExecutorObservation(
            state=EvaluationState.STARTING,
            current_stage="correctness",
        ),
    )

    duplicate = await handle.snapshot()
    with pytest.raises(RuntimeError, match="regressed"):
        await handle.status()

    recovered = await handle.snapshot()
    assert duplicate.revision == first.revision == recovered.revision
    assert recovered.state is EvaluationState.RUNNING


@pytest.mark.asyncio
async def test_cancellation_and_failure_release_executor_capacity() -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock)
    service = _coordinator(executor, InMemoryEvaluationStore())
    requirements = ResourceRequirements()

    canceled = await service.submit(_request("capacity-canceled"))
    assert (await service.availability(requirements)).in_flight == 1
    await canceled.cancel()
    assert (await service.availability(requirements)).in_flight == 0

    failed = await service.submit(_request("capacity-failed"))
    assert (await service.availability(requirements)).in_flight == 1
    executor.set_state(
        failed.id,
        EvaluationState.FAILED,
        failure="provider failed",
        stage_results=(
            EvaluationStepResult(
                name="correctness",
                state=StageState.FAILED,
                failure="check failed",
            ),
            EvaluationStepResult(name="measurement", state=StageState.SKIPPED),
        ),
    )
    await failed.status()

    assert (await service.availability(requirements)).in_flight == 0


@pytest.mark.asyncio
async def test_restart_inspects_shared_remote_state_without_duplicate_submission(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    backend = FakeEvaluationBackend()
    first = _coordinator(
        FakeEvaluationExecutor(clock, backend=backend),
        FilesystemEvaluationStore(tmp_path),
    )
    original = await first.submit(_request("restart-shared-provider"))

    resumed = _coordinator(
        FakeEvaluationExecutor(clock, backend=backend),
        FilesystemEvaluationStore(tmp_path),
    )
    (recovered,) = await resumed.reconcile()

    assert recovered.id == original.id
    assert await recovered.status() is EvaluationState.QUEUED
    assert len(backend.submissions) == 1


@pytest.mark.asyncio
async def test_restart_retries_durable_cancellation_before_submission(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FakeEvaluationBackend()
    store = FilesystemEvaluationStore(tmp_path)
    first_executor = FakeEvaluationExecutor(
        clock,
        backend=backend,
        fail_cancel_once=True,
    )
    first = _coordinator(first_executor, store)
    handle = await first.submit(_request("cancel-crash-window"))

    with pytest.raises(OSError, match=r"^$"):
        await handle.cancel()
    canceled = await store.get(handle.id)
    assert canceled is not None
    assert canceled.cancel_requested is True

    resumed_executor = FakeEvaluationExecutor(clock, backend=backend)
    resumed = _coordinator(resumed_executor, FilesystemEvaluationStore(tmp_path))
    (recovered,) = await resumed.reconcile()

    assert await recovered.status() is EvaluationState.CANCELED
    assert resumed_executor.cancellations == [handle.id]
    assert len(backend.submissions) == 1


@pytest.mark.parametrize("provider_accepted", [False, True])
@pytest.mark.asyncio
async def test_restart_retries_pending_submission_despite_orphan_queued_observation(
    tmp_path: Path,
    *,
    provider_accepted: bool,
) -> None:
    clock = FakeClock()
    store = FilesystemEvaluationStore(tmp_path)
    pending = _request("restart-orphan-queued")
    handle_id = stable_handle_id(pending.key)
    await store.claim(pending, handle_id=handle_id)

    backend = FakeEvaluationBackend()
    if provider_accepted:
        backend.accept(handle_id, pending)
    executor = FakeEvaluationExecutor(clock, backend=backend)
    executor.script_observations(ExecutorObservation(state=EvaluationState.QUEUED))
    resumed = _coordinator(executor, FilesystemEvaluationStore(tmp_path))

    (recovered,) = await resumed.reconcile()

    assert recovered.id == handle_id
    assert (await recovered.snapshot()).submission_pending is False
    assert len(backend.submissions) == 1
