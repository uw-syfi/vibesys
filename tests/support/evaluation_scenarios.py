"""Run semantic evidence producers, replacing only execution boundaries.

The builders return durable producer records and their public projections.
No helper manufactures evidence, evaluation handles, or stage result records.
"""

from __future__ import annotations

import asyncio
import string
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from enum import StrEnum, auto
from functools import cache
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import anyio
from hypothesis import strategies as st

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.run.evaluation_backend import SemanticEvaluationBackend, SemanticEvaluationIdentity
from vibesys.run.slurm_evaluation import SlurmSemanticEvaluationExecutor
from vs_evaluation.api import (
    ContentDigest,
    EvaluationCompleted,
    EvaluationFailed,
    EvaluationRequest,
    EvidenceKind,
    StoredEvaluation,
    SubmittedSemanticEvaluation,
    TrustedEvidence,
)
from vs_evaluation.api.testing import InMemoryEvaluationNamespace
from vs_evaluator_protocol.api import (
    PROTOCOL_VERSION,
    ErrorRecord,
    Hello,
    MetricSpec,
    PartialMeasurement,
    Progress,
    Result,
)
from vs_runtime.api import (
    AccuracyEvaluation,
    AgentEvaluation,
    BenchmarkEvaluation,
    CandidateWorkspace,
    MetricDirection,
)
from vs_runtime.api.infrastructure import ProtocolBenchmarkContract, TrustedEvaluationPlan
from vs_runtime.api.testing import FakeRun, FakeWorkspace, FakeWorkspaces
from vs_sandbox.api.slurm import PROFILE_OUTPUT_ROOT, SlurmEvaluationPlan, SlurmExecutionPolicy
from vs_slurm.api import (
    FakeConnector,
    SlurmCluster,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmJobRunner,
    SlurmJobStatus,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

__all__ = [
    "EvaluationScenario",
    "Producer",
    "ScenarioOutcome",
    "ScenarioSpec",
    "build_scenario",
    "capture_projection",
    "capture_submission",
    "scenario_specs",
    "slurm_scenario_specs",
]


class Producer(StrEnum):
    DIRECT = "direct"
    SLURM = "slurm"


class ScenarioOutcome(StrEnum):
    PASS = auto()
    CORRECTNESS_FAIL = "correctness_fail"
    INFRA_FAIL = "infra_fail"
    TIMEOUT = "timeout"


class _ExecutingWorkspaces(FakeWorkspaces):
    """Materialize scratch directories for the executing filesystem boundary."""

    async def create_candidate(
        self, from_revision: str | None = None, *, member_id: str | None = None
    ) -> CandidateWorkspace:
        candidate = await super().create_candidate(from_revision, member_id=member_id)
        await anyio.Path(candidate.path).mkdir(parents=True, exist_ok=True)
        return candidate


@dataclass(frozen=True)
class ScenarioSpec:
    """Execution inputs, never producer outputs. Acceptance is a consumer input."""

    outcome: ScenarioOutcome = ScenarioOutcome.PASS
    kinds: tuple[EvidenceKind, ...] = (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK)
    accepted: bool = True
    same_handle: bool = True
    metric: float = 12.0
    revision: str | None = None
    patch: str = "scenario candidate patch"
    failure: str | None = None
    benchmark_failure: bool = False
    scope_id: str | None = "original"
    late_failure: bool = False
    scheduler_failed: bool = False
    direction: MetricDirection | None = None
    unit: str | None = None
    partial: PartialMeasurement | None = None
    trusted_plan: TrustedEvaluationPlan | None = None


def capture_projection(spec: ScenarioSpec, producer: Producer = Producer.DIRECT) -> AgentEvaluation:
    """Produce a detached fixture outside an active event loop."""
    if spec.partial is None:
        return _cached_projection(spec, producer)
    return _capture_projection(spec, producer)


@cache
def _cached_projection(spec: ScenarioSpec, producer: Producer) -> AgentEvaluation:
    return _capture_projection(spec, producer)


def _capture_projection(spec: ScenarioSpec, producer: Producer) -> AgentEvaluation:

    async def capture(root: Path) -> AgentEvaluation:
        async with build_scenario(root, spec, producer) as scenario:
            return scenario.projection

    with TemporaryDirectory(prefix="evaluation-projection-") as directory:
        return asyncio.run(capture(Path(directory)))


async def capture_submission(
    spec: ScenarioSpec, producer: Producer = Producer.DIRECT
) -> tuple[EvaluationRequest, SubmittedSemanticEvaluation]:
    """Return immutable production inputs and handles for lifecycle-only Fakes."""
    with TemporaryDirectory(prefix="evaluation-submission-") as directory:
        async with build_scenario(Path(directory), spec, producer) as scenario:
            return scenario.record.request, scenario.submission


def scenario_specs(
    *,
    outcomes: tuple[ScenarioOutcome, ...] = tuple(ScenarioOutcome),
) -> st.SearchStrategy[ScenarioSpec]:
    """Generate stage sets, boundary failures, evidence availability and joins."""
    return st.builds(
        ScenarioSpec,
        outcome=st.sampled_from(outcomes),
        kinds=st.sampled_from(
            (
                (EvidenceKind.ACCURACY,),
                (EvidenceKind.BENCHMARK,),
                (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK),
            )
        ),
        accepted=st.booleans(),
        same_handle=st.booleans(),
        metric=st.integers(min_value=1, max_value=1000).map(float),
        patch=st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=32),
        benchmark_failure=st.booleans(),
        late_failure=st.booleans(),
        direction=st.one_of(st.none(), st.sampled_from(tuple(MetricDirection))),
        unit=st.one_of(st.none(), st.sampled_from(("requests/s", "ms", "tokens/s"))),
        partial=st.one_of(
            st.none(),
            st.builds(
                PartialMeasurement,
                name=st.just("warmup_throughput"),
                value=st.integers(min_value=1, max_value=1000).map(float),
                direction=st.sampled_from(("max", "min")),
                unit=st.just("requests/s"),
                target=st.just(1001.0),
                progress=st.builds(
                    Progress,
                    completed=st.integers(min_value=0, max_value=9),
                    required=st.just(10),
                    unit=st.just("rounds"),
                ),
            ),
        ),
    )


def slurm_scenario_specs() -> st.SearchStrategy[ScenarioSpec]:
    """Cross command verdicts with independently reported aggregate failures."""
    return st.tuples(
        scenario_specs(outcomes=(ScenarioOutcome.PASS, ScenarioOutcome.CORRECTNESS_FAIL)),
        st.booleans(),
    ).map(lambda pair: replace(pair[0], scheduler_failed=pair[1]))


@dataclass
class EvaluationScenario:
    """Actual captured submission, durable result and agent-facing reduction."""

    spec: ScenarioSpec
    backend: SemanticEvaluationBackend
    run: FakeRun
    workspaces_impl: FakeWorkspaces
    connector: FakeConnector | None
    submission: SubmittedSemanticEvaluation
    record: StoredEvaluation
    evidence: tuple[TrustedEvidence, ...]
    accepted_evidence: tuple[TrustedEvidence, ...]
    projection: AgentEvaluation
    candidate_patch: str
    namespace: InMemoryEvaluationNamespace

    @property
    def profile_capture_count(self) -> int:
        """Count captures actually executed by the Fake cluster's profile command."""
        captures = self.workspace.path.parent / "profile-captures.txt"
        return len(captures.read_text().splitlines()) if captures.exists() else 0

    @property
    def workspace(self) -> FakeWorkspace:
        """Return the submitted candidate workspace."""
        return self.workspaces_impl.root

    @property
    def workspaces(self) -> FakeWorkspaces:
        """Return the injected workspace API, including canonical patch export."""
        return self.workspaces_impl

    async def replay(self) -> SubmittedSemanticEvaluation:
        """Join identical content or execute a changed candidate through another scope."""
        _script_direct(self.run, self.spec)
        revision = self.workspace.revision
        assert isinstance(revision, str)
        if not self.spec.same_handle:
            revision = await self.workspace.snapshot("changed replay candidate")
            self.workspaces.set_patch(revision, f"{self.candidate_patch} replay change")
        submitted = await self.backend.submit_revision_evidence(
            revision,
            self.spec.kinds,
            scope_id=self.spec.scope_id if self.spec.same_handle else "other",
            own=self._schedule_fault,
        )
        await _finish(self.backend, submitted.handle_id)
        return submitted

    async def _schedule_fault(self, submitted: SubmittedSemanticEvaluation) -> None:
        _schedule_fault(self.connector, self.spec, submitted)


def _schedule_fault(
    connector: FakeConnector | None, spec: ScenarioSpec, submitted: SubmittedSemanticEvaluation
) -> None:
    if connector is not None and spec.outcome in {
        ScenarioOutcome.INFRA_FAIL,
        ScenarioOutcome.TIMEOUT,
    }:
        if spec.late_failure and len(spec.kinds) > 1:
            connector.script(submitted.handle_id, missing_stage_result=True)
        else:
            connector.script(
                submitted.handle_id, states=(SlurmJobStatus.FAILED,), missing_exit_status=True
            )
    elif connector is not None and spec.scheduler_failed:
        connector.script(submitted.handle_id, states=(SlurmJobStatus.FAILED,))


def _script_direct(run: FakeRun, spec: ScenarioSpec) -> None:
    if spec.outcome in {ScenarioOutcome.INFRA_FAIL, ScenarioOutcome.TIMEOUT}:
        error = (
            OSError("execution infrastructure failed")
            if spec.outcome is ScenarioOutcome.INFRA_FAIL
            else TimeoutError("evaluation timed out")
        )
        run.evaluation.script_accuracy(
            AccuracyEvaluation(executed=True)
            if spec.late_failure and len(spec.kinds) > 1
            else error
        )
        run.evaluation.script_benchmark(error)
        return
    failed = spec.outcome is ScenarioOutcome.CORRECTNESS_FAIL
    accuracy_failure = "accuracy failed" if spec.failure is None else spec.failure
    benchmark_failure = "benchmark failed" if spec.failure is None else spec.failure
    run.evaluation.script_accuracy(
        AccuracyEvaluation(
            executed=True,
            feedback=accuracy_failure if failed and not spec.benchmark_failure else None,
        )
    )
    run.evaluation.script_benchmark(
        BenchmarkEvaluation(
            executed=True,
            feedback=benchmark_failure if failed else None,
            row=None if failed else {"throughput": spec.metric},
            metric_name="throughput" if not failed else None,
            metric_direction=spec.direction,
            metric_unit=spec.unit,
            partial_measurement=spec.partial if failed else None,
        )
    )


def _slurm_executor(
    root: Path, workspaces: FakeWorkspaces, spec: ScenarioSpec
) -> tuple[SlurmSemanticEvaluationExecutor, FakeConnector]:
    connector = FakeConnector(root / "scheduler")
    config = SlurmConfig(
        name="scenario",
        remote_workspace_root=str(root / "remote"),
        transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
    )
    failed = spec.outcome is ScenarioOutcome.CORRECTNESS_FAIL
    accuracy_failure = "accuracy failed" if spec.failure is None else spec.failure
    benchmark_failure = "benchmark failed" if spec.failure is None else spec.failure
    stream = "\n".join(
        (
            Hello(
                protocol=PROTOCOL_VERSION,
                metrics={
                    "throughput": MetricSpec(
                        direction=spec.direction.value if spec.direction is not None else None,
                        unit=spec.unit,
                    )
                },
            ).model_dump_json(),
            (
                ErrorRecord(message=benchmark_failure, partial=spec.partial)
                if failed
                else Result(values={"throughput": spec.metric})
            ).model_dump_json(),
        )
    )
    accuracy = (
        sys.executable,
        "-c",
        "import sys; sys.stdout.write(" + repr(accuracy_failure) + "); raise SystemExit(1)"
        if failed and not spec.benchmark_failure
        else "pass",
    )
    benchmark = (
        sys.executable,
        "-c",
        "import sys; from pathlib import Path; Path(sys.argv[-1]).write_text("
        + repr(stream)
        + "); raise SystemExit("
        + ("1" if failed else "0")
        + ")",
    )
    profile_summary = "top kernels: gemm 61%; plan=" + (
        "default" if spec.trusted_plan is None else spec.trusted_plan.profile_command or "none"
    )
    profile = (
        sys.executable,
        "-c",
        "from pathlib import Path\nwith Path("
        + repr(str(root / "profile-captures.txt"))
        + ").open('a') as captures:\n    captures.write('capture' + chr(10))\n"
        + (
            "print(" + repr(spec.failure or "profile failed") + "); raise SystemExit(1)"
            if failed
            else "Path("
            + repr(PROFILE_OUTPUT_ROOT)
            + ").mkdir(); print("
            + repr(profile_summary)
            + ")"
        ),
    )
    if spec.outcome is ScenarioOutcome.TIMEOUT:
        benchmark = (
            sys.executable,
            "-c",
            "print('evaluation timed out'); raise SystemExit(124)",
        )
        if not spec.late_failure:
            accuracy = benchmark
    trusted = spec.trusted_plan or TrustedEvaluationPlan(
        accuracy_command="scenario-accuracy",
        benchmark_command="scenario-benchmark",
        benchmark_contract=ProtocolBenchmarkContract(output_argument="--output"),
    )
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(remote_python=sys.executable),
        SlurmEvaluationPlan(
            config_path=root / "slurm.toml",
            accuracy_command=accuracy,
            benchmark_command=benchmark,
            profile_command=profile,
        ),
        trusted,
        workspaces,
        InMemoryEvaluationNamespace(),
        root / "handles",
        cluster=SlurmCluster(
            SlurmJobRunner(config, process=connector), state_root=root / "cluster"
        ),
    )
    return executor, connector


async def _finish(backend: SemanticEvaluationBackend, handle_id: str) -> None:
    # This bound is a hung-test guard, not the simulated timeout verdict.
    result = await backend.await_result(handle_id, 60)
    assert isinstance(result, EvaluationCompleted | EvaluationFailed)


@asynccontextmanager
async def build_scenario(
    root: Path,
    spec: ScenarioSpec | None = None,
    producer: Producer = Producer.DIRECT,
) -> AsyncIterator[EvaluationScenario]:
    """Execute real producers and own all executor cleanup for one isolated case."""
    spec = spec or ScenarioSpec()
    await anyio.Path(root).mkdir(parents=True, exist_ok=True)
    run = FakeRun(PLUGIN, project_root=root / "project", supports_parallel_candidates=True)
    workspaces = _ExecutingWorkspaces(
        FakeWorkspace(path=root / "project", revision=spec.revision or "fake-revision"),
        supports_parallel_candidates=True,
    )
    revision = spec.revision or await workspaces.root.snapshot("candidate")
    workspaces.set_patch(revision, spec.patch)
    namespace = InMemoryEvaluationNamespace()
    identity = SemanticEvaluationIdentity(
        evaluator=ContentDigest.sha256(b"evaluator"),
        workload=ContentDigest.sha256(b"workload"),
        environment=ContentDigest.sha256(b"environment"),
    )
    executor = None
    connector = None
    if producer is Producer.SLURM:
        executor, connector = _slurm_executor(root, workspaces, spec)
    backend = SemanticEvaluationBackend(
        run.evaluation,
        workspaces,
        namespace,
        identity,
        executor=executor,
        submitted_time=lambda: 100.0,
        plan=spec.trusted_plan,
        queue_allowance_seconds=1 if spec.trusted_plan is not None else None,
    )
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(run.close)
        cleanup.push_async_callback(workspaces.close)
        cleanup.push_async_callback(backend.close)

        # Claim before dispatch lets the executing connector inject faults under
        # the producer's real stable handle, without fabricating its identity.
        async def own(submitted: SubmittedSemanticEvaluation) -> None:
            _schedule_fault(connector, spec, submitted)

        _script_direct(run, spec)
        submission = await backend.submit_revision_evidence(
            revision, spec.kinds, scope_id=spec.scope_id, own=own
        )
        await _finish(backend, submission.handle_id)
        record = await backend.recorded_snapshot(submission.handle_id)
        evidence = tuple(
            TrustedEvidence.model_validate(stage.result)
            for stage in record.stage_results
            if stage.result is not None
        )
        (projection,) = await backend.agent_evaluations((submission.handle_id,))
        yield EvaluationScenario(
            spec,
            backend,
            run,
            workspaces,
            connector,
            submission,
            record,
            evidence,
            await backend.evidence_for(workspaces.root, spec.kinds),
            projection,
            await workspaces.export_patch(revision),
            namespace,
        )
