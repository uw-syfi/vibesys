"""Fused Slurm execution for semantic agent evaluations."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.run.evaluation_backend import SemanticEvaluationStage
from vibesys.run.slurm_evaluation import SlurmSemanticEvaluationExecutor
from vs_evaluation.api import (
    ContentDigest,
    EvaluationCoordinator,
    EvaluationFailed,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    ExecutorObservation,
    ResourceRequirements,
    StageFailureKind,
    StageState,
    TrustedEvidence,
)
from vs_evaluation.api.testing import FakeClock, InMemoryEvaluationStore
from vs_project.api import StateNamespace
from vs_runtime.api import RunCleanupError
from vs_runtime.api.infrastructure import ScalarBenchmarkContract, TrustedEvaluationPlan
from vs_runtime.api.testing import FakeRun
from vs_sandbox.api.slurm import PROFILE_OUTPUT_ROOT, SlurmEvaluationPlan, SlurmExecutionPolicy
from vs_slurm.api import (
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStageResult,
    SlurmBatchWaitResult,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmJobHandle,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmSshTransport,
)
from vs_slurm.fake_connector import FakeConnector
from vs_slurm.wiring import SlurmCluster

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api import CandidateWorkspace, Workspace, Workspaces


class _TrackedCandidate:
    def __init__(
        self,
        inner: CandidateWorkspace,
        *,
        release: asyncio.Event,
        error: Exception | None,
    ) -> None:
        self._inner = inner
        self._release = release
        self._error = error
        self.discard_started = asyncio.Event()
        self.discard_calls = 0

    @property
    def path(self) -> Path:
        return self._inner.path

    async def discard(self) -> None:
        self.discard_started.set()
        await self._release.wait()
        self.discard_calls += 1
        if self._error is not None:
            raise self._error
        await self._inner.discard()


class _TrackedWorkspaces:
    def __init__(
        self,
        inner: Workspaces,
        *,
        errors: tuple[Exception | None, ...] = (),
        blocked: bool = False,
    ) -> None:
        self._inner = inner
        self._errors = errors
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.candidates: list[_TrackedCandidate] = []

    @property
    def root(self) -> Workspace:
        return self._inner.root

    @property
    def supports_parallel_candidates(self) -> bool:
        return self._inner.supports_parallel_candidates

    async def create_candidate(
        self,
        from_revision: str | None = None,
        *,
        member_id: str | None = None,
    ) -> CandidateWorkspace:
        inner = await self._inner.create_candidate(from_revision, member_id=member_id)
        inner.path.mkdir(parents=True, exist_ok=True)
        index = len(self.candidates)
        error = self._errors[index] if index < len(self._errors) else None
        candidate = _TrackedCandidate(inner, release=self.release, error=error)
        self.candidates.append(candidate)
        return cast("CandidateWorkspace", candidate)

    async def adopt(self, revision: str) -> None:
        await self._inner.adopt(revision)

    async def export_patch(self, revision: str) -> str:
        return await self._inner.export_patch(revision)


class _Runner(SlurmJobRunner):
    def __init__(
        self,
        state_root: Path,
        *,
        block_wait: bool = False,
        fail_cancel: bool = False,
        stages: tuple[SlurmBatchStageResult, ...] | None = None,
        service_log_tail: str = "",
    ) -> None:
        config = _config().model_copy(
            update={
                "remote_workspace_root": str(state_root / "runs"),
                "transport": SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
            }
        )
        scratch_root = state_root / "scratch"
        scratch_root.mkdir(parents=True, exist_ok=True)
        super().__init__(
            config, process=FakeConnector(state_root / "transport"), scratch_root=scratch_root
        )
        self._stages = stages
        self._service_log_tail = service_log_tail
        self.job_exit_code = 0
        self.collection_failure: str | None = None
        self.submissions = 0
        self.request: SlurmBatchRequest | None = None
        self.handle = SlurmBatchHandle.model_validate(
            {
                "job": {
                    "job_id": "42",
                    "invocation_id": "semantic",
                    "config_identity": "0" * 64,
                    "remote_workspace": "/runs/semantic/workspace",
                    "remote_status_path": "/runs/semantic/status",
                    "remote_log_path": "/runs/semantic/log",
                },
                "stages": ({"name": "accuracy"}, {"name": "benchmark"}),
                "submission_seconds": 1.0,
            }
        )
        self.cancellations = 0
        self.job_status = SlurmJobStatus.PENDING
        self.wait_started = threading.Event()
        self.wait_finished = threading.Event()
        self._release_wait = threading.Event()
        self._fail_cancel = fail_cancel
        if not block_wait:
            self._release_wait.set()

    def submit_batch(
        self, request: SlurmBatchRequest, *, operation_id: str | None = None
    ) -> SlurmBatchHandle:
        assert operation_id is not None
        recovered = self.recover_handle(request, operation_id=operation_id, job_id="42")
        assert isinstance(recovered, SlurmBatchHandle)
        self.handle = recovered.model_copy(update={"submission_seconds": 1.0})
        self.submissions += 1
        self.request = request
        self.job_status = SlurmJobStatus.RUNNING
        return self.handle

    def inspect_job(self, job_id: str) -> tuple[SlurmJobStatus, str | None, str | None]:
        """Expose the same deterministic scheduler evidence through public inspection."""
        assert job_id == self.handle.job.job_id
        self.wait_started.set()
        if self._release_wait.is_set():
            self.wait_finished.set()
            if self.job_status is SlurmJobStatus.RUNNING:
                self.job_status = (
                    SlurmJobStatus.COMPLETED if self.job_exit_code == 0 else SlurmJobStatus.FAILED
                )
        return self.job_status, None, None

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
        if self.job_status is SlurmJobStatus.RUNNING:
            self.job_status = SlurmJobStatus.COMPLETED
        return SlurmBatchWaitResult(handle=handle, status=self.job_status, timed_out=False)

    def collect_batch(self, handle: SlurmBatchHandle) -> SlurmBatchResult:
        del handle
        framed = (
            "__VIBESYS_FRAMEWORK_BENCHMARK_JSON__\n"
            '{"throughput": 12}\n'
            "__VIBESYS_FRAMEWORK_BENCHMARK_JSON_END__"
        )
        result = SlurmBatchResult(
            job_id="42",
            job_exit_code=self.job_exit_code,
            job_output="",
            phase_timings_seconds={},
            content_cache_hits=0,
            service_log_tail=self._service_log_tail,
            stages=self._stages
            if self._stages is not None
            else (
                SlurmBatchStageResult(
                    name="accuracy",
                    exit_code=0,
                    stdout="ok",
                    stderr="",
                    elapsed_seconds=1.0,
                    skipped=False,
                ),
                SlurmBatchStageResult(
                    name="benchmark",
                    exit_code=0,
                    stdout=framed,
                    stderr="",
                    elapsed_seconds=2.0,
                    skipped=False,
                ),
            ),
        )
        if self._stages is None and self.request is not None:
            requested = {stage.name for stage in self.request.stages}
            result = replace(
                result, stages=tuple(stage for stage in result.stages if stage.name in requested)
            )
        return (
            result
            if self.collection_failure is None
            else replace(result, collection_failure=self.collection_failure)
        )

    def poll_batch(self, handle: SlurmBatchHandle) -> SlurmJobStatus:
        """Inspect the same external job state used by wait and cancellation."""
        assert handle == self.handle
        return self.job_status

    def cancel(self, handle: SlurmJobHandle) -> None:
        """Apply cancellation to the same scripted allocation as inspection."""
        assert handle == self.handle.job
        self.cancel_batch(self.handle)

    def cancel_batch(self, handle: SlurmBatchHandle) -> None:
        del handle
        self.cancellations += 1
        if not self._fail_cancel:
            self.job_status = SlurmJobStatus.CANCELLED
        self._release_wait.set()
        if self._fail_cancel:
            raise RuntimeError


def _namespace(tmp_path: Path) -> StateNamespace:
    root = tmp_path / ".vibesys" / "state" / "slurm-evaluation"
    root.mkdir(parents=True)
    return StateNamespace(project_root=tmp_path, root=root, portable=False)


def _config() -> SlurmConfig:
    return SlurmConfig(
        name="test", remote_workspace_root="/runs", transport=SlurmSshTransport(host="test")
    )


def _request(
    snapshot: str,
    kinds: tuple[EvidenceKind, ...] = (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK),
) -> EvaluationRequest:
    digest = ContentDigest.sha256(b"same")
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    return EvaluationRequest(
        key="semantic-fused",
        stages=tuple(
            EvaluationStep(
                name=kind.value,
                payload=SemanticEvaluationStage(
                    snapshot=snapshot, kind=kind, fingerprints=fingerprints
                ).model_dump(mode="json"),
            )
            for kind in kinds
        ),
    )


async def _terminal(
    executor: SlurmSemanticEvaluationExecutor, handle_id: str
) -> ExecutorObservation:
    while True:
        observed = await executor.inspect(handle_id)
        if observed is not None and observed.state in {
            EvaluationState.SUCCEEDED,
            EvaluationState.FAILED,
        }:
            return observed
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        (0, None, 0, None),
        (0, None, 7, None),
        (1, None, 0, None),
        (70, None, 0, None),
        (255, None, 0, None),
        (0, "metadata unavailable", 0, None),
        (0, None, None, None),
        (0, None, 0, "stdout unavailable"),
        (0, None, 7, "stderr unavailable"),
    ],
)
@pytest.mark.parametrize(
    "kind", [EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK, EvidenceKind.PROFILE]
)
async def test_semantic_executor_preserves_infrastructure_failure_provenance(
    tmp_path: Path,
    fault: tuple[int, str | None, int | None, str | None],
    kind: EvidenceKind,
) -> None:
    job_exit_code, batch_failure, stage_exit_code, stage_failure = fault
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    config = _config()
    stage = SlurmBatchStageResult(
        name=kind.value,
        exit_code=stage_exit_code,
        stdout="retained accuracy diagnostic",
        stderr="retained server diagnostic",
        elapsed_seconds=1.0,
        skipped=False,
    )
    if stage_failure is not None:
        stage = replace(stage, collection_failure=stage_failure)
    runner = _Runner(tmp_path / "runner", stages=(stage,))
    runner.job_exit_code = job_exit_code
    runner.collection_failure = batch_failure
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=tmp_path / "slurm.toml",
            accuracy_command=("python", "accuracy.py"),
            benchmark_command=("python", "benchmark.py"),
            profile_command=("python", "profile.py"),
        ),
        TrustedEvaluationPlan(accuracy_command="unused"),
        _TrackedWorkspaces(run.workspaces),
        _namespace(tmp_path),
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )
    await executor.submit(_request(snapshot, (kind,)), handle_id="provenance")
    observed = await _terminal(executor, "provenance")

    expected_state = (
        EvaluationState.FAILED
        if job_exit_code != 0 or batch_failure or stage_exit_code is None or stage_failure
        else EvaluationState.SUCCEEDED
    )
    assert observed.state is expected_state
    evidence = TrustedEvidence.model_validate(observed.stage_results[0].result)
    passes = expected_state is EvaluationState.SUCCEEDED and stage_exit_code == 0
    assert evidence.outcome is (EvidenceOutcome.PASSED if passes else EvidenceOutcome.FAILED)
    if expected_state is EvaluationState.FAILED:
        assert observed.failure
        assert observed.stage_results[0].state is StageState.FAILED
        assert evidence.semantic_summary
    await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        (70, None, 0, None),
        (0, "batch metadata unavailable", 0, None),
        (0, None, 0, "first stage artifact unavailable"),
        (0, None, None, None),
    ],
)
async def test_coordinator_retains_completed_stages_after_late_infrastructure_failure(
    tmp_path: Path, fault: tuple[int, str | None, int | None, str | None]
) -> None:
    job_exit_code, batch_failure, stage_exit_code, stage_failure = fault
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    config = _config()
    accuracy = SlurmBatchStageResult(
        name="accuracy",
        exit_code=stage_exit_code,
        stdout="accuracy output",
        stderr="",
        elapsed_seconds=1.0,
        skipped=False,
    )
    if stage_failure is not None:
        accuracy = replace(accuracy, collection_failure=stage_failure)
    benchmark = SlurmBatchStageResult(
        name="benchmark",
        exit_code=0,
        stdout="benchmark output",
        stderr="",
        elapsed_seconds=2.0,
        skipped=False,
    )
    runner = _Runner(tmp_path / "runner", stages=(accuracy, benchmark))
    runner.job_exit_code = job_exit_code
    runner.collection_failure = batch_failure
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=tmp_path / "slurm.toml",
            accuracy_command=("python", "accuracy.py"),
            benchmark_command=("python", "benchmark.py"),
        ),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        _TrackedWorkspaces(run.workspaces),
        _namespace(tmp_path),
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )
    coordinator = EvaluationCoordinator(
        executor, InMemoryEvaluationStore(), FakeClock(), max_await_timeout_s=5
    )
    handle = await coordinator.submit(_request(snapshot))
    assert isinstance(await handle.await_result(5), EvaluationFailed)
    record = await coordinator.snapshot(handle.id)
    assert record.state is EvaluationState.FAILED
    assert len(record.stage_results) == 2
    assert all(stage.result is not None for stage in record.stage_results)
    assert record.stage_results[0].failure_kind is StageFailureKind.COLLECTION
    evidence = tuple(TrustedEvidence.model_validate(stage.result) for stage in record.stage_results)
    assert evidence[0].outcome is EvidenceOutcome.FAILED
    assert evidence[1].outcome is (
        EvidenceOutcome.FAILED if job_exit_code or batch_failure else EvidenceOutcome.PASSED
    )
    await executor.close()


@pytest.mark.asyncio
async def test_semantic_executor_fuses_recovers_and_reports_shared_capacity(tmp_path: Path) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    namespace = _namespace(tmp_path)
    config = _config()
    runner = _Runner(tmp_path / "runner")
    plan = SlurmEvaluationPlan(
        config_path=tmp_path / "slurm.toml",
        accuracy_command=("python", "accuracy.py"),
        benchmark_command=("python", "benchmark.py"),
    )
    trusted = TrustedEvaluationPlan(
        accuracy_command="unused",
        benchmark_command="unused",
        benchmark_contract=ScalarBenchmarkContract(output_argument="--output", metric="throughput"),
    )
    first = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        plan,
        trusted,
        _TrackedWorkspaces(run.workspaces),
        namespace,
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )

    availability = await first.availability(ResourceRequirements())
    assert availability.capacity == 1
    await first.submit(_request(snapshot), handle_id="fused")
    observed = await _terminal(first, "fused")

    assert observed.state is EvaluationState.SUCCEEDED
    assert runner.submissions == 1
    assert runner.request is not None
    assert [stage.name for stage in runner.request.stages] == ["accuracy", "benchmark"]
    benchmark = TrustedEvidence.model_validate(observed.stage_results[1].result)
    assert benchmark.metrics[0].value == 12

    resumed = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        plan,
        trusted,
        _TrackedWorkspaces(run.workspaces),
        namespace,
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )
    assert (await _terminal(resumed, "fused")).state is EvaluationState.SUCCEEDED
    assert runner.submissions == 1
    await first.close()
    await resumed.close()


@pytest.mark.asyncio
async def test_unsupported_profile_evaluation_fails_instead_of_staying_queued(
    tmp_path: Path,
) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    config = _config()
    runner = _Runner(tmp_path / "runner")
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=tmp_path / "slurm.toml",
            accuracy_command=("python", "accuracy.py"),
            benchmark_command=("python", "benchmark.py"),
        ),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        _TrackedWorkspaces(run.workspaces),
        _namespace(tmp_path),
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )
    clock = FakeClock()
    coordinator = EvaluationCoordinator(
        executor, InMemoryEvaluationStore(), clock, max_await_timeout_s=5
    )

    handle = await coordinator.submit(_request(snapshot, (EvidenceKind.PROFILE,)))
    result = await handle.await_result(5)

    assert isinstance(result, EvaluationFailed)
    assert result.message is not None
    assert "not profile" in result.message
    assert runner.submissions == 0
    await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK])
async def test_a_kind_without_a_command_is_rejected_not_passed(
    tmp_path: Path, kind: EvidenceKind
) -> None:
    """A stage with no command ran ``true`` and was recorded as passed evidence."""
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    config = _config()
    runner = _Runner(tmp_path / "runner")
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(config_path=tmp_path / "slurm.toml"),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        _TrackedWorkspaces(run.workspaces),
        _namespace(tmp_path),
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )
    coordinator = EvaluationCoordinator(
        executor, InMemoryEvaluationStore(), FakeClock(), max_await_timeout_s=5
    )

    availability = await executor.availability(ResourceRequirements())
    handle = await coordinator.submit(_request(snapshot, (kind,)))
    result = await handle.await_result(5)

    assert kind.value not in availability.supported_evidence_kinds
    assert isinstance(result, EvaluationFailed)
    assert result.message is not None
    assert f"not {kind.value}" in result.message
    assert runner.submissions == 0
    await executor.close()


@pytest.mark.asyncio
async def test_a_plan_with_a_trusted_capture_produces_profile_evidence(tmp_path: Path) -> None:
    """Regression: no executor produced profile evidence, so every profile was unsupported."""
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    config = _config()
    runner = _Runner(
        tmp_path / "runner",
        stages=(
            SlurmBatchStageResult(
                name="profile",
                exit_code=0,
                stdout="top kernels: gemm 61%, attention 22%\n",
                stderr="",
                elapsed_seconds=3.0,
                skipped=False,
            ),
        ),
    )
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=tmp_path / "slurm.toml",
            benchmark_command=("python", "benchmark.py"),
            profile_command=("python", "rocprof_profiler/remote_capture.py"),
        ),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        _TrackedWorkspaces(run.workspaces),
        _namespace(tmp_path),
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )

    availability = await executor.availability(ResourceRequirements())
    await executor.submit(_request(snapshot, (EvidenceKind.PROFILE,)), handle_id="profile")
    observed = await _terminal(executor, "profile")

    assert EvidenceKind.PROFILE.value in availability.supported_evidence_kinds
    assert observed.state is EvaluationState.SUCCEEDED
    assert runner.request is not None
    assert runner.request.service is None
    (stage,) = runner.request.stages
    assert stage.command[-1] == "python rocprof_profiler/remote_capture.py"
    assert [item.remote_path for item in stage.tree_artifacts] == [PROFILE_OUTPUT_ROOT]
    evidence = TrustedEvidence.model_validate(observed.stage_results[0].result)
    assert evidence.kind is EvidenceKind.PROFILE
    assert evidence.outcome is EvidenceOutcome.PASSED
    assert evidence.semantic_summary == "top kernels: gemm 61%, attention 22%\n"
    await executor.close()


@pytest.mark.asyncio
async def test_a_plan_without_a_capture_reports_no_profile_kind(tmp_path: Path) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    executor = SlurmSemanticEvaluationExecutor(
        _config(),
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=tmp_path / "slurm.toml",
            accuracy_command=("python", "accuracy.py"),
            benchmark_command=("python", "benchmark.py"),
        ),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        _TrackedWorkspaces(run.workspaces),
        _namespace(tmp_path),
        tmp_path / "handles",
        cluster=SlurmCluster(_Runner(tmp_path / "runner"), state_root=tmp_path / "cluster"),
    )

    availability = await executor.availability(ResourceRequirements())

    assert availability.supported_evidence_kinds == ("accuracy", "benchmark")
    await executor.close()


@pytest.mark.asyncio
async def test_failed_accuracy_fails_the_evaluation_with_its_diagnostics(tmp_path: Path) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    config = _config()
    runner = _Runner(
        tmp_path / "runner",
        stages=(
            SlurmBatchStageResult(
                name="accuracy",
                exit_code=1,
                stdout="prompt 3: output differs from the reference",
                stderr="",
                elapsed_seconds=1.0,
                skipped=False,
            ),
            SlurmBatchStageResult(
                name="benchmark",
                exit_code=None,
                stdout="",
                stderr="",
                elapsed_seconds=0.0,
                skipped=True,
            ),
        ),
    )
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=tmp_path / "slurm.toml",
            accuracy_command=("python", "accuracy.py"),
            benchmark_command=("python", "benchmark.py"),
        ),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        _TrackedWorkspaces(run.workspaces),
        _namespace(tmp_path),
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )

    await executor.submit(_request(snapshot), handle_id="fused")
    observed = await _terminal(executor, "fused")

    assert observed.state is EvaluationState.FAILED
    assert observed.failure is not None
    assert "prompt 3: output differs from the reference" in observed.failure
    accuracy = TrustedEvidence.model_validate(observed.stage_results[0].result)
    assert accuracy.outcome is EvidenceOutcome.FAILED
    assert observed.stage_results[1].state is StageState.SKIPPED
    await executor.close()


@pytest.mark.asyncio
async def test_silent_benchmark_failure_reports_the_time_limit_and_server_log(
    tmp_path: Path,
) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    config = _config()
    runner = _Runner(
        tmp_path / "runner",
        stages=(
            SlurmBatchStageResult(
                name="accuracy",
                exit_code=0,
                stdout="ok",
                stderr="",
                elapsed_seconds=1.0,
                skipped=False,
            ),
            SlurmBatchStageResult(
                name="benchmark",
                exit_code=124,
                stdout="",
                stderr="",
                elapsed_seconds=600.0,
                skipped=False,
            ),
        ),
        service_log_tail="decode: 4000 steps, 120 ms/step at 22 sequences",
    )
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=tmp_path / "slurm.toml",
            accuracy_command=("python", "accuracy.py"),
            benchmark_command=("python", "benchmark.py"),
        ),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        _TrackedWorkspaces(run.workspaces),
        _namespace(tmp_path),
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )

    await executor.submit(_request(snapshot), handle_id="fused")
    observed = await _terminal(executor, "fused")

    benchmark = TrustedEvidence.model_validate(observed.stage_results[1].result)
    assert benchmark.semantic_summary is not None
    assert "hit its time limit" in benchmark.semantic_summary
    assert "decode: 4000 steps, 120 ms/step" in benchmark.semantic_summary
    await executor.close()


@pytest.mark.asyncio
async def test_close_drains_provider_before_discarding_candidate(tmp_path: Path) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    config = _config()
    runner = _Runner(tmp_path / "runner", block_wait=True)
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=tmp_path / "slurm.toml",
            accuracy_command=("python", "accuracy.py"),
            benchmark_command=("python", "benchmark.py"),
        ),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        _TrackedWorkspaces(run.workspaces),
        _namespace(tmp_path),
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )
    await executor.submit(_request(snapshot), handle_id="close-running")
    await asyncio.to_thread(runner.wait_started.wait)

    await executor.close()

    assert runner.cancellations == 1
    assert runner.wait_finished.is_set()
    assert run.workspaces.candidates[0].discarded


@pytest.mark.asyncio
async def test_close_retains_execution_until_cleanup_settles(tmp_path: Path) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    workspaces = _TrackedWorkspaces(run.workspaces, errors=(None, None), blocked=True)
    config = _config()
    runner = _Runner(tmp_path / "runner")
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=tmp_path / "slurm.toml",
            accuracy_command=("python", "accuracy.py"),
            benchmark_command=("python", "benchmark.py"),
        ),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        workspaces,
        _namespace(tmp_path),
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )
    await executor.submit(_request(snapshot), handle_id="retained-during-close")
    assert (await _terminal(executor, "retained-during-close")).state is EvaluationState.SUCCEEDED

    closing = asyncio.create_task(executor.close())
    await workspaces.candidates[0].discard_started.wait()
    inspecting = asyncio.create_task(executor.inspect("retained-during-close"))
    await asyncio.sleep(0)

    assert len(workspaces.candidates) == 1
    workspaces.release.set()
    await closing
    assert await inspecting is not None


@pytest.mark.asyncio
async def test_close_attempts_every_cleanup_and_aggregates_errors(tmp_path: Path) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    workspaces = _TrackedWorkspaces(
        _TrackedWorkspaces(run.workspaces),
        errors=(ValueError("first discard"), RuntimeError("second discard")),
    )
    config = _config()
    runner = _Runner(tmp_path / "runner")
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=tmp_path / "slurm.toml",
            accuracy_command=("python", "accuracy.py"),
            benchmark_command=("python", "benchmark.py"),
        ),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        workspaces,
        _namespace(tmp_path),
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )
    await executor.submit(_request(snapshot), handle_id="cleanup-one")
    await executor.submit(_request(snapshot), handle_id="cleanup-two")
    await _terminal(executor, "cleanup-one")
    await _terminal(executor, "cleanup-two")

    with pytest.raises(RunCleanupError) as caught:
        await executor.close()

    assert [candidate.discard_calls for candidate in workspaces.candidates] == [1, 1]
    assert {str(error) for error in caught.value.failures} == {
        "first discard",
        "second discard",
    }


@pytest.mark.asyncio
async def test_close_discards_workspace_when_provider_cleanup_fails(tmp_path: Path) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    config = _config()
    runner = _Runner(tmp_path / "runner", block_wait=True, fail_cancel=True)
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=tmp_path / "slurm.toml",
            accuracy_command=("python", "accuracy.py"),
            benchmark_command=("python", "benchmark.py"),
        ),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        _TrackedWorkspaces(run.workspaces),
        _namespace(tmp_path),
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )
    await executor.submit(_request(snapshot), handle_id="provider-cleanup-error")
    await asyncio.to_thread(runner.wait_started.wait)

    with pytest.raises(RunCleanupError, match="cleanup failed"):
        await executor.close()

    assert run.workspaces.candidates[0].discarded


@pytest.mark.asyncio
async def test_read_only_restart_inspection_does_not_recreate_candidate_workspace(
    tmp_path: Path,
) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    config = _config()
    runner = _Runner(tmp_path / "runner")
    plan = SlurmEvaluationPlan(
        config_path=tmp_path / "slurm.toml",
        accuracy_command=("python", "accuracy.py"),
        benchmark_command=("python", "benchmark.py"),
    )
    namespace = _namespace(tmp_path)
    first = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        plan,
        TrustedEvaluationPlan(),
        _TrackedWorkspaces(run.workspaces),
        namespace,
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )
    await first.submit(_request(snapshot, (EvidenceKind.ACCURACY,)), handle_id="inspect-only")
    assert (await _terminal(first, "inspect-only")).state is EvaluationState.SUCCEEDED
    workspaces = _TrackedWorkspaces(run.workspaces, errors=())
    resumed = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        plan,
        TrustedEvaluationPlan(),
        workspaces,
        namespace,
        tmp_path / "handles",
        cluster=SlurmCluster(runner, state_root=tmp_path / "cluster"),
    )
    assert await resumed.inspect_only("inspect-only") is None
    await resumed.close()
    assert workspaces.candidates == []
    assert runner.submissions == 1
    assert runner.cancellations == 0
    await first.close()
