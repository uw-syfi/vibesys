"""Real executors over the Fake Slurm cluster, shared by every evaluation request test.

``Stack`` is the production path (semantic Slurm executor over the sandbox's
Slurm executor over a ``Cluster``) with only the cluster and workspaces faked.
``ScenarioCluster`` scripts the scheduler states and the raw stage outputs a job
ends with, and counts real submissions so tests can prove "exactly once".
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    DecisionId,
    MeasurementPlan,
    MeasurementStage,
    RequestId,
    RevisionRef,
    RunId,
    Scope,
    SubmitMeasurement,
)
from vs_project.api import StateNamespace
from vs_runtime.api import render_stage_failure
from vs_runtime.api.infrastructure import (
    ScalarBenchmarkContract,
    SemanticSlurmEvaluationExecutor,
    TrustedEvaluationPlan,
)
from vs_runtime.api.testing import FakeWorkspace, FakeWorkspaces
from vs_sandbox.api.slurm import SlurmEvaluationPlan, SlurmExecutionPolicy
from vs_slurm.api import (
    ClusterSubmitOutcome,
    FakeCluster,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStageResult,
    SlurmConfig,
    SlurmJobRequest,
    SlurmJobStatus,
    SlurmSshTransport,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.contracts import CandidateWorkspace

SCOPE = Scope(owner=RunId(root="run"), generation=0)
ADMISSION = DecisionId(root="admission-1")
DIGEST = "ab" * 32
BENCHMARK_OUTPUT = (
    "__VIBESYS_FRAMEWORK_BENCHMARK_JSON__\n"
    '{"throughput": 12}\n'
    "__VIBESYS_FRAMEWORK_BENCHMARK_JSON_END__"
)


class ScenarioCluster(FakeCluster):
    """A Fake cluster whose every submission ends the way the test scripted."""

    def __init__(self) -> None:
        """Start with jobs that complete successfully on the first observation."""
        super().__init__()
        self.states: tuple[SlurmJobStatus, ...] = (SlurmJobStatus.COMPLETED,)
        self.accuracy_exit = 0
        self.benchmark_exit = 0
        self.benchmark_output = BENCHMARK_OUTPUT
        self.submissions: list[str] = []
        self.accepted = threading.Event()

    @property
    def cancelled(self) -> list[str]:
        """Operation identities the scheduler was asked to cancel."""
        return sorted(self._cancelled)

    def submit(
        self, request: SlurmJobRequest | SlurmBatchRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        """Script the job's ending on first sight of its identity, then behave as Fake."""
        if isinstance(request, SlurmBatchRequest) and operation_id not in self.submissions:
            self.submissions.append(operation_id)
            self.script(operation_id, states=self.states, result=self._result(request))
        outcome = super().submit(request, operation_id=operation_id)
        self.accepted.set()
        return outcome

    def _result(self, request: SlurmBatchRequest) -> SlurmBatchResult:
        stages = []
        skipped = False
        for stage in request.stages:
            exit_code = self.accuracy_exit if stage.name == "accuracy" else self.benchmark_exit
            stages.append(
                SlurmBatchStageResult(
                    name=stage.name,
                    exit_code=None if skipped else exit_code,
                    stdout=""
                    if skipped
                    else (self.benchmark_output if stage.name == "benchmark" else "ok"),
                    stderr="",
                    elapsed_seconds=None if skipped else 1.0,
                    skipped=skipped,
                )
            )
            skipped = skipped or (exit_code != 0 and request.stop_on_failure)
        return SlurmBatchResult(
            job_id="0",
            job_exit_code=0,
            job_output="",
            stages=tuple(stages),
            phase_timings_seconds={},
            content_cache_hits=0,
        )


class _Candidates(FakeWorkspaces):
    """Workspaces whose candidates exist on disk, as a real worktree would."""

    async def create_candidate(
        self, from_revision: str | None = None, *, member_id: str | None = None
    ) -> CandidateWorkspace:
        candidate = await super().create_candidate(from_revision, member_id=member_id)
        candidate.path.mkdir(parents=True, exist_ok=True)
        return candidate


@dataclass
class Stack:
    """One production-shaped executor and the Fakes beneath it."""

    executor: SemanticSlurmEvaluationExecutor
    cluster: ScenarioCluster
    snapshot: str


def _config() -> SlurmConfig:
    return SlurmConfig(
        name="test", remote_workspace_root="/runs", transport=SlurmSshTransport(host="test")
    )


async def build_stack(root: Path, cluster: ScenarioCluster | None = None) -> Stack:
    """Build the semantic Slurm executor over ``cluster``, reusing durable state under root."""
    cluster = cluster or ScenarioCluster()
    workspaces = _Candidates(FakeWorkspace(), supports_parallel_candidates=True)
    snapshot = await workspaces.root.snapshot("candidate")
    state = root / ".vibesys" / "state" / "evaluation"
    state.mkdir(parents=True, exist_ok=True)
    namespace = StateNamespace(project_root=root, root=state, portable=False)
    executor = SemanticSlurmEvaluationExecutor(
        _config(),
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=root / "slurm.toml",
            accuracy_command=("python", "accuracy.py"),
            benchmark_command=("python", "benchmark.py"),
        ),
        TrustedEvaluationPlan(
            accuracy_command="unused",
            benchmark_command="unused",
            benchmark_contract=ScalarBenchmarkContract(
                output_argument="--output", metric="throughput"
            ),
        ),
        workspaces,
        namespace,  # type: ignore[arg-type]  # the evaluation namespace is a StateNamespace
        root / "handles",
        stage_failure_text=render_stage_failure,
        cluster=cluster,
    )
    return Stack(executor, cluster, snapshot)


def plan(
    *, stages: tuple[str, ...] = ("accuracy", "benchmark"), candidate: str = "candidate"
) -> MeasurementPlan:
    """A resolved official measurement of one revision."""
    return MeasurementPlan(
        purpose="official",
        candidate=RevisionRef.of_git_commit(candidate),
        evaluator_digest=DIGEST,
        workload_digest=DIGEST,
        environment_digest=DIGEST,
        stages=tuple(MeasurementStage(stage_id=s, execution_budget=10.0) for s in stages),
        policy="ordered",
        recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest=DIGEST),
        submitted_at=0.0,
        queue_allowance=10.0,
        deadline_at=20.0,
    )


def submission(
    name: str = "sub", candidate: str = "candidate", override: MeasurementPlan | None = None
) -> SubmitMeasurement:
    """A canonical submission request."""
    return SubmitMeasurement(
        request_id=RequestId(root=name),
        scope=SCOPE,
        admission_id=ADMISSION,
        deadline_at=100.0,
        plan=override or plan(candidate=candidate),
    )
