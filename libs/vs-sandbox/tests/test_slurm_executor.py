from __future__ import annotations

import asyncio
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_evaluation.api.testing as evaluation_testing
from vs_evaluation.api import (
    EvaluationCoordinator,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    ExecutorCancellationUnconfirmedError,
    ExecutorCancellationUnknownError,
    ExecutorObservation,
    ExecutorRejectedError,
    FilesystemEvaluationStore,
    PollPhase,
    StageState,
)
from vs_evaluation.api.testing import FakeClock
from vs_sandbox.api.slurm import (
    SharedSlurmAdmission,
    SlurmCommandResult,
    SlurmEvaluationExecutor,
    SlurmStagePayload,
    SlurmTargetLifecycle,
)
from vs_slurm.api import (
    ClusterCancelOutcome,
    ClusterCancelRequested,
    ClusterCollectOutcome,
    ClusterInspectOutcome,
    ClusterObservation,
    ClusterRejected,
    ClusterSubmitOutcome,
    ClusterSubmitted,
    ClusterTarget,
    ClusterUnknown,
    FakeCluster,
    FakeConnector,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStage,
    SlurmCluster,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmError,
    SlurmJobHandle,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmSshTransport,
    validate_cluster_operation_id,
)

_RAW_STAGE_INPUT = """import json, sys
from pathlib import Path
files, trees, output, code = json.loads(sys.argv[1])
for path in files:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{}')
for path in trees:
    Path(path).mkdir(parents=True, exist_ok=True)
sys.stdout.write(output)
raise SystemExit(code)
"""


class _ScenarioCluster(FakeCluster):
    """Production batch fixtures with controlled scheduler observations and counters."""

    def __init__(
        self,
        config: SlurmConfig,
        *,
        fail_before_stage: bool = False,
        benchmark_exit_code: int | None = 0,
        benchmark_stdout: str = '{"throughput": 10}',
        service_log_tail: str = "",
    ) -> None:
        super().__init__()
        self.config = config
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
        self.handle: SlurmBatchHandle | None = None
        self._initial_waited_seconds = 0.0
        self._producer_handles: dict[str, SlurmBatchHandle] = {}
        self.fail_before_stage = fail_before_stage
        self._configured: set[str] = set()
        self._lost_reply = False
        self._rejection: str | None = None
        self._scheduler_states: tuple[SlurmJobStatus, ...] | None = None

    def submit(
        self, request: SlurmBatchRequest | SlurmJobRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        try:
            validate_cluster_operation_id(operation_id)
        except SlurmError:
            return super().submit(request, operation_id=operation_id)
        if isinstance(request, SlurmJobRequest):
            return super().submit(request, operation_id=operation_id)
        initial_waited = self._initial_waited_seconds
        if operation_id not in self._configured:
            self.request = request
            handle, baseline = self._produce(request, operation_id)
            self._producer_handles[operation_id] = handle
            states = self._scheduler_states or (
                SlurmJobStatus.FAILED
                if self.fail_before_stage or self.job_exit_code
                else SlurmJobStatus.COMPLETED,
            )
            self.script(
                operation_id,
                states=states,
                result=self._result(baseline),
                on_accept=self._accepted,
                on_dispatch=self._dispatch,
                lost_submit_reply=self._lost_reply,
            )
            if self._rejection is not None:
                self.script(
                    operation_id,
                    states=states,
                    result=self._result(baseline),
                    on_dispatch=self._dispatch,
                    rejected_reason=self._rejection,
                )
            self._configured.add(operation_id)
        submitted = super().submit(request, operation_id=operation_id)
        if isinstance(submitted, ClusterSubmitted):
            assert isinstance(submitted.handle, SlurmBatchHandle)
            self.handle = self._producer_handles[operation_id].model_copy(
                update={"submission_seconds": 4.0, "waited_seconds": initial_waited}
            )
            self._producer_handles[operation_id] = self.handle
            return submitted.model_copy(update={"handle": self.handle})
        return submitted

    def _accepted(self) -> None:
        self.submissions += 1
        self.job_status = SlurmJobStatus.RUNNING

    def _dispatch(self) -> None:
        pass

    def _produce(
        self, request: SlurmBatchRequest, operation_id: str
    ) -> tuple[SlurmBatchHandle, SlurmBatchResult]:
        root = request.workspace.parent / "scenario-producers" / operation_id
        config = self.config.model_copy(
            update={
                "remote_workspace_root": str(root / "remote"),
                "transport": SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
            }
        )
        runner = SlurmJobRunner(config, process=FakeConnector(root / "scheduler"))

        # A missing exit code is injected into the collected raw record below;
        # the executing transport itself must receive a concrete process exit.
        def process_exit(stage_name: str) -> int:
            raw_exit = (
                self.accuracy_exit_code if stage_name == "accuracy" else self.benchmark_exit_code
            )
            return 0 if raw_exit is None else raw_exit

        stages = tuple(
            replace(
                stage,
                command=(
                    sys.executable,
                    "-c",
                    _RAW_STAGE_INPUT,
                    json.dumps(
                        [
                            [artifact.remote_path for artifact in stage.file_artifacts],
                            [artifact.remote_path for artifact in stage.tree_artifacts],
                            "passed" if stage.name == "accuracy" else self.benchmark_stdout,
                            process_exit(stage.name),
                        ]
                    ),
                ),
            )
            for stage in request.stages
        )
        before_stage = self.fail_before_stage or (
            self.job_exit_code != 0 and not self.completed_before_allocation_failure
        )
        setup_script = root / "setup.sh"
        if before_stage:
            setup_script.parent.mkdir(parents=True, exist_ok=True)
            setup_script.write_text("#!/bin/bash\nexit 70\n")
        execution = replace(
            request,
            stages=stages,
            setup_script=str(setup_script) if before_stage else None,
            service=None,
        )
        handle = runner.submit_batch(execution, operation_id=operation_id)
        return handle, runner.collect_batch(handle)

    def _result(self, baseline: SlurmBatchResult) -> SlurmBatchResult:
        # Raw faults and deterministic diagnostic durations are injected below
        # evaluation translation. Identities, artifacts and the envelope come
        # from the production runner over an executing Fake Slurm transport.
        before_stage = self.fail_before_stage or (
            self.job_exit_code != 0 and not self.completed_before_allocation_failure
        )
        stages = tuple(
            replace(
                stage,
                exit_code=self.accuracy_exit_code
                if stage.name == "accuracy"
                else (None if stage.skipped else self.benchmark_exit_code),
                elapsed_seconds=None
                if stage.skipped
                else (2.0 if stage.name == "accuracy" else 4.0),
                collection_failure=self.collection_failure
                if stage.name == "accuracy"
                else stage.collection_failure,
            )
            for stage in baseline.stages
        )
        return replace(
            baseline,
            job_exit_code=70 if self.fail_before_stage else self.job_exit_code,
            job_output="service startup failed"
            if self.fail_before_stage
            else ("allocation failed" if before_stage else ""),
            stages=stages,
            service_log_tail=self.service_log_tail,
            phase_timings_seconds={"staging": 3.0, "collection": 1.0},
            content_cache_hits=2,
        )

    def _shadow_target(
        self, target: ClusterTarget, *, by_job_id: bool
    ) -> tuple[ClusterTarget, bool]:
        if isinstance(target, SlurmBatchHandle | SlurmJobHandle):
            job = target.job if isinstance(target, SlurmBatchHandle) else target
            known = self._producer_handles.get(job.invocation_id)
            if known is not None and job == known.job:
                return job.invocation_id, False
        elif by_job_id:
            for operation_id, handle in self._producer_handles.items():
                if handle.job.job_id == target:
                    return operation_id, False
        return target, by_job_id

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        translated, by_job_id = self._shadow_target(target, by_job_id=by_job_id)
        observed = super().inspect(translated, by_job_id=by_job_id)
        if observed.operation_id in self._producer_handles:
            handle = self._producer_handles[observed.operation_id]
            if isinstance(observed, ClusterObservation):
                return observed.model_copy(update={"job_id": handle.job.job_id, "handle": handle})
            return observed.model_copy(update={"job_id": handle.job.job_id})
        return observed

    def collect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCollectOutcome:
        translated, by_job_id = self._shadow_target(target, by_job_id=by_job_id)
        collected = super().collect(translated, by_job_id=by_job_id)
        if collected.operation_id not in self._producer_handles:
            return collected
        handle = self._producer_handles[collected.operation_id]
        updates = {}
        if isinstance(collected, ClusterUnknown):
            updates["job_id"] = handle.job.job_id
        if collected.result is not None:
            updates["result"] = replace(collected.result, job_id=handle.job.job_id)
        return collected.model_copy(update=updates)

    def cancel(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCancelOutcome:
        translated, by_job_id = self._shadow_target(target, by_job_id=by_job_id)
        observed = _ScenarioCluster.inspect(self, translated, by_job_id=by_job_id)
        if isinstance(observed, ClusterObservation):
            self.cancellations += 1
            self.cancelled_job_ids.append(observed.job_id)
            self.job_status = SlurmJobStatus.CANCELLED
        cancelled = super().cancel(translated, by_job_id=by_job_id)
        if cancelled.operation_id in self._producer_handles:
            handle = self._producer_handles[cancelled.operation_id]
            cancelled = cancelled.model_copy(update={"job_id": handle.job.job_id})
        return cancelled


class _TimedOutCluster(_ScenarioCluster):
    def __init__(self, config: SlurmConfig, *, already_waited: float) -> None:
        super().__init__(config)
        self._initial_waited_seconds = already_waited
        self.wait_timeouts: list[float] = []
        self.deadline_clock = _DeadlineClock(1_000)
        self._scheduler_states = (SlurmJobStatus.RUNNING,)

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        observed = super().inspect(target, by_job_id=by_job_id)
        if (
            isinstance(observed, ClusterObservation)
            and observed.status is SlurmJobStatus.RUNNING
            and not self.wait_timeouts
        ):
            assert self.handle is not None
            remaining = self.config.job_timeout_seconds - self.handle.waited_seconds
            self.wait_timeouts.append(remaining)
            self.deadline_clock.advance(remaining)
            self.handle = self.handle.model_copy(
                update={"waited_seconds": self.handle.waited_seconds + remaining}
            )
        return observed


class _BlockingCluster(_ScenarioCluster):
    def __init__(self, config: SlurmConfig) -> None:
        super().__init__(config)
        self.wait_started = threading.Event()
        self.wait_finished = threading.Event()
        self._release_wait = threading.Event()
        self._scheduler_states = (SlurmJobStatus.RUNNING,)

    def release(self) -> None:
        """Release the inspection barrier during test cleanup."""
        self._release_wait.set()

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        observed = super().inspect(target, by_job_id=by_job_id)
        if (
            isinstance(observed, ClusterObservation)
            and observed.status is SlurmJobStatus.RUNNING
            and not self.wait_started.is_set()
        ):
            self.wait_started.set()
            self._release_wait.wait()
            self.wait_finished.set()
            return super().inspect(target, by_job_id=by_job_id)
        return observed

    def cancel(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCancelOutcome:
        cancelled = super().cancel(target, by_job_id=by_job_id)
        self._release_wait.set()
        return cancelled


class _GatedScancelCluster(_BlockingCluster):
    """A scheduler whose scancel is slow: it ends only when the test opens its gate."""

    def __init__(self, config: SlurmConfig) -> None:
        super().__init__(config)
        self.scancel_entered = threading.Event()
        self.scancel_gate = threading.Event()
        self.scancel_done = threading.Event()

    def cancel(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCancelOutcome:
        self.scancel_entered.set()
        self.scancel_gate.wait()
        cancelled = super().cancel(target, by_job_id=by_job_id)
        self.scancel_done.set()
        return cancelled


class _UnreachableSchedulerCluster(_BlockingCluster):
    def cancel(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCancelOutcome:
        cancelled = super().cancel(target, by_job_id=by_job_id)
        return ClusterUnknown(operation_id=cancelled.operation_id, reason="scancel unreachable")


class _DeadlineClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _workspace(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    return workspace


def _config() -> SlurmConfig:
    return SlurmConfig(
        name="fake-cluster",
        remote_workspace_root="/runs",
        transport=SlurmSshTransport(host="fake-cluster"),
        # The Fake scheduler reports teardown for a few inspections; pace the
        # wait loop tightly so those inspections do not cost real seconds.
        poll_interval_seconds=0.001,
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
    runner = _ScenarioCluster(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
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
    runner = _ScenarioCluster(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
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
async def test_executor_maps_missing_stage_evidence_to_failed_collection(tmp_path: Path) -> None:
    config = _config()
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=_ScenarioCluster(config, fail_before_stage=True),
    )

    await executor.submit(_request(), handle_id="eval-failed")
    observed = await _terminal(executor, "eval-failed")

    assert observed.state is EvaluationState.FAILED
    assert observed.stage_results[0].failure == "service startup failed"
    assert observed.stage_results[1].state is StageState.FAILED
    for stage in observed.stage_results:
        result = SlurmCommandResult.model_validate(stage.result)
        assert result.exit_code is None
        assert result.executed is False


@pytest.mark.asyncio
@settings(max_examples=12)
@example(exit_code=None)
@example(exit_code=0)
@example(exit_code=255)
@given(exit_code=st.one_of(st.none(), st.integers(min_value=0, max_value=255)))
async def test_executor_never_invents_a_successful_exit_code(exit_code: int | None) -> None:
    with TemporaryDirectory(prefix="stage-exit-contract-") as directory:
        tmp_path = Path(directory)
        config = _config()
        runner = _ScenarioCluster(config)
        executor = SlurmEvaluationExecutor(
            config,
            workspace=_workspace(tmp_path),
            setup_script=None,
            service=None,
            support_trees={},
            handle_root=tmp_path / "handles",
            cluster=runner,
        )
        try:
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
@settings(max_examples=12)
@example(completed=False, exit_code=0)
@example(completed=False, exit_code=255)
@example(completed=True, exit_code=255)
@given(completed=st.booleans(), exit_code=st.integers(min_value=0, max_value=255))
async def test_executor_preserves_steps_but_rejects_failed_batch(
    *, completed: bool, exit_code: int
) -> None:
    with TemporaryDirectory(prefix="batch-exit-contract-") as directory:
        tmp_path = Path(directory)
        config = _config()
        runner = _ScenarioCluster(config)
        runner.completed_before_allocation_failure = completed
        executor = SlurmEvaluationExecutor(
            config,
            workspace=_workspace(tmp_path),
            setup_script=None,
            service=None,
            support_trees={},
            handle_root=tmp_path / "handles",
            cluster=runner,
        )
        try:
            runner.job_exit_code = exit_code
            handle_id = f"eval-batch-exit-{exit_code}"
            await executor.submit(_request(), handle_id=handle_id)
            observed = await _terminal(executor, handle_id)
            assert len(observed.stage_results) == 2
            if completed or exit_code == 0:
                assert all(stage.state is StageState.SUCCEEDED for stage in observed.stage_results)
            else:
                assert observed.stage_results[0].state is StageState.FAILED
                assert observed.stage_results[1].state is StageState.FAILED
            expected = EvaluationState.SUCCEEDED if exit_code == 0 else EvaluationState.FAILED
            assert observed.state is expected
            if exit_code and completed:
                assert runner.handle is not None
                assert observed.failure == (
                    f"_SlurmExecutionError: Slurm batch '{runner.handle.job.job_id}' exited with code {exit_code}"
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
    runner = _ScenarioCluster(config, benchmark_exit_code=benchmark_exit)
    runner.accuracy_exit_code = accuracy_exit
    runner.collection_failure = collection_failure
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
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
    runner = _ScenarioCluster(config)
    runner.accuracy_exit_code = accuracy_exit
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
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
    runner = _ScenarioCluster(config)
    runner.accuracy_exit_code = accuracy_exit
    runner.collection_failure = collection_failure
    runner.job_exit_code = allocation_exit
    runner.completed_before_allocation_failure = True
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
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
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=_ScenarioCluster(config, benchmark_exit_code=exit_code, benchmark_stdout=""),
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
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=_ScenarioCluster(
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
    runner = _ScenarioCluster(config)
    handle_root = tmp_path / "handles"
    first = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=handle_root,
        cluster=runner,
    )
    await first.submit(_request(), handle_id="eval-resume")
    assert (await _terminal(first, "eval-resume")).state is EvaluationState.SUCCEEDED

    resumed = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=handle_root,
        cluster=runner,
    )
    recovered = await resumed.inspect("eval-resume")
    assert recovered is not None
    assert (await _terminal(resumed, "eval-resume")).state is EvaluationState.SUCCEEDED
    assert runner.submissions == 1


@pytest.mark.asyncio
async def test_recovered_evaluation_never_reports_starting_after_running(tmp_path: Path) -> None:
    config = _config()
    runner = _ScenarioCluster(config)
    handle_root = tmp_path / "handles"

    def executor() -> SlurmEvaluationExecutor:
        return SlurmEvaluationExecutor(
            config,
            workspace=_workspace(tmp_path),
            setup_script=None,
            service=None,
            support_trees={},
            handle_root=handle_root,
            cluster=runner,
        )

    first = executor()
    await first.submit(_request(), handle_id="eval-monotonic")
    assert (await _terminal(first, "eval-monotonic")).state is EvaluationState.SUCCEEDED

    resumed = executor()
    recovered = await resumed.inspect("eval-monotonic")
    assert recovered is not None
    assert recovered.state is EvaluationState.RUNNING
    # Let the recovery task take its admission lease before the next poll.
    await asyncio.sleep(0)
    polled = await resumed.inspect("eval-monotonic")
    assert polled is not None
    assert polled.state is not EvaluationState.STARTING
    assert (await _terminal(resumed, "eval-monotonic")).state is EvaluationState.SUCCEEDED


@pytest.mark.asyncio
async def test_executor_enforces_one_persisted_wait_deadline(tmp_path: Path) -> None:
    config = _config().model_copy(update={"job_timeout_seconds": 10})
    runner = _TimedOutCluster(config, already_waited=4.0)
    handle_root = tmp_path / "handles"
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=handle_root,
        cluster=runner,
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
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=handle_root,
        cluster=runner,
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
    runner = _BlockingCluster(config)
    clock = _DeadlineClock(1_000)
    handle_root = tmp_path / "handles"
    first = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=handle_root,
        cluster=runner,
        deadline_clock=clock,
    )
    try:
        await first.submit(_request(), handle_id="eval-crash-during-wait")
        await asyncio.to_thread(runner.wait_started.wait)
        await first.close()

        clock.advance(11)
        resumed = SlurmEvaluationExecutor(
            config,
            workspace=_workspace(tmp_path),
            setup_script=None,
            service=None,
            support_trees={},
            handle_root=handle_root,
            cluster=runner,
            deadline_clock=clock,
        )

        observed = await _terminal(resumed, "eval-crash-during-wait")

        assert observed.state is EvaluationState.FAILED
        assert runner.cancellations == 2
    finally:
        runner.release()


@pytest.mark.asyncio
async def test_executor_close_cancels_and_drains_background_execution(tmp_path: Path) -> None:
    config = _config()
    runner = _BlockingCluster(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
    )
    try:
        await executor.submit(_request(), handle_id="eval-close")
        await asyncio.to_thread(runner.wait_started.wait)

        await executor.close()

        assert runner.cancellations == 1
        assert runner.wait_finished.is_set()
        observed = await executor.inspect("eval-close")
        assert observed is not None
        assert observed.state is EvaluationState.CANCELED
    finally:
        runner.release()


def test_command_managed_stage_disables_shared_service() -> None:
    payload = SlurmStagePayload(target_lifecycle=SlurmTargetLifecycle.COMMAND_MANAGED)

    assert payload.target_lifecycle is SlurmTargetLifecycle.COMMAND_MANAGED


@pytest.mark.asyncio
async def test_cancelling_the_execution_task_cancels_the_submitted_slurm_job(
    tmp_path: Path,
) -> None:
    config = _config()
    runner = _BlockingCluster(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
    )
    try:
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

        assert runner.handle is not None
        assert runner.cancelled_job_ids == [runner.handle.job.job_id]
        observed = await executor.inspect("eval-interrupted")
        assert observed is not None
        assert observed.state is EvaluationState.CANCELED
    finally:
        runner.release()


@pytest.mark.asyncio
async def test_a_cancelled_cancel_still_finishes_the_scancel_before_it_returns(
    tmp_path: Path,
) -> None:
    config = _config()
    runner = _GatedScancelCluster(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
    )
    try:
        await executor.submit(_request(), handle_id="eval-double-cancel")
        await asyncio.to_thread(runner.wait_started.wait)
        canceller = asyncio.create_task(executor.cancel("eval-double-cancel"))
        await asyncio.to_thread(runner.scancel_entered.wait)

        # A second cancellation (teardown cancelling a task already being cancelled)
        # must not abandon the scancel worker thread mid-flight.
        canceller.cancel()
        for _ in range(50):
            await asyncio.sleep(0)
        assert not canceller.done()

        runner.scancel_gate.set()
        outcome = await asyncio.gather(canceller, return_exceptions=True)

        assert [type(item) for item in outcome] == [asyncio.CancelledError]
        assert runner.scancel_done.is_set()
        assert runner.handle is not None
        assert runner.cancelled_job_ids == [runner.handle.job.job_id]
    finally:
        runner.scancel_gate.set()
        runner.release()


@pytest.mark.asyncio
async def test_a_failed_scancel_does_not_stop_the_cancellation_from_finishing(
    tmp_path: Path,
) -> None:
    config = _config()
    runner = _UnreachableSchedulerCluster(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
    )
    try:
        await executor.submit(_request(), handle_id="eval-unreachable")
        await asyncio.to_thread(runner.wait_started.wait)
        executions = [
            task for task in asyncio.all_tasks() if task.get_name().startswith("vibesys-slurm-")
        ]

        executions[0].cancel()
        outcomes = await asyncio.gather(*executions, return_exceptions=True)

        assert [type(outcome) for outcome in outcomes] == [asyncio.CancelledError]
        # A failed scancel may be retried by the next cleanup step, never skipped.
        assert runner.handle is not None
        assert set(runner.cancelled_job_ids) == {runner.handle.job.job_id}
    finally:
        runner.release()


class _PendingCancellationCluster(_BlockingCluster):
    """Scheduler accepts cancellation separately from the eventual termination."""

    terminate: bool = False

    def cancel(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCancelOutcome:
        if self.terminate:
            return super().cancel(target, by_job_id=by_job_id)
        observed = _ScenarioCluster.inspect(self, target, by_job_id=by_job_id)
        if isinstance(observed, ClusterObservation):
            self.cancellations += 1
            self.cancelled_job_ids.append(observed.job_id)
            return ClusterCancelRequested(
                operation_id=observed.operation_id, job_id=observed.job_id
            )
        return observed


@pytest.mark.asyncio
async def test_scancel_acknowledgement_does_not_complete_release_or_suppress_retry(
    tmp_path: Path,
) -> None:
    """Scope cleanup must observe terminal scheduler state, not just a sent request."""
    config = _config()
    runner = _PendingCancellationCluster(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
        cancel_confirmation_seconds=3 * config.poll_interval_seconds,
    )
    try:
        await executor.submit(_request(), handle_id="eval-pending-cancel")
        await asyncio.to_thread(runner.wait_started.wait)
        with pytest.raises(ExecutorCancellationUnconfirmedError, match="was requested for job"):
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
    finally:
        runner.release()


class _LostSubmissionAcknowledgementCluster(_ScenarioCluster):
    """Accepted submission whose reply and subsequent identity observations are lost."""

    def __init__(self, config: SlurmConfig) -> None:
        super().__init__(config)
        self.accepted = threading.Event()
        self._lost_reply = True
        self._scheduler_states = (SlurmJobStatus.UNKNOWN,)

    def _accepted(self) -> None:
        super()._accepted()
        self.accepted.set()

    def cancel(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCancelOutcome:
        observed = super().inspect(target, by_job_id=by_job_id)
        return ClusterUnknown(
            operation_id=observed.operation_id, reason="scheduler identity unknown"
        )


@pytest.mark.asyncio
async def test_missing_external_identity_keeps_dispatched_cancellation_unresolved(
    tmp_path: Path,
) -> None:
    """A lost accepted Slurm handle cannot manufacture CANCELED or completed cleanup."""
    config = _config()
    runner = _LostSubmissionAcknowledgementCluster(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
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
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
    )
    restored = EvaluationCoordinator(resumed, store, FakeClock())
    with pytest.raises(ExecutorCancellationUnknownError, match="unknown external identity"):
        await restored.cancel(handle.id)
    remaining = await store.get(handle.id)
    assert remaining is not None
    assert remaining.state is not EvaluationState.CANCELED
    assert runner.submissions == 1


class _RejectedBeforeSubmissionCluster(_ScenarioCluster):
    """Staging rejection retains definitive absence of scheduler acceptance."""

    def __init__(self, config: SlurmConfig) -> None:
        super().__init__(config)
        self._rejection = "Slurm transport exec failed with exit code 255"


@pytest.mark.asyncio
async def test_known_staging_rejection_is_failed_and_cleanup_needs_no_external_identity(
    tmp_path: Path,
) -> None:
    """A known pre-submit fault remains ordinary planner failure, not unknown cleanup."""
    config = _config()
    runner = _RejectedBeforeSubmissionCluster(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
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


class _GatedStagingRejectionCluster(_RejectedBeforeSubmissionCluster):
    def __init__(self, config: SlurmConfig) -> None:
        super().__init__(config)
        self.staging_started = threading.Event()
        self.release_staging = threading.Event()

    def _dispatch(self) -> None:
        self.staging_started.set()
        self.release_staging.wait()


@pytest.mark.asyncio
async def test_cancellation_during_rejected_staging_preserves_definite_failure(
    tmp_path: Path,
) -> None:
    config = _config()
    runner = _GatedStagingRejectionCluster(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
    )
    try:
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
    finally:
        runner.release_staging.set()


@pytest.mark.parametrize("operation_id", ["", "../outside", "with space", "x" * 129])
def test_scenario_cluster_preserves_library_validation(operation_id: str, tmp_path: Path) -> None:
    cluster = _ScenarioCluster(_config())
    outcome = cluster.submit(
        SlurmJobRequest(workspace=_workspace(tmp_path), command=("true",)),
        operation_id=operation_id,
    )
    assert isinstance(outcome, ClusterRejected)
    assert cluster.submissions == 0


def test_scenario_cluster_duplicate_identity_does_not_repeat_acceptance(tmp_path: Path) -> None:
    cluster = _ScenarioCluster(_config())
    request = SlurmBatchRequest(
        workspace=_workspace(tmp_path),
        stages=(SlurmBatchStage(name="accuracy", command=("true",)),),
    )
    first = cluster.submit(request, operation_id="same")
    duplicate = cluster.submit(request, operation_id="same")
    assert isinstance(first, ClusterSubmitted)
    assert isinstance(duplicate, ClusterSubmitted)
    assert duplicate.handle == first.handle
    assert cluster.submissions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [None, "", " ", "\t"])
async def test_executor_rejects_missing_or_blank_commands_before_acceptance(
    tmp_path: Path, command: str | None
) -> None:
    cluster = _ScenarioCluster(_config())
    executor = SlurmEvaluationExecutor(
        _config(),
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=cluster,
    )
    request = EvaluationRequest(
        key="invalid-command",
        stages=(
            EvaluationStep(
                name="accuracy",
                payload=SlurmStagePayload(command=command).model_dump(mode="json"),
            ),
        ),
    )
    try:
        with pytest.raises(ExecutorRejectedError, match="command"):
            await executor.submit(request, handle_id="invalid-command")
        assert cluster.submissions == 0
        assert await executor.inspect("invalid-command") is None
    finally:
        await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("handle_id", ["", "../outside", "with space", "x" * 129])
async def test_executor_rejects_invalid_identity_before_persistence(
    tmp_path: Path, handle_id: str
) -> None:
    cluster = _ScenarioCluster(_config())
    executor = SlurmEvaluationExecutor(
        _config(),
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=cluster,
    )
    try:
        for operation, error in (
            (lambda: executor.submit(_request(), handle_id=handle_id), ExecutorRejectedError),
            (lambda: executor.inspect(handle_id), SlurmError),
            (lambda: executor.cancel(handle_id), SlurmError),
        ):
            with pytest.raises(error, match="operation_id"):
                await operation()
        assert cluster.submissions == 0
        assert list((tmp_path / "handles").iterdir()) == []
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_inactive_executor_cancels_prepared_intent_without_external_identity(
    tmp_path: Path,
) -> None:
    cluster = FakeCluster()
    admission = SharedSlurmAdmission(1)
    workspace = _workspace(tmp_path)
    first = SlurmEvaluationExecutor(
        _config(),
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=cluster,
        admission=admission,
    )
    inactive = SlurmEvaluationExecutor(
        _config(),
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=cluster,
        admission=admission,
    )
    try:
        async with admission.lease("occupied"):
            await first.submit(_request(), handle_id="prepared")
            await inactive.cancel("prepared")
            observed = await inactive.inspect("prepared")
            assert observed is not None
            assert observed.state is EvaluationState.CANCELED
            unknown = cluster.inspect("prepared")
            assert isinstance(unknown, ClusterUnknown)
            assert unknown.job_id is None
            await first.close()
        rejected = cluster.submit(
            SlurmBatchRequest(
                workspace=workspace,
                stages=(SlurmBatchStage(name="accuracy", command=("true",)),),
            ),
            operation_id="prepared",
        )
        assert isinstance(rejected, ClusterRejected)
    finally:
        await first.close()
        await inactive.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("implementation", ["fake", "slurm"])
async def test_conflicting_executor_cannot_cancel_or_recover_another_payload(
    tmp_path: Path, implementation: str
) -> None:
    workspace = _workspace(tmp_path)
    accepted = threading.Event()
    operation_id = "claimed-operation"
    if implementation == "fake":
        cluster = FakeCluster()
        cluster.script(operation_id, states=(SlurmJobStatus.PENDING,))
        cluster.on_accept(operation_id, accepted.set)
        config = _config()
    else:
        connector = FakeConnector(tmp_path / "connector")
        connector.script(operation_id, states=(SlurmJobStatus.PENDING,))
        connector.on_accept(operation_id, accepted.set)
        remote = tmp_path / "remote"
        remote.mkdir()
        config = SlurmConfig(
            name="fake-cluster",
            remote_workspace_root=str(remote),
            transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        )
        cluster = SlurmCluster(
            SlurmJobRunner(config, process=connector), state_root=tmp_path / "cluster-identity"
        )
    owner = SlurmEvaluationExecutor(
        config,
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "owner-handles",
        cluster=cluster,
    )
    conflicting = SlurmEvaluationExecutor(
        config,
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "conflict-handles",
        cluster=cluster,
    )
    reopened = SlurmEvaluationExecutor(
        config,
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "conflict-handles",
        cluster=cluster,
    )
    request = EvaluationRequest(
        key="claimed",
        stages=tuple(
            EvaluationStep(
                name=name,
                payload=SlurmStagePayload(command="true").model_dump(mode="json"),
            )
            for name in ("accuracy", "benchmark")
        ),
    )
    changed = request.model_copy(
        update={
            "stages": (
                request.stages[0].model_copy(
                    update={"payload": SlurmStagePayload(command="false").model_dump(mode="json")}
                ),
                request.stages[1],
            )
        }
    )
    try:
        await owner.submit(request, handle_id=operation_id)
        await asyncio.to_thread(accepted.wait)
        await conflicting.submit(changed, handle_id=operation_id)
        observed = await _terminal(conflicting, operation_id)
        assert observed.state is EvaluationState.FAILED
        assert observed.failure is not None
        assert "different request" in observed.failure
        await conflicting.close()
        restored = await _terminal(reopened, operation_id)
        assert restored.state is EvaluationState.FAILED
        await reopened.cancel(operation_id)
        await reopened.submit(changed, handle_id=operation_id)
        retried = await reopened.inspect(operation_id)
        assert retried is not None
        assert retried.state is EvaluationState.FAILED
        original = cluster.inspect(operation_id)
        assert isinstance(original, ClusterObservation)
        assert original.status is SlurmJobStatus.PENDING
    finally:
        await conflicting.close()
        await reopened.close()
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", tuple(SlurmJobStatus))
async def test_read_only_restart_inspection_never_resumes_or_cancels_work(
    tmp_path: Path, status: SlurmJobStatus
) -> None:
    config = _config()
    runner = _ScenarioCluster(config)
    first = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
    )
    await first.submit(_request(), handle_id="inspect-only")
    assert (await _terminal(first, "inspect-only")).state is EvaluationState.SUCCEEDED
    runner.script("inspect-only", states=(status,))
    runner.job_status = status
    resumed = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
    )
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
    runner = _BlockingCluster(config)
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=runner,
    )
    coordinator = EvaluationCoordinator(
        executor, FilesystemEvaluationStore(tmp_path / "records"), FakeClock()
    )
    handle = await coordinator.submit(_request())
    await asyncio.to_thread(runner.wait_started.wait)
    active = await coordinator.snapshot(handle.id)
    assert active.state is EvaluationState.RUNNING
    runner.script(handle.id, states=(SlurmJobStatus.PENDING,))
    runner.job_status = SlurmJobStatus.PENDING
    inspected = await coordinator.inspect_snapshot(handle.id)
    assert inspected is not None
    assert inspected.state is EvaluationState.RUNNING
    assert inspected.current_stage is None
    assert runner.submissions == 1
    assert runner.cancellations == 0
    await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("implementation", ["fake", "slurm"])
async def test_poll_reports_each_lifecycle_phase_without_submitting_or_recovering(
    tmp_path: Path, implementation: str
) -> None:
    operation_id = "polled-operation"
    states = (SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING, SlurmJobStatus.COMPLETED)
    if implementation == "fake":
        cluster = FakeCluster()
        config = _config()
        cluster.script(operation_id, states=states)
    else:
        connector = FakeConnector(tmp_path / "connector")
        connector.script(operation_id, states=states)
        remote = tmp_path / "remote"
        remote.mkdir()
        config = SlurmConfig(
            name="fake-cluster",
            remote_workspace_root=str(remote),
            transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        )
        cluster = SlurmCluster(
            SlurmJobRunner(config, process=connector), state_root=tmp_path / "cluster-identity"
        )
    executor = SlurmEvaluationExecutor(
        config,
        workspace=_workspace(tmp_path),
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=cluster,
    )
    try:
        assert (await executor.poll(operation_id)).phase is PollPhase.UNSUBMITTED
        assert list((tmp_path / "handles").iterdir()) == []
        await executor.submit(_request(), handle_id=operation_id)
        await _terminal(executor, operation_id)
        phases = [(await executor.poll(operation_id)).phase for _ in range(4)]
        assert phases[-1] is PollPhase.ENDED
        assert phases == sorted(phases, key=list(PollPhase).index)
        ended = await executor.poll(operation_id)
        assert ended.terminal is not None
        # No stage result was scripted, so both clusters end without inventing a success.
        assert ended.terminal.state is EvaluationState.FAILED
    finally:
        await executor.close()
