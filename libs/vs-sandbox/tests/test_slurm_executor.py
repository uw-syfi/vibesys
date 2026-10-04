from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

import vs_evaluation.api.testing as evaluation_testing
from vs_evaluation.api import (
    EvaluationCoordinator,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    ExecutorCancellationUnknownError,
    ExecutorObservation,
    ExecutorRejectedError,
    FilesystemEvaluationStore,
    StageState,
)
from vs_evaluation.api.testing import FakeClock
from vs_sandbox.api.slurm import (
    SlurmCommandResult,
    SlurmEvaluationExecutor,
    SlurmStagePayload,
    SlurmTargetLifecycle,
)
from vs_slurm.api import (
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStageResult,
    SlurmBatchWaitResult,
    SlurmConfig,
    SlurmError,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmSshTransport,
    SlurmSubmissionRejectedError,
)

if TYPE_CHECKING:
    from pathlib import Path


class _FakeRunner(SlurmJobRunner):
    def __init__(
        self,
        config: SlurmConfig,
        *,
        fail_before_stage: bool = False,
        benchmark_exit_code: int | None = 0,
        benchmark_stdout: str = '{"throughput": 10}',
        service_log_tail: str = "",
    ) -> None:
        super().__init__(config)
        self.service_log_tail = service_log_tail
        self.job_exit_code = 0
        self.completed_before_allocation_failure = False
        self.accuracy_exit_code: int | None = 0
        self.collection_failure: str | None = None
        self.benchmark_exit_code = benchmark_exit_code
        self.benchmark_stdout = benchmark_stdout
        self.submissions = 0
        self.cancellations = 0
        self.job_status = SlurmJobStatus.UNKNOWN
        self.cancelled_job_ids: list[str] = []
        self.request: SlurmBatchRequest | None = None
        self.handle = SlurmBatchHandle.model_validate(
            {
                "job": {
                    "job_id": "1234",
                    "invocation_id": "evaluation",
                    "config_identity": "0" * 64,
                    "remote_workspace": "/runs/evaluation/workspace",
                    "remote_status_path": "/runs/evaluation/status.txt",
                    "remote_log_path": "/runs/evaluation/job.log",
                },
                "stages": ({"name": "accuracy"}, {"name": "benchmark"}),
                "submission_seconds": 4.0,
            }
        )
        self.fail_before_stage = fail_before_stage

    def submit_batch(self, request: SlurmBatchRequest) -> SlurmBatchHandle:
        self.submissions += 1
        self.job_status = SlurmJobStatus.RUNNING
        self.request = request
        return self.handle

    def wait_batch(
        self,
        handle: SlurmBatchHandle,
        *,
        timeout_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> SlurmBatchWaitResult:
        del timeout_seconds, cancel_event
        self.job_status = SlurmJobStatus.COMPLETED
        return SlurmBatchWaitResult(
            handle=handle,
            status=SlurmJobStatus.FAILED
            if self.fail_before_stage or self.job_exit_code != 0
            else SlurmJobStatus.COMPLETED,
            timed_out=False,
        )

    def collect_batch(self, handle: SlurmBatchHandle) -> SlurmBatchResult:
        del handle
        if self.fail_before_stage or (
            self.job_exit_code != 0 and not self.completed_before_allocation_failure
        ):
            return SlurmBatchResult(
                job_id="1234",
                job_exit_code=70 if self.fail_before_stage else self.job_exit_code,
                job_output="service startup failed"
                if self.fail_before_stage
                else "allocation failed",
                stages=(),
                phase_timings_seconds={"staging": 3.0},
                content_cache_hits=2,
            )
        stopped = (
            self.request is not None
            and self.request.stop_on_failure
            and self.accuracy_exit_code not in (None, 0)
        )
        return SlurmBatchResult(
            job_id="1234",
            job_exit_code=self.job_exit_code,
            job_output="",
            service_log_tail=self.service_log_tail,
            stages=(
                SlurmBatchStageResult(
                    name="accuracy",
                    exit_code=self.accuracy_exit_code,
                    stdout="passed",
                    stderr="",
                    elapsed_seconds=2.0,
                    skipped=False,
                    collection_failure=self.collection_failure,
                ),
                SlurmBatchStageResult(
                    name="benchmark",
                    exit_code=None if stopped else self.benchmark_exit_code,
                    stdout="" if stopped else self.benchmark_stdout,
                    stderr="",
                    elapsed_seconds=None if stopped else 4.0,
                    skipped=stopped,
                ),
            ),
            phase_timings_seconds={"staging": 3.0, "collection": 1.0},
            content_cache_hits=2,
        )

    def poll_batch(self, handle: SlurmBatchHandle) -> SlurmJobStatus:
        """Observe lifecycle independently of submission/cancellation acknowledgements."""
        del handle
        return self.job_status

    def cancel_batch(self, handle: SlurmBatchHandle) -> None:
        self.cancellations += 1
        self.cancelled_job_ids.append(handle.job.job_id)
        self.job_status = SlurmJobStatus.CANCELLED


class _TimedOutRunner(_FakeRunner):
    def __init__(self, config: SlurmConfig, *, already_waited: float) -> None:
        super().__init__(config)
        self.handle = self.handle.model_copy(update={"waited_seconds": already_waited})
        self.wait_timeouts: list[float | None] = []
        self.deadline_clock = _DeadlineClock(1_000)

    def wait_batch(
        self,
        handle: SlurmBatchHandle,
        *,
        timeout_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> SlurmBatchWaitResult:
        del cancel_event
        self.wait_timeouts.append(timeout_seconds)
        assert timeout_seconds is not None
        self.deadline_clock.advance(timeout_seconds)
        return SlurmBatchWaitResult(
            handle=handle.model_copy(
                update={"waited_seconds": handle.waited_seconds + timeout_seconds}
            ),
            status=SlurmJobStatus.RUNNING,
            timed_out=True,
        )


class _BlockingRunner(_FakeRunner):
    def __init__(self, config: SlurmConfig) -> None:
        super().__init__(config)
        self.wait_started = threading.Event()
        self.wait_finished = threading.Event()
        self._release_wait = threading.Event()

    def wait_batch(
        self,
        handle: SlurmBatchHandle,
        *,
        timeout_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> SlurmBatchWaitResult:
        del timeout_seconds, cancel_event
        self.wait_started.set()
        self._release_wait.wait()
        self.wait_finished.set()
        return SlurmBatchWaitResult(
            handle=handle,
            status=SlurmJobStatus.CANCELLED,
            timed_out=False,
        )

    def cancel_batch(self, handle: SlurmBatchHandle) -> None:
        super().cancel_batch(handle)
        self._release_wait.set()


class _UnreachableSchedulerRunner(_BlockingRunner):
    def cancel_batch(self, handle: SlurmBatchHandle) -> None:
        super().cancel_batch(handle)
        message = "scancel unreachable"
        raise SlurmError(message)


class _DeadlineClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _config() -> SlurmConfig:
    return SlurmConfig(
        name="fake-cluster",
        remote_workspace_root="/runs",
        transport=SlurmSshTransport(host="fake-cluster"),
    )


def _request() -> EvaluationRequest:
    return EvaluationRequest(
        key="fused",
        stages=tuple(
            EvaluationStep(
                name=name,
                payload=SlurmStagePayload(
                    command=f"run-{name}",
                    timeout_seconds=timeout,
                ).model_dump(mode="json"),
            )
            for name, timeout in (("accuracy", 17), ("benchmark", 41))
        ),
    )


async def _terminal(executor: SlurmEvaluationExecutor, handle_id: str) -> ExecutorObservation:
    while True:
        observed = await executor.inspect(handle_id)
        if observed is not None and observed.state in {
            EvaluationState.SUCCEEDED,
            EvaluationState.FAILED,
        }:
            return observed
        await executor.wait_for_change(handle_id, timeout_s=float("inf"))


@pytest.mark.asyncio
async def test_executor_fuses_stages_and_preserves_execution_metadata(tmp_path: Path) -> None:
    config = _config()
    runner = _FakeRunner(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )

    await executor.submit(_request(), handle_id="eval-fused")
    observed = await _terminal(executor, "eval-fused")

    assert observed.state is EvaluationState.SUCCEEDED
    assert runner.submissions == 1
    assert runner.request is not None
    assert tuple(stage.timeout_seconds for stage in runner.request.stages) == (17, 41)
    result = SlurmCommandResult.model_validate(observed.stage_results[0].result)
    assert result.execution_metadata is not None
    assert result.execution_metadata.content_cache_hits == 2


@pytest.mark.asyncio
async def test_executor_rejects_malformed_stages_without_submitting(tmp_path: Path) -> None:
    config = _config()
    runner = _FakeRunner(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    mixed = EvaluationRequest(
        key="mixed",
        stages=tuple(
            EvaluationStep(
                name=name,
                payload=SlurmStagePayload(
                    command=f"run-{name}", target_lifecycle=lifecycle
                ).model_dump(mode="json"),
            )
            for name, lifecycle in (
                ("accuracy", SlurmTargetLifecycle.COMMAND_MANAGED),
                ("benchmark", SlurmTargetLifecycle.SHARED_SERVICE),
            )
        ),
    )

    with pytest.raises(ExecutorRejectedError, match="one target lifecycle"):
        await executor.submit(mixed, handle_id="eval-mixed")

    assert await executor.inspect("eval-mixed") is None
    assert runner.submissions == 0
    await executor.close()


@pytest.mark.asyncio
async def test_executor_maps_pre_stage_failure_and_skips_remainder(tmp_path: Path) -> None:
    config = _config()
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=_FakeRunner(config, fail_before_stage=True),
    )

    await executor.submit(_request(), handle_id="eval-failed")
    observed = await _terminal(executor, "eval-failed")

    assert observed.state is EvaluationState.FAILED
    assert observed.stage_results[0].failure == "service startup failed"
    assert observed.stage_results[1].state is StageState.SKIPPED


@pytest.mark.asyncio
async def test_executor_never_invents_a_successful_exit_code(tmp_path: Path) -> None:
    config = _config()
    runner = _FakeRunner(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    try:
        for exit_code in (None, *range(256)):
            runner.benchmark_exit_code = exit_code
            handle_id = f"eval-exit-code-{exit_code}"
            await executor.submit(_request(), handle_id=handle_id)
            observed = await _terminal(executor, handle_id)
            if exit_code is None:
                assert observed.state is EvaluationState.FAILED
                assert observed.failure == (
                    "SlurmOutcomeUnknownError: Slurm stage 'benchmark' has an unknown outcome: "
                    "missing exit code"
                )
                assert observed.stage_results[0].state is StageState.SUCCEEDED
                assert observed.stage_results[1].state is StageState.FAILED
                result = SlurmCommandResult.model_validate(observed.stage_results[1].result)
                assert result.exit_code is None
                assert result.stdout == runner.benchmark_stdout
                assert SlurmCommandResult.model_validate(
                    observed.stage_results[0].result
                ).stdout == ("passed")
            else:
                expected = StageState.SUCCEEDED if exit_code == 0 else StageState.FAILED
                assert observed.stage_results[1].state is expected
                result = SlurmCommandResult.model_validate(observed.stage_results[1].result)
                assert result.exit_code == exit_code
    finally:
        await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_executor_preserves_steps_but_rejects_failed_batch(
    tmp_path: Path, *, completed: bool
) -> None:
    config = _config()
    runner = _FakeRunner(config)
    runner.completed_before_allocation_failure = completed
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    try:
        for exit_code in range(256):
            runner.job_exit_code = exit_code
            handle_id = f"eval-batch-exit-{exit_code}"
            await executor.submit(_request(), handle_id=handle_id)
            observed = await _terminal(executor, handle_id)
            assert len(observed.stage_results) == 2
            if completed or exit_code == 0:
                assert all(stage.state is StageState.SUCCEEDED for stage in observed.stage_results)
            else:
                assert observed.stage_results[0].state is StageState.FAILED
                assert observed.stage_results[1].state is StageState.SKIPPED
            expected = EvaluationState.SUCCEEDED if exit_code == 0 else EvaluationState.FAILED
            assert observed.state is expected
            if exit_code and completed:
                assert observed.failure == (
                    f"_SlurmExecutionError: Slurm batch '1234' exited with code {exit_code}"
                )
    finally:
        await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("accuracy_exit", [None, 0, 7])
@pytest.mark.parametrize("benchmark_exit", [None, 0, 7])
@pytest.mark.parametrize("collection_failure", [None, "missing stage artifact"])
async def test_executor_keeps_both_stage_streams_across_unknown_and_incomplete_outcomes(
    tmp_path: Path,
    accuracy_exit: int | None,
    benchmark_exit: int | None,
    collection_failure: str | None,
) -> None:
    config = _config()
    runner = _FakeRunner(config, benchmark_exit_code=benchmark_exit)
    runner.accuracy_exit_code = accuracy_exit
    runner.collection_failure = collection_failure
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    try:
        await executor.submit(
            _request().model_copy(update={"stop_on_failure": False}), handle_id="eval-evidence"
        )
        observed = await _terminal(executor, "eval-evidence")
        succeeded = accuracy_exit == 0 and benchmark_exit == 0 and collection_failure is None
        assert observed.state is (
            EvaluationState.SUCCEEDED if succeeded else EvaluationState.FAILED
        )
        assert len(observed.stage_results) == 2
        for stage, exit_code, stdout in zip(
            observed.stage_results,
            (accuracy_exit, benchmark_exit),
            ("passed", runner.benchmark_stdout),
            strict=True,
        ):
            raw = SlurmCommandResult.model_validate(stage.result)
            assert raw.exit_code == exit_code
            assert raw.stdout == stdout
            assert raw.collection_failure == (
                collection_failure if stage.name == "accuracy" else None
            )
            if stage.name == "accuracy":
                assert raw.execution_metadata is not None
                assert raw.execution_metadata.job_exit_code == 0
            if exit_code != 0 or (stage.name == "accuracy" and collection_failure):
                assert stage.state is StageState.FAILED
    finally:
        await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("accuracy_exit", [1, 7, 137, 255])
async def test_failed_accuracy_skips_benchmark_when_requested(
    tmp_path: Path, accuracy_exit: int
) -> None:
    config = _config()
    runner = _FakeRunner(config)
    runner.accuracy_exit_code = accuracy_exit
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    try:
        await executor.submit(_request(), handle_id="eval-accuracy-gate")
        observed = await _terminal(executor, "eval-accuracy-gate")
        assert observed.state is EvaluationState.FAILED
        assert observed.stage_results[0].state is StageState.FAILED
        assert observed.stage_results[1].state is StageState.SKIPPED
    finally:
        await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("accuracy_exit", "collection_failure", "allocation_exit"),
    [(None, None, 0), (0, "missing accuracy artifact", 0), (0, None, 70)],
)
async def test_executor_preserves_both_stages_through_durable_coordinator_failure(
    tmp_path: Path, accuracy_exit: int | None, collection_failure: str | None, allocation_exit: int
) -> None:
    config = _config()
    runner = _FakeRunner(config)
    runner.accuracy_exit_code = accuracy_exit
    runner.collection_failure = collection_failure
    runner.job_exit_code = allocation_exit
    runner.completed_before_allocation_failure = True
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    coordinator = EvaluationCoordinator(
        executor, FilesystemEvaluationStore(tmp_path / "coordinator"), FakeClock()
    )
    try:
        handle = await coordinator.submit(_request())
        await _terminal(executor, handle.id)
        observed = await coordinator.snapshot(handle.id)
        assert observed.status is EvaluationState.FAILED
        assert len(observed.stage_results) == 2
        assert (
            SlurmCommandResult.model_validate(observed.stage_results[0].result).stdout == "passed"
        )
        assert (
            SlurmCommandResult.model_validate(observed.stage_results[1].result).stdout
            == runner.benchmark_stdout
        )
        assert (await coordinator.history())[0].stage_results == observed.stage_results
    finally:
        await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [
        (124, "stage 'benchmark' hit its time limit after 4 s and printed no output"),
        (137, "stage 'benchmark' hit its time limit after 4 s and printed no output"),
        (3, "stage 'benchmark' exited with code 3 after 4 s and printed no output"),
    ],
)
async def test_executor_reports_a_silent_failed_stage(
    tmp_path: Path, exit_code: int, expected: str
) -> None:
    config = _config()
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=_FakeRunner(config, benchmark_exit_code=exit_code, benchmark_stdout=""),
    )

    await executor.submit(_request(), handle_id="eval-silent")
    observed = await _terminal(executor, "eval-silent")

    assert observed.state is EvaluationState.FAILED
    assert observed.stage_results[0].state is StageState.SUCCEEDED
    assert observed.stage_results[1].state is StageState.FAILED
    assert observed.stage_results[1].failure == expected
    assert observed.failure == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("benchmark_stdout", "expected_head"),
    [
        ("", "stage 'benchmark' hit its time limit after 4 s and printed no output"),
        ("warmup timed out", "warmup timed out"),
    ],
)
async def test_failed_stage_reports_the_server_log_tail(
    tmp_path: Path, benchmark_stdout: str, expected_head: str
) -> None:
    config = _config()
    server_log = "decode: 2600 steps, 112.2 ms/step, 22.7 items/step"
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=_FakeRunner(
            config,
            benchmark_exit_code=124,
            benchmark_stdout=benchmark_stdout,
            service_log_tail=server_log,
        ),
    )

    await executor.submit(_request(), handle_id="eval-server-log")
    observed = await _terminal(executor, "eval-server-log")

    failure = observed.stage_results[1].failure
    assert failure is not None
    assert failure.startswith(expected_head)
    assert failure.endswith(server_log)
    assert observed.stage_results[0].failure is None


@pytest.mark.asyncio
async def test_executor_recovers_durable_handle_without_resubmission(tmp_path: Path) -> None:
    config = _config()
    runner = _FakeRunner(config)
    handle_root = tmp_path / "handles"
    first = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=handle_root,
        runner=runner,
    )
    await first.submit(_request(), handle_id="eval-resume")
    assert (await _terminal(first, "eval-resume")).state is EvaluationState.SUCCEEDED

    resumed = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=handle_root,
        runner=runner,
    )
    recovered = await resumed.inspect("eval-resume")
    assert recovered is not None
    assert (await _terminal(resumed, "eval-resume")).state is EvaluationState.SUCCEEDED
    assert runner.submissions == 1


@pytest.mark.asyncio
async def test_executor_enforces_one_persisted_wait_deadline(tmp_path: Path) -> None:
    config = _config().model_copy(update={"job_timeout_seconds": 10})
    runner = _TimedOutRunner(config, already_waited=4.0)
    handle_root = tmp_path / "handles"
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=handle_root,
        runner=runner,
        deadline_clock=runner.deadline_clock,
    )

    await executor.submit(_request(), handle_id="eval-timeout")
    observed = await _terminal(executor, "eval-timeout")

    assert observed.state is EvaluationState.FAILED
    assert (
        observed.failure == "_SlurmExecutionError: Slurm evaluation exceeded its 10-second deadline"
    )
    assert runner.wait_timeouts == [6.0]
    assert runner.cancellations == 1

    resumed = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=handle_root,
        runner=runner,
        deadline_clock=runner.deadline_clock,
    )
    recovered = await resumed.inspect("eval-timeout")
    assert recovered is not None
    assert (await _terminal(resumed, "eval-timeout")).state is EvaluationState.FAILED
    assert runner.wait_timeouts == [6.0]
    assert runner.cancellations == 2


@pytest.mark.asyncio
async def test_executor_recovers_deadline_persisted_before_interrupted_wait(
    tmp_path: Path,
) -> None:
    config = _config().model_copy(update={"job_timeout_seconds": 10})
    runner = _BlockingRunner(config)
    clock = _DeadlineClock(1_000)
    handle_root = tmp_path / "handles"
    first = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=handle_root,
        runner=runner,
        deadline_clock=clock,
    )
    await first.submit(_request(), handle_id="eval-crash-during-wait")
    await asyncio.to_thread(runner.wait_started.wait)
    await first.close()

    clock.advance(11)
    resumed = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=handle_root,
        runner=runner,
        deadline_clock=clock,
    )

    observed = await _terminal(resumed, "eval-crash-during-wait")

    assert observed.state is EvaluationState.FAILED
    assert runner.cancellations == 2


@pytest.mark.asyncio
async def test_executor_close_cancels_and_drains_background_execution(tmp_path: Path) -> None:
    config = _config()
    runner = _BlockingRunner(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    await executor.submit(_request(), handle_id="eval-close")
    await asyncio.to_thread(runner.wait_started.wait)

    await executor.close()

    assert runner.cancellations == 1
    assert runner.wait_finished.is_set()
    observed = await executor.inspect("eval-close")
    assert observed is not None
    assert observed.state is EvaluationState.CANCELED


def test_command_managed_stage_disables_shared_service() -> None:
    payload = SlurmStagePayload(target_lifecycle=SlurmTargetLifecycle.COMMAND_MANAGED)

    assert payload.target_lifecycle is SlurmTargetLifecycle.COMMAND_MANAGED


@pytest.mark.asyncio
async def test_cancelling_the_execution_task_cancels_the_submitted_slurm_job(
    tmp_path: Path,
) -> None:
    config = _config()
    runner = _BlockingRunner(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    await executor.submit(_request(), handle_id="eval-interrupted")
    await asyncio.to_thread(runner.wait_started.wait)

    # What asyncio.run does to leftover tasks on a hard interrupt: cancel them
    # directly, without going through the executor's own cancel().
    executions = [
        task for task in asyncio.all_tasks() if task.get_name().startswith("vibesys-slurm-")
    ]
    assert len(executions) == 1
    executions[0].cancel()
    await asyncio.gather(*executions, return_exceptions=True)

    assert runner.cancelled_job_ids == ["1234"]
    observed = await executor.inspect("eval-interrupted")
    assert observed is not None
    assert observed.state is EvaluationState.CANCELED


@pytest.mark.asyncio
async def test_a_failed_scancel_does_not_stop_the_cancellation_from_finishing(
    tmp_path: Path,
) -> None:
    config = _config()
    runner = _UnreachableSchedulerRunner(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    await executor.submit(_request(), handle_id="eval-unreachable")
    await asyncio.to_thread(runner.wait_started.wait)
    executions = [
        task for task in asyncio.all_tasks() if task.get_name().startswith("vibesys-slurm-")
    ]

    executions[0].cancel()
    outcomes = await asyncio.gather(*executions, return_exceptions=True)

    assert [type(outcome) for outcome in outcomes] == [asyncio.CancelledError]
    # A failed scancel may be retried by the next cleanup step, never skipped.
    assert set(runner.cancelled_job_ids) == {"1234"}


class _PendingCancellationRunner(_BlockingRunner):
    """Scheduler Fake that accepts scancel while the allocation remains running."""

    terminate: bool = False

    def cancel_batch(self, handle: SlurmBatchHandle) -> None:
        """Separate accepted cancellation from the deliberate terminal observation."""
        if self.terminate:
            super().cancel_batch(handle)
        else:
            self.cancellations += 1
            self.cancelled_job_ids.append(handle.job.job_id)


@pytest.mark.asyncio
async def test_scancel_acknowledgement_does_not_complete_release_or_suppress_retry(
    tmp_path: Path,
) -> None:
    """Scope cleanup must observe terminal scheduler state, not just a sent request."""
    config = _config()
    runner = _PendingCancellationRunner(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    await executor.submit(_request(), handle_id="eval-pending-cancel")
    await asyncio.to_thread(runner.wait_started.wait)
    with pytest.raises(SlurmError, match="not terminal"):
        await executor.cancel("eval-pending-cancel")
    observed = await executor.inspect("eval-pending-cancel")
    assert observed is not None
    assert observed.state is EvaluationState.RUNNING
    assert runner.cancellations == 1
    runner.terminate = True
    await executor.cancel("eval-pending-cancel")
    observed = await executor.inspect("eval-pending-cancel")
    assert observed is not None
    assert observed.state is EvaluationState.CANCELED
    assert runner.cancellations == 2
    assert runner.wait_finished.is_set()


class _LostSubmissionAcknowledgementRunner(_FakeRunner):
    """Remote scheduler accepts a job, then loses the returned external identity."""

    def __init__(self, config: SlurmConfig) -> None:
        super().__init__(config)
        self.accepted = threading.Event()

    def submit_batch(self, request: SlurmBatchRequest) -> SlurmBatchHandle:
        """Keep the accepted job in remote state while failing the submit reply."""
        super().submit_batch(request)
        self.accepted.set()
        raise _LostSubmitReplyError


class _LostSubmitReplyError(OSError):
    """Injected remote acceptance followed by transport loss."""


@pytest.mark.asyncio
async def test_missing_external_identity_keeps_dispatched_cancellation_unresolved(
    tmp_path: Path,
) -> None:
    """A lost accepted Slurm handle cannot manufacture CANCELED or completed cleanup."""
    config = _config()
    runner = _LostSubmissionAcknowledgementRunner(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    store = evaluation_testing.InMemoryEvaluationStore()
    coordinator = EvaluationCoordinator(executor, store, FakeClock())
    handle = await coordinator.submit(_request())
    await asyncio.to_thread(runner.accepted.wait)
    with pytest.raises(ExecutorCancellationUnknownError, match="unknown external identity"):
        await handle.cancel()
    record = await store.get(handle.id)
    assert record is not None
    assert record.cancel_requested
    assert record.dispatch_authorized is True
    assert record.state in {
        EvaluationState.QUEUED,
        EvaluationState.STARTING,
        EvaluationState.RUNNING,
    }
    assert runner.job_status is SlurmJobStatus.RUNNING
    resumed = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    restored = EvaluationCoordinator(resumed, store, FakeClock())
    with pytest.raises(ExecutorCancellationUnknownError, match="unknown external identity"):
        await restored.cancel(handle.id)
    remaining = await store.get(handle.id)
    assert remaining is not None
    assert remaining.state is not EvaluationState.CANCELED
    assert runner.submissions == 1


class _RejectedBeforeSubmissionRunner(_FakeRunner):
    """Fail staging with definitive no-scheduler-resource evidence."""

    def submit_batch(self, request: SlurmBatchRequest) -> SlurmBatchHandle:
        del request
        raise SlurmSubmissionRejectedError.transport_failed("exec", 255)


@pytest.mark.asyncio
async def test_known_staging_rejection_is_failed_and_cleanup_needs_no_external_identity(
    tmp_path: Path,
) -> None:
    """A known pre-submit fault remains ordinary planner failure, not unknown cleanup."""
    config = _config()
    runner = _RejectedBeforeSubmissionRunner(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    coordinator = EvaluationCoordinator(
        executor, evaluation_testing.InMemoryEvaluationStore(), FakeClock()
    )
    handle = await coordinator.submit(_request())
    observed = await _terminal(executor, handle.id)
    assert observed.state is EvaluationState.FAILED
    assert runner.submissions == 0
    await coordinator.cancel(handle.id)
    await coordinator.cancel(handle.id)
    assert await coordinator.status(handle.id) is EvaluationState.FAILED
    assert runner.cancellations == 0
    await executor.close()


class _GatedStagingRejectionRunner(_RejectedBeforeSubmissionRunner):
    def __init__(self, config: SlurmConfig) -> None:
        super().__init__(config)
        self.staging_started = threading.Event()
        self.release_staging = threading.Event()

    def submit_batch(self, request: SlurmBatchRequest) -> SlurmBatchHandle:
        self.staging_started.set()
        self.release_staging.wait()
        return super().submit_batch(request)


@pytest.mark.asyncio
async def test_cancellation_during_rejected_staging_preserves_definite_failure(
    tmp_path: Path,
) -> None:
    config = _config()
    runner = _GatedStagingRejectionRunner(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    await executor.submit(_request(), handle_id="staging-cancel")
    await asyncio.to_thread(runner.staging_started.wait)
    # cancel() dispatches cancellation before yielding to drain acceptance;
    # release the synchronous staging operation at that yield, without time.
    asyncio.get_running_loop().call_soon(runner.release_staging.set)
    await executor.cancel("staging-cancel")
    observed = await executor.inspect("staging-cancel")
    assert observed is not None
    assert observed.state is EvaluationState.FAILED
    assert runner.cancellations == 0
    await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", tuple(SlurmJobStatus))
async def test_read_only_restart_inspection_never_resumes_or_cancels_work(
    tmp_path: Path, status: SlurmJobStatus
) -> None:
    config = _config()
    runner = _FakeRunner(config)
    options = {
        "workspace": tmp_path,
        "setup_script": None,
        "service": None,
        "support_trees": {},
        "handle_root": tmp_path / "handles",
        "runner": runner,
    }
    first = SlurmEvaluationExecutor(config, **options)
    await first.submit(_request(), handle_id="inspect-only")
    assert (await _terminal(first, "inspect-only")).state is EvaluationState.SUCCEEDED
    runner.job_status = status
    resumed = SlurmEvaluationExecutor(config, **options)
    result = await resumed.inspect_only("inspect-only")
    if status in {SlurmJobStatus.FAILED, SlurmJobStatus.COMPLETED, SlurmJobStatus.UNKNOWN}:
        assert result is None
    else:
        assert result is not None
    await resumed.close()
    assert runner.submissions == 1
    assert runner.cancellations == 0
    assert runner.job_status is status


@pytest.mark.asyncio
async def test_read_only_pending_scheduler_does_not_regress_active_evaluation(
    tmp_path: Path,
) -> None:
    config = _config()
    runner = _BlockingRunner(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=tmp_path,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        runner=runner,
    )
    coordinator = EvaluationCoordinator(
        executor, FilesystemEvaluationStore(tmp_path / "records"), FakeClock()
    )
    handle = await coordinator.submit(_request())
    await asyncio.to_thread(runner.wait_started.wait)
    active = await coordinator.snapshot(handle.id)
    assert active.state is EvaluationState.RUNNING
    runner.job_status = SlurmJobStatus.PENDING
    inspected = await coordinator.inspect_snapshot(handle.id)
    assert inspected is not None
    assert inspected.state is EvaluationState.RUNNING
    assert inspected.current_stage is None
    assert runner.submissions == 1
    assert runner.cancellations == 0
    await executor.close()
