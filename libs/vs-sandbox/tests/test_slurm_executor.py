from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api import (
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    ExecutorObservation,
    StageState,
)
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
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmSshTransport,
)

if TYPE_CHECKING:
    from pathlib import Path


class _FakeRunner(SlurmJobRunner):
    def __init__(
        self,
        config: SlurmConfig,
        *,
        fail_before_stage: bool = False,
        benchmark_exit_code: int = 0,
        benchmark_stdout: str = '{"throughput": 10}',
        service_log_tail: str = "",
    ) -> None:
        super().__init__(config)
        self.service_log_tail = service_log_tail
        self.benchmark_exit_code = benchmark_exit_code
        self.benchmark_stdout = benchmark_stdout
        self.submissions = 0
        self.cancellations = 0
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
        return SlurmBatchWaitResult(
            handle=handle,
            status=SlurmJobStatus.COMPLETED,
            timed_out=False,
        )

    def collect_batch(self, handle: SlurmBatchHandle) -> SlurmBatchResult:
        del handle
        if self.fail_before_stage:
            return SlurmBatchResult(
                job_id="1234",
                job_exit_code=70,
                job_output="service startup failed",
                stages=(),
                phase_timings_seconds={"staging": 3.0},
                content_cache_hits=2,
            )
        return SlurmBatchResult(
            job_id="1234",
            job_exit_code=0,
            job_output="",
            service_log_tail=self.service_log_tail,
            stages=(
                SlurmBatchStageResult(
                    name="accuracy",
                    exit_code=0,
                    stdout="passed",
                    stderr="",
                    elapsed_seconds=2.0,
                    skipped=False,
                ),
                SlurmBatchStageResult(
                    name="benchmark",
                    exit_code=self.benchmark_exit_code,
                    stdout=self.benchmark_stdout,
                    stderr="",
                    elapsed_seconds=4.0,
                    skipped=False,
                ),
            ),
            phase_timings_seconds={"staging": 3.0, "collection": 1.0},
            content_cache_hits=2,
        )

    def cancel_batch(self, handle: SlurmBatchHandle) -> None:
        del handle
        self.cancellations += 1


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
        await asyncio.sleep(0)


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
