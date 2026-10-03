"""Fused Slurm execution for semantic agent evaluations."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, cast

import pytest

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.run.evaluation_backend import SemanticEvaluationStage
from vibesys.run.slurm_evaluation import SlurmSemanticEvaluationExecutor
from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    ExecutorObservation,
    ResourceRequirements,
    StageState,
    TrustedEvidence,
)
from vs_project.api import StateNamespace
from vs_runtime.api.infrastructure import ScalarBenchmarkContract, TrustedEvaluationPlan
from vs_runtime.api.testing import FakeRun
from vs_sandbox.api.slurm import SlurmEvaluationPlan, SlurmExecutionPolicy
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
        errors: tuple[Exception | None, ...],
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
        index = len(self.candidates)
        candidate = _TrackedCandidate(inner, release=self.release, error=self._errors[index])
        self.candidates.append(candidate)
        return cast("CandidateWorkspace", candidate)

    async def adopt(self, revision: str) -> None:
        await self._inner.adopt(revision)

    async def export_patch(self, revision: str) -> str:
        return await self._inner.export_patch(revision)


class _Runner(SlurmJobRunner):
    def __init__(
        self,
        config: SlurmConfig,
        *,
        block_wait: bool = False,
        fail_cancel: bool = False,
        stages: tuple[SlurmBatchStageResult, ...] | None = None,
    ) -> None:
        super().__init__(config)
        self._stages = stages
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
        self.wait_started = threading.Event()
        self.wait_finished = threading.Event()
        self._release_wait = threading.Event()
        self._fail_cancel = fail_cancel
        if not block_wait:
            self._release_wait.set()

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
        self.wait_started.set()
        self._release_wait.wait()
        self.wait_finished.set()
        return SlurmBatchWaitResult(handle=handle, status=SlurmJobStatus.COMPLETED, timed_out=False)

    def collect_batch(self, handle: SlurmBatchHandle) -> SlurmBatchResult:
        del handle
        framed = (
            "__VIBESYS_FRAMEWORK_BENCHMARK_JSON__\n"
            '{"throughput": 12}\n'
            "__VIBESYS_FRAMEWORK_BENCHMARK_JSON_END__"
        )
        return SlurmBatchResult(
            job_id="42",
            job_exit_code=0,
            job_output="",
            phase_timings_seconds={},
            content_cache_hits=0,
            stages=self._stages
            or (
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

    def cancel_batch(self, handle: SlurmBatchHandle) -> None:
        del handle
        self.cancellations += 1
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


def _request(snapshot: str) -> EvaluationRequest:
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
            for kind in (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK)
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
async def test_semantic_executor_fuses_recovers_and_reports_shared_capacity(tmp_path: Path) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    namespace = _namespace(tmp_path)
    config = _config()
    runner = _Runner(config)
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
        run.workspaces,
        namespace,
        tmp_path / "handles",
        runner=runner,
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
        run.workspaces,
        namespace,
        tmp_path / "handles",
        runner=runner,
    )
    assert (await _terminal(resumed, "fused")).state is EvaluationState.SUCCEEDED
    assert runner.submissions == 1
    await first.close()
    await resumed.close()


@pytest.mark.asyncio
async def test_failed_accuracy_fails_the_evaluation_with_its_diagnostics(tmp_path: Path) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    config = _config()
    runner = _Runner(
        config,
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
        run.workspaces,
        _namespace(tmp_path),
        tmp_path / "handles",
        runner=runner,
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
async def test_close_drains_provider_before_discarding_candidate(tmp_path: Path) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    config = _config()
    runner = _Runner(config, block_wait=True)
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=tmp_path / "slurm.toml",
            accuracy_command=("python", "accuracy.py"),
            benchmark_command=("python", "benchmark.py"),
        ),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        run.workspaces,
        _namespace(tmp_path),
        tmp_path / "handles",
        runner=runner,
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
    runner = _Runner(config)
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(config_path=tmp_path / "slurm.toml"),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        workspaces,
        _namespace(tmp_path),
        tmp_path / "handles",
        runner=runner,
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
        run.workspaces,
        errors=(ValueError("first discard"), RuntimeError("second discard")),
    )
    config = _config()
    runner = _Runner(config)
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(config_path=tmp_path / "slurm.toml"),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        workspaces,
        _namespace(tmp_path),
        tmp_path / "handles",
        runner=runner,
    )
    await executor.submit(_request(snapshot), handle_id="cleanup-one")
    await executor.submit(_request(snapshot), handle_id="cleanup-two")
    await _terminal(executor, "cleanup-one")
    await _terminal(executor, "cleanup-two")

    with pytest.raises(ExceptionGroup) as caught:
        await executor.close()

    assert [candidate.discard_calls for candidate in workspaces.candidates] == [1, 1]
    assert {str(error) for error in caught.value.exceptions} == {
        "first discard",
        "second discard",
    }


@pytest.mark.asyncio
async def test_close_discards_workspace_when_provider_cleanup_fails(tmp_path: Path) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path / "project", supports_parallel_candidates=True)
    snapshot = await run.workspaces.root.snapshot("candidate")
    config = _config()
    runner = _Runner(config, block_wait=True, fail_cancel=True)
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(config_path=tmp_path / "slurm.toml"),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        run.workspaces,
        _namespace(tmp_path),
        tmp_path / "handles",
        runner=runner,
    )
    await executor.submit(_request(snapshot), handle_id="provider-cleanup-error")
    await asyncio.to_thread(runner.wait_started.wait)

    with pytest.raises(ExceptionGroup, match="cleanup failed"):
        await executor.close()

    assert run.workspaces.candidates[0].discarded
